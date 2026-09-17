/**
 * The background queue that transcribes bucket recordings on this machine.
 *
 * **Why a queue and not a button.** A run parked for a local transcript waits
 * up to fourteen days, and a hundred recordings on one GPU is a day's work —
 * so the unit of the feature is "leave it running", not "press this now". The
 * queue is derived from the plane on every poll and holds nothing of its own,
 * which is the same decision `importQueue.ts` records: enqueuing *is* the run
 * being parked, so a relaunch, a crash or a second machine all see the same
 * list and there is nothing to reconcile.
 *
 * **One at a time, deliberately.** A transcription saturates whatever it runs
 * on; two would each take twice as long, finish no sooner, and make the
 * progress bar a lie. The one useful concurrency — downloading the next
 * recording while the current one decodes — is not worth the second failure
 * mode.
 *
 * **It is mounted above the screens and outside their key**, so switching tab
 * does not stop it, exactly as `ImportScreen`'s gate poll had to stop being a
 * screen-local interval. Changing *plane* does stop it: the runs belong to the
 * organisation that was signed in, and the effect depends on the identity.
 *
 * **Paid plane only.** Bucket runs exist on no other, so in local mode this
 * polls nothing at all rather than asking a plane that answers 404.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  api,
  type BucketObjectRow,
  type Transcribed,
  type WhisperProgress,
  type WhisperStatus,
} from "./api";
import { scopedKey, useBackend } from "./backend";
import { bucketIdFor } from "./bucket";
import {
  AWAITING,
  MAX_ATTEMPTS,
  TRANSCRIBE_POLL_MS,
  chosenModel,
  hoursFor,
  languageOf,
  nextJob,
  readiness,
  rowsByRun,
  toJob,
  waitingRuns,
  type Attempt,
  type Job,
} from "./transcribing";

const MODEL_KEY = "companyBrain.whisperModel";
const PAUSED_KEY = "companyBrain.whisperPaused";

function stored(key: string, identity: string): string | null {
  try {
    return window.localStorage.getItem(scopedKey(key, identity));
  } catch {
    // Some webview configurations make this throw outright, and a settings
    // preference must degrade to "nothing remembered" rather than to a screen
    // that will not render.
    return null;
  }
}

function keep(key: string, identity: string, value: string): void {
  try {
    window.localStorage.setItem(scopedKey(key, identity), value);
  } catch {
    /* not worth telling the user about */
  }
}

/** What one finished attempt left behind, for the panel to show. */
export interface Outcome {
  workflowId: string;
  title: string;
  ok: boolean;
  /** Present on success: what it cost in wall time and what it ran on. */
  result?: Transcribed;
  error?: unknown;
  at: number;
}

interface TranscriberState {
  /** Null until the first read of the sidecar has answered. */
  status: WhisperStatus | null;
  model: string;
  setModel: (name: string) => void;
  readiness: ReturnType<typeof readiness>;
  /** Runs parked for this machine, oldest first. */
  jobs: Job[];
  /** The one being worked on, or null between them. */
  current: Job | null;
  attempt: Attempt | null;
  history: Outcome[];
  paused: boolean;
  setPaused: (paused: boolean) => void;
  /** Ask the binary what it will run on. Free; resolves false when it
   *  refused, with the reason in `error`. */
  probe: () => Promise<boolean>;
  probing: boolean;
  /** Download a model, reporting bytes. Resolves false when it refused. */
  download: (name: string) => Promise<boolean>;
  downloading: { name: string; done: number; total: number } | null;
  /** Hand a run back to Amazon: it re-quotes and parks at its gate again. */
  giveUp: (workflowId: string) => Promise<boolean>;
  /** How long the queue will take at the speed last measured, or null. */
  hoursLeft: number | null;
  refresh: () => Promise<void>;
  error: unknown;
}

const Ctx = createContext<TranscriberState | null>(null);

