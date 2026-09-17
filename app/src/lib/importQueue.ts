/**
 * The import queue: every run this organisation has, and what each is doing.
 *
 * **The catalog is the source of truth, and that removed a whole layer.**
 * Enqueuing an import *is* starting the workflow — the free stages cost nothing
 * and the gate is where money is decided — so every queued item is a `run` row
 * from the moment it exists. There is nothing to persist client-side, nothing to
 * reconcile, and nothing that can drift: a relaunch, a crash, or a different
 * machine all see the same queue, because none of it was ever in this process.
 *
 * That is the fix for a screen that held exactly one run in `useState` and
 * forgot it on approval. A window reload, a plane switch or a second import
 * silently dropped the first, and after approving there was no feedback at all.
 *
 * **Two sources, because neither is enough alone** — the same split
 * `activeRuns.ts` records. `/runs` is discovery: catalog only, so it answers
 * with Temporal down, and it carries what each run has been billed. `/runs/{id}`
 * is the truth about a live one, asked only about runs that look live, because
 * asking it for finished runs would pay that cost for nothing.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  type GateReport,
  type RunListItem,
  type RunProgress,
  type VideoGateReport,
} from "./api";

/** How often to re-ask. Matches `activeRuns.ts`: slow enough not to matter, and
 *  roughly one chunk of semantic extraction per poll. */
export const QUEUE_POLL_MS = 5000;

/** How many runs the queue holds. Enough to cover a batch import and the days
 *  around it; the catalog keeps the rest, and the Library is where a reader goes
 *  looking for an old one. */
export const QUEUE_LIMIT = 30;

export interface QueueItem {
  run: RunListItem;
  /** Live from Temporal. `null` means nobody could say — an old plane, a run
   *  whose history has aged out, or a workflow inside a long activity that is
   *  not answering queries. All of them must read as *keep waiting*. */
  state: string | null;
  stage: string | null;
  progress: RunProgress | null;
  /** Present only while this run is parked at its gate.
   *
   *  Two fields rather than a union, because which one is filled follows from
   *  `run.kind` and the two reports genuinely differ: a video's carries a probe
   *  and an optional preview, a document's carries a required preview and a
   *  profile. Polling a video at `/gate` would decode its report into the wrong
   *  type and drop every field they do not share, without failing — the hazard
   *  `rebuild-gate` was split out to avoid. */
  gate: GateReport | null;
  videoGate: VideoGateReport | null;
}

/** A run the catalog thinks has not finished.
 *
 *  `finishedAt` rather than `state`, deliberately: the state column is written
 *  by the workflow and a run that died without recording an outcome keeps
 *  whatever it had, while the timestamp is only ever set by `finish_run`. */
export const unfinished = (run: RunListItem): boolean => run.finishedAt === null;

/** Whether this item is waiting on a person.
 *
 *  Read from the catalog rather than from Temporal, because the gate writes
 *  `state = 'awaiting_approval'` when it parks and that survives a Temporal the
 *  queue cannot reach. */
export const waiting = (run: RunListItem): boolean =>
  run.state === "awaiting_approval";

/**
 * The queue.
 *
 * **`libraryFilter` is a narrowing, and `null` means every library**, which is
 * the opposite of what this hook's argument used to mean. It took the selected
 * library and could not be asked for anything else, so a run started from a
 * screen that does not use the library picker — every probe the Channel tab
 * starts, whose library is `lib_yt_<channelId>` — was invisible here while the
 * sidebar, which reads `/project-summary` and is project-wide, showed it going.
 * Reported as "a video requested from Channel does not appear in Import"; the
 * 11 runs were parked correctly, in a library the queue was not asking about.
 *
 * The filter is a *request parameter* rather than a filter over what arrived:
 * `QUEUE_LIMIT` is 30, so narrowing after the fetch would show a library the
 * last 30 runs of *every* library happened to include, which is a different
 * and much smaller list than "the last 30 of this one".
 */
export function useImportQueue(libraryFilter: string | null) {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  // A ref, not state: the effect below must not re-subscribe when a poll lands.
  const alive = useRef(true);
  // Which gates have already been fetched, so a run parked for seven days is
  // not re-asked every five seconds for a report that cannot change.
  const gates = useRef(new Map<string, GateReport | VideoGateReport>());

  const poll = useCallback(async () => {
    let page;
    try {
      page = await api.runsList({
        // Omitted rather than sent empty: the route reads an absent
        // `library_id` as "every library", which is the whole point.
        ...(libraryFilter ? { libraryId: libraryFilter } : {}),
        limit: QUEUE_LIMIT,
      });
    } catch (e) {
      if (alive.current) {
        setError(e);
        setLoaded(true);
      }
      return;
    }
    if (alive.current) setError(null);

    const next = await Promise.all(
      page.runs.map(async (run): Promise<QueueItem> => {
        let state: string | null = run.state;
        let stage: string | null = run.stage;
        let progress: RunProgress | null = null;
        if (unfinished(run)) {
          try {
            const live = await api.runStatus(run.workflowId);
            // `null` from Temporal is "nobody could say", never "failed", so the
            // catalog's answer stands rather than being overwritten with nothing.
            state = live.state ?? run.state;
            stage = live.stage ?? run.stage;
            progress = live.progress;
          } catch {
            // A run the control plane could not describe keeps what the catalog
            // said. Dropping it would make a queue that hides work in flight
            // exactly when something is wrong.
          }
        }

        // A bucket recording publishes the same report a video does, on its
        // own route; the queue renders both through `VideoGateReview`.
        const isVideo = run.kind === "video" || run.kind === "audio";
        let cached = gates.current.get(run.workflowId) ?? null;
        if (cached === null && waiting(run)) {
          try {
            cached =
              run.kind === "audio"
                ? await api.audioGate(run.workflowId)
                : isVideo
                  ? await api.videoGate(run.workflowId)
                  : await api.ingestGate(run.workflowId);
            if (cached) gates.current.set(run.workflowId, cached);
          } catch {
            // 404 for a rebuild parked at its own gate, which this queue does
            // not render a report for. Not an error the person can act on.
          }
        }
        return {
          run,
          state,
          stage,
          progress,
          gate: isVideo ? null : (cached as GateReport | null),
          videoGate: isVideo ? (cached as VideoGateReport | null) : null,
        };
      }),
    );
    if (alive.current) {
      setItems(next);
      setLoaded(true);
    }
  }, [libraryFilter]);

  useEffect(() => {
    alive.current = true;
    // Immediately, not after the first interval: a relaunch must show the queue
    // it already has rather than an empty panel for five seconds.
    void poll();
    const timer = window.setInterval(() => void poll(), QUEUE_POLL_MS);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
    };
  }, [poll]);

  /** Forget a gate report, so the next poll fetches the run's current state.
   *  Called after a decision: the report is about a question already answered. */
  const settled = useCallback((workflowId: string) => {
    gates.current.delete(workflowId);
  }, []);

  return { items, error, loaded, refresh: poll, settled };
}
