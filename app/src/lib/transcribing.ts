/**
 * The decisions behind transcribing on this machine, as functions.
 *
 * Same split as `lib/bucket.ts` beside it and for the same reason: what is
 * worth asserting here is arithmetic and eligibility — is this machine ready,
 * which run goes next, how many hours 147 recordings will take — and a test
 * that had to mount a provider, fake five Tauri commands and wait on a poll to
 * check a multiplication would be worth very little.
 *
 * The provider in `localTranscriber.tsx` holds the polling and the calls and
 * no decisions.
 */
import type { BucketObjectRow, RunListItem, WhisperStatus } from "./api";

/** How often to ask the plane for runs parked for a transcript.
 *
 *  Slower than the import queue's five seconds: a run parked here waits up to
 *  fourteen days, so nothing is gained by asking often, and a transcription is
 *  already saturating the machine when it matters most. */
export const TRANSCRIBE_POLL_MS = 20000;

/** The state a bucket run sits in while it waits for this machine. Written by
 *  the workflow when it parks, so it survives a Temporal nobody can reach. */
export const AWAITING = "awaiting_transcript";

/** How many times one run is attempted before it is left alone.
 *
 *  Two, not more: a failure here is a missing model, a container the decoder
 *  cannot read or a machine that ran out of disk, and none of those is fixed
 *  by trying again immediately. The run stays parked and the person is offered
 *  Amazon, which is the remedy that always works. */
export const MAX_ATTEMPTS = 2;

/** Where a run is, as this queue sees it. */
export interface Job {
  workflowId: string;
  libraryId: string;
  documentId: string;
  title: string;
  /** From the bucket catalogue, so the refusal for an undecodable container
   *  happens before the audio is downloaded rather than after. */
  container: string;
  durationS: number;
  language: string;
}

export interface Attempt {
  phase: "audio" | "transcribe" | "upload";
  done: number;
  total: number;
}

/**
 * Whether this machine can transcribe at all, and what is missing when not.
 *
 * Three answers rather than a boolean, because they call for three different
 * things on the screen: a build with no binary needs a script run, a model
 * that is not downloaded needs a button pressed, and everything else is ready.
 */
export function readiness(
  status: WhisperStatus | null,
  model: string,
): "unknown" | "no_binary" | "no_model" | "ready" {
  if (!status) return "unknown";
  if (!status.binary) return "no_binary";
  if (!status.models.some((m) => m.name === model && m.present)) return "no_model";
  return "ready";
}

/** The model a person chose, falling back to the build's own default when
 *  what they chose is not a model this app knows — a bundle downgraded under a
 *  remembered preference must not leave the queue pointing at nothing. */
export function chosenModel(status: WhisperStatus | null, remembered: string | null): string {
  if (!status) return remembered ?? "";
  if (remembered && status.models.some((m) => m.name === remembered)) return remembered;
  return status.defaultModel;
}

/** `es-US` → `es`. Transcribe wants the region and whisper.cpp refuses it. */
export function languageOf(source: string): string {
  const code = (source || "es-US").trim().toLowerCase().split(/[-_]/)[0];
  return code || "es";
}

/**
 * The runs waiting for this machine, in the order they will be taken.
 *
 * Oldest first, which is the order they were approved in — a person who ticked
 * a hundred recordings gets them back in the order the screen lists them,
 * rather than in whatever order the catalog's page happened to arrive.
 */
export function waitingRuns(runs: RunListItem[]): RunListItem[] {
  return runs
    .filter((r) => r.state === AWAITING && r.finishedAt === null)
    .filter((r) => r.libraryId !== null && r.documentId !== null)
    .slice()
    .sort((a, b) => a.startedAt.localeCompare(b.startedAt));
}

/**
 * The next run to attempt: the first waiting one this machine has not already
 * failed at `MAX_ATTEMPTS` times, and whose container it can decode.
 *
 * A run it cannot decode is skipped rather than failed, because the paid plane
 * should never have started it locally — `transcriberFor` refuses the
 * container at quote time — so meeting one here means the two lists disagree,
 * and silently burning two attempts on it would hide that.
 */
export function nextJob(
  jobs: Job[],
  attempts: ReadonlyMap<string, number>,
  decodable: readonly string[],
): Job | null {
  return (
    jobs.find(
      (j) =>
        (attempts.get(j.workflowId) ?? 0) < MAX_ATTEMPTS &&
        decodable.includes(j.container),
    ) ?? null
  );
}

/** Join a waiting run to the catalogue row that knows its container and
 *  duration. The run row carries neither, and the object row is the only place
 *  either is recorded without a second read of the bucket. */
export function toJob(
  run: RunListItem,
  rows: ReadonlyMap<string, BucketObjectRow>,
  language: string,
): Job | null {
  if (!run.libraryId || !run.documentId) return null;
  const object = rows.get(run.workflowId) ?? null;
  return {
    workflowId: run.workflowId,
    libraryId: run.libraryId,
    documentId: run.documentId,
    title: object?.title || run.title || run.label || run.workflowId,
    container: object?.container ?? "",
    durationS: object?.durationS ?? 0,
    language,
  };
}

/** The catalogue's rows keyed by the run that is indexing them. */
export function rowsByRun(rows: BucketObjectRow[]): Map<string, BucketObjectRow> {
  const out = new Map<string, BucketObjectRow>();
  for (const row of rows) if (row.runId) out.set(row.runId, row);
  return out;
}

/**
 * How long this machine will take over some audio, at the speed it last
 * measured — `null` until it has transcribed something.
 *
 * This is the figure that makes the choice a real one: Amazon quotes dollars
 * and this quotes hours, and nobody can trade the two without both. Deliberately
 * not guessed from the model's name or the backend: a 4060 and a laptop's
 * integrated GPU both report `cuda` and are an order of magnitude apart.
 */
export function hoursFor(audioSeconds: number, speed: number | null): number | null {
  if (!speed || speed <= 0 || audioSeconds <= 0) return null;
  return audioSeconds / speed / 3600;
}

/** `1.8x` — how much faster than real time the last run went. */
export function speedLabel(speed: number | null): string {
  if (!speed || speed <= 0) return "";
  return speed >= 10 ? `${Math.round(speed)}x` : `${speed.toFixed(1)}x`;
}

/** A percentage for the progress bar, whatever phase reported it. */
export function percent(attempt: Attempt | null): number {
  if (!attempt) return 0;
  if (attempt.total <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((attempt.done / attempt.total) * 100)));
}