export function LocalTranscriberProvider({ children }: { children: ReactNode }) {
  const { info, identity } = useBackend();
  const cloud = info?.mode === "cloud";

  const [status, setStatus] = useState<WhisperStatus | null>(null);
  const [model, setModelState] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [current, setCurrent] = useState<Job | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [history, setHistory] = useState<Outcome[]>([]);
  const [paused, setPausedState] = useState(false);
  const [downloading, setDownloading] = useState<{ name: string; done: number; total: number } | null>(
    null,
  );
  const [probing, setProbing] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Refs, not state: the loop below must not restart because a poll landed,
  // and `working` is what keeps two ticks from starting the same run twice.
  const alive = useRef(true);
  const working = useRef(false);
  const attempts = useRef(new Map<string, number>());
  // What `readJobs` just found. The tick runs in the same turn as the fetch,
  // so the closure's `jobs` is still the previous render's array — this is the
  // list the decision is actually made against.
  const waiting = useRef<Job[]>([]);

  const readStatus = useCallback(async () => {
    try {
      const next = await api.whisperStatus();
      if (!alive.current) return;
      setStatus(next);
      setModelState((chosen) => chosen || chosenModel(next, stored(MODEL_KEY, identity)));
    } catch (e) {
      if (alive.current) setError(e);
    }
  }, [identity]);

  /** The runs parked for this machine, joined with the catalogue rows that
   *  know each one's container and duration. */
  const readJobs = useCallback(async () => {
    if (!cloud) {
      setJobs([]);
      return;
    }
    let runs;
    try {
      const page = await api.runsList({ kinds: "audio", states: AWAITING, limit: 100 });
      runs = waitingRuns(page.runs);
    } catch (e) {
      if (alive.current) setError(e);
      return;
    }
    // One catalogue read per bucket the waiting runs belong to, not per run:
    // the file holds every object, and a hundred runs of one corpus share it.
    const libraries = [...new Set(runs.map((r) => r.libraryId ?? ""))].filter(Boolean);
    const rows = new Map<string, BucketObjectRow>();
    const languages = new Map<string, string>();
    for (const libraryId of libraries) {
      const bucketId = bucketIdFor(libraryId);
      if (!bucketId) continue;
      try {
        const detail = await api.bucketDetail(bucketId);
        for (const [runId, row] of rowsByRun(detail.objects)) rows.set(runId, row);
        languages.set(libraryId, languageOf(detail.bucket.source.language));
      } catch {
        // A bucket somebody forgot while its runs were still parked. The runs
        // are still real and still transcribable; they lose only the title and
        // the container check, which the plane already made at quote time.
      }
    }
    const next = runs
      .map((r) => toJob(r, rows, languages.get(r.libraryId ?? "") ?? "es"))
      .filter((j): j is Job => j !== null);
    if (alive.current) {
      waiting.current = next;
      setJobs(next);
      setError(null);
    }
  }, [cloud]);

  /** One run, end to end: the presigned link, the model, the upload. */
  const run = useCallback(async (job: Job) => {
    setCurrent(job);
    setAttempt({ phase: "audio", done: 0, total: 0 });
    attempts.current.set(job.workflowId, (attempts.current.get(job.workflowId) ?? 0) + 1);
    try {
      // Minted here rather than kept anywhere: it dies with the assumed-role
      // session, and a queue that cached one would hand whisper a dead URL an
      // hour into a batch.
      const link = await api.mediaLink(job.libraryId, job.documentId, 0);
      const done = await api.whisperTranscribe(
        {
          workflowId: job.workflowId,
          audioUrl: link.url,
          container: job.container,
          model,
          language: job.language,
          audioSeconds: job.durationS,
        },
        (p: WhisperProgress) => {
          if (!alive.current) return;
          setAttempt({
            phase: p.phase === "transcribe" ? "transcribe" : "audio",
            done: p.done,
            total: p.total,
          });
        },
      );
      setAttempt({ phase: "upload", done: 0, total: 0 });
      await api.transcriptUpload(job.workflowId, done.path, {
        engine: done.engine,
        model: done.model,
        language: done.language,
      });
      if (!alive.current) return;
      setHistory((h) => [{ workflowId: job.workflowId, title: job.title, ok: true, result: done, at: Date.now() }, ...h].slice(0, 20));
      // It is no longer parked, so drop it from the list now rather than
      // leaving a finished run on screen until the next poll.
      waiting.current = waiting.current.filter((j) => j.workflowId !== job.workflowId);
      setJobs((rest) => rest.filter((j) => j.workflowId !== job.workflowId));
    } catch (e) {
      if (!alive.current) return;
      setHistory((h) => [{ workflowId: job.workflowId, title: job.title, ok: false, error: e, at: Date.now() }, ...h].slice(0, 20));
    } finally {
      if (alive.current) {
        setCurrent(null);
        setAttempt(null);
      }
    }
  }, [model]);

  // The tick: read what is waiting, then take one if nothing is in flight.
  const tick = useCallback(async () => {
    await readJobs();
    if (working.current || paused || !cloud) return;
    if (readiness(status, model) !== "ready") return;
    working.current = true;
    try {
      const job = nextJob(waiting.current, attempts.current, status?.decodable ?? []);
      if (job) await run(job);
    } finally {
      working.current = false;
    }
  }, [readJobs, paused, cloud, status, model, run]);

  // `alive` has one owner and one lifetime: two effects writing it meant a
  // re-read of the sidecar's status marked every other poll's answer as
  // arriving after unmount, and the queue silently stopped updating.
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    void readStatus();
  }, [readStatus]);

  useEffect(() => {
    if (!cloud) return;
    setPausedState(stored(PAUSED_KEY, identity) === "1");
  }, [cloud, identity]);

  useEffect(() => {
    if (!cloud) {
      setJobs([]);
      return;
    }
    void tick();
    const timer = window.setInterval(() => void tick(), TRANSCRIBE_POLL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [cloud, identity, tick]);

  const setModel = useCallback(
    (name: string) => {
      setModelState(name);
      keep(MODEL_KEY, identity, name);
    },
    [identity],
  );

  const setPaused = useCallback(
    (next: boolean) => {
      setPausedState(next);
      keep(PAUSED_KEY, identity, next ? "1" : "0");
    },
    [identity],
  );

  const probe = useCallback(async (): Promise<boolean> => {
    setProbing(true);
    setError(null);
    try {
      await api.whisperProbe();
      // Re-read rather than folding the answer in here: the fact is written to
      // disk by Rust, and one reader keeps the screen and the next launch from
      // disagreeing about the same machine.
      await readStatus();
      return true;
    } catch (e) {
      setError(e);
      return false;
    } finally {
      setProbing(false);
    }
  }, [readStatus]);

  const download = useCallback(
    async (name: string): Promise<boolean> => {
      setDownloading({ name, done: 0, total: 0 });
      setError(null);
      try {
        await api.whisperDownloadModel(name, (p) =>
          setDownloading({ name, done: p.done, total: p.total }),
        );
        await readStatus();
        return true;
      } catch (e) {
        setError(e);
        return false;
      } finally {
        setDownloading(null);
      }
    },
    [readStatus],
  );

  const giveUp = useCallback(
    async (workflowId: string): Promise<boolean> => {
      setError(null);
      try {
        await api.runSwitchTranscriber(workflowId);
        // It is back at a gate with Amazon's price on it, so it is no longer
        // this queue's business — and must not be re-attempted before the next
        // poll notices.
        attempts.current.set(workflowId, MAX_ATTEMPTS);
        waiting.current = waiting.current.filter((j) => j.workflowId !== workflowId);
        setJobs((rest) => rest.filter((j) => j.workflowId !== workflowId));
        return true;
      } catch (e) {
        setError(e);
        return false;
      }
    },
    [],
  );

  const hoursLeft = useMemo(
    () =>
      hoursFor(
        jobs.reduce((total, j) => total + j.durationS, 0),
        status?.measuredSpeed ?? null,
      ),
    [jobs, status],
  );

  const value = useMemo<TranscriberState>(
    () => ({
      status,
      model,
      setModel,
      readiness: readiness(status, model),
      jobs,
      current,
      attempt,
      history,
      paused,
      setPaused,
      probe,
      probing,
      download,
      downloading,
      giveUp,
      hoursLeft,
      refresh: async () => {
        await readStatus();
        await readJobs();
      },
      error,
    }),
    [
      status, model, setModel, jobs, current, attempt, history, paused, setPaused,
      probe, probing, download, downloading, giveUp, hoursLeft, readStatus, readJobs, error,
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** The queue, or a still one — a screen rendered outside the provider (every
 *  test that does not need it) gets an empty queue rather than a crash. */
export function useLocalTranscriber(): TranscriberState {
  return useContext(Ctx) ?? IDLE;
}

const IDLE: TranscriberState = {
  status: null,
  model: "",
  setModel: () => {},
  readiness: "unknown",
  jobs: [],
  current: null,
  attempt: null,
  history: [],
  paused: false,
  setPaused: () => {},
  probe: async () => false,
  probing: false,
  download: async () => false,
  downloading: null,
  giveUp: async () => false,
  hoursLeft: null,
  refresh: async () => {},
  error: null,
};
