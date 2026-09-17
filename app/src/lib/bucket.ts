/**
 * The Bucket screen's arithmetic, pure, so it can be asserted without a DOM.
 *
 * What the screen decides — which rows are ticked, what the ticked rows would
 * cost, what "quote these" sends — is a set of functions over the plane's
 * payload and the gates that have parked. The same split `lib/channel.ts`
 * makes for the same reason: jsdom lays out nothing and asserts nothing about
 * a `<table>`, and the properties worth pinning are about numbers.
 *
 * Simpler than the channel's, and the difference is worth naming: **the plane
 * joins the run onto the row**. `GET /buckets/{id}` reads the catalog for each
 * object's document, active version and in-flight run, so `runId` arrives on
 * the row and nothing here reconstructs a join from the import queue. What is
 * left to do client-side is to attach the gate a parked run has published,
 * because that is a live workflow query the plane does not pre-fetch.
 */

import type { BucketObjectRow, StageOptions, VideoGateReport } from "./api";

export { duration } from "./channel";

/** How often the parked probes are asked for their gate. */
export const PROBE_POLL_MS = 5000;

/** One row of the objects table: the plane's row plus the gate, if parked. */
export interface Row {
  object: BucketObjectRow;
  /** The probe's gate, once it has parked at one and this screen has read it. */
  gate: VideoGateReport | null;
}

/** The prefix `s3source.library_id_for` puts in front of a bucket id. */
export const BUCKET_LIBRARY_PREFIX = "lib_s3_";

export function bucketIdFor(libraryId: string | null): string | null {
  if (!libraryId || !libraryId.startsWith(BUCKET_LIBRARY_PREFIX)) return null;
  return libraryId.slice(BUCKET_LIBRARY_PREFIX.length);
}

export function buildRows(
  objects: BucketObjectRow[],
  gates: Record<string, VideoGateReport>,
): Row[] {
  return objects.map((object) => ({
    object,
    gate: object.runId ? (gates[object.runId] ?? null) : null,
  }));
}

export const indexed = (row: Row): boolean => row.object.state === "indexed";
export const pending = (row: Row): boolean => row.object.state === "pending";
/** A row's gate is answerable: it exists and nobody has answered it yet. */
export const awaiting = (row: Row): boolean => row.gate !== null;

/** What the plane will quote, and what a person may tick before it has.
 *
 *  An object that is unavailable, indexed, or already in flight is not
 *  quotable: the first is gone, the second would short-circuit at
 *  `link_duplicate` for nothing, the third has a run already. `reindex` lifts
 *  the second, deliberately: re-indexing is a choice, and it costs the full
 *  transcription unless the archive holds the transcript. */
export function quotable(row: Row, reindex = false): boolean {
  if (!row.object.available) return false;
  if (row.object.state === "pending") return false;
  if (row.object.state === "indexed" && !reindex) return false;
  return true;
}

export function toggle(picked: Set<string>, key: string): Set<string> {
  const next = new Set(picked);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

/** The text a title search matches: title, key and source, folded. */
export function rowText(row: Row): string {
  return `${row.object.title} ${row.object.key} ${row.object.source}`
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

export interface Filters {
  text: string;
  source: string;
  /** `all`, `indexed`, `pending`, `unindexed`, `awaiting`. */
  state: string;
  yearFrom: string;
  yearTo: string;
}

export const NO_FILTERS: Filters = { text: "", source: "", state: "all", yearFrom: "", yearTo: "" };

export function filterRows(rows: Row[], filters: Filters): Row[] {
  const needle = filters.text.trim().normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  return rows.filter((row) => {
    if (needle && !rowText(row).includes(needle)) return false;
    if (filters.source && row.object.source !== filters.source) return false;
    if (filters.state === "awaiting") {
      if (!awaiting(row)) return false;
    } else if (filters.state !== "all" && row.object.state !== filters.state) {
      return false;
    }
    const year = row.object.recordedAt.slice(0, 4);
    if (filters.yearFrom && (!year || year < filters.yearFrom)) return false;
    if (filters.yearTo && (!year || year > filters.yearTo)) return false;
    return true;
  });
}

/** The distinct sources on the table, in the order they first appear. */
export function sources(rows: Row[]): string[] {
  const out: string[] = [];
  for (const row of rows) {
    const s = row.object.source;
    if (s && !out.includes(s)) out.push(s);
  }
  return out;
}

/** What this row would cost to index, low and high. `null` means the gate has
 *  not quoted it yet — never zero, which would read as free. */
export function cost(row: Row): { usd: number | null; high: number | null } {
  const estimate = row.gate?.estimate;
  if (!estimate) return { usd: null, high: null };
  return { usd: estimate.totalUsd, high: estimate.totalUsdHigh ?? estimate.totalUsd };
}

/**
 * The sum on the screen: what the ticked rows come to.
 *
 * `unpriced` is the count of ticked rows whose gate reports no price or has
 * not parked yet, reported rather than folded in as zero — the rule every
 * figure in this product follows. `transcription` is broken out because it is
 * the line that dwarfs the rest, and `seconds` is what a person can reason
 * about before a single gate has parked: the quote is `seconds × the published
 * rate`, and the rate lives on the plane.
 */
export function totals(
  rows: Row[],
  picked: ReadonlySet<string>,
): {
  count: number;
  seconds: number;
  estimated: number;
  usd: number | null;
  usdHigh: number | null;
  transcription: number | null;
  unpriced: number;
} {
  const chosen = rows.filter((r) => picked.has(r.object.key));
  let usd: number | null = null;
  let high: number | null = null;
  let transcription: number | null = null;
  let unpriced = 0;
  for (const row of chosen) {
    const { usd: low, high: top } = cost(row);
    if (low === null) {
      unpriced += 1;
    } else {
      usd = (usd ?? 0) + low;
      high = (high ?? 0) + (top ?? low);
    }
    for (const stage of row.gate?.estimate.stages ?? []) {
      if (stage.stage === "transcription" && stage.usd !== null) {
        transcription = (transcription ?? 0) + stage.usd;
      }
    }
  }
  return {
    count: chosen.length,
    seconds: chosen.reduce((s, r) => s + r.object.durationS, 0),
    estimated: chosen.filter((r) => r.object.durationEstimated).length,
    usd,
    usdHigh: high,
    transcription,
    unpriced,
  };
}

/**
 * How many of the ticked rows this machine's decoder cannot read.
 *
 * The plane refuses them one by one at quote time — `transcriberFor` starts an
 * M4A on Amazon whatever the batch asked — so this changes nothing about what
 * happens. What it changes is whether the person finds out before pressing the
 * button or afterwards, from a per-key line in the answer. Four of the first
 * corpus's 147 recordings are M4A.
 */
export function undecodable(
  rows: Row[],
  picked: ReadonlySet<string>,
  decodable: readonly string[],
): number {
  return rows.filter(
    (r) => picked.has(r.object.key) && !decodable.includes(r.object.container),
  ).length;
}

/** The keys a "quote" sends: ticked, quotable, and not already parked. */
export function toProbe(rows: Row[], picked: ReadonlySet<string>, reindex = false): string[] {
  return rows
    .filter((r) => picked.has(r.object.key) && quotable(r, reindex) && r.object.runId === null)
    .map((r) => r.object.key);
}

/** The runs an "approve" answers: ticked rows whose gate has parked. */
export function toApprove(rows: Row[], picked: ReadonlySet<string>): Row[] {
  return rows.filter((r) => picked.has(r.object.key) && r.object.runId !== null && r.gate !== null);
}

/**
 * The switches a run is approved with: the gate's own recommendation for the
 * stages it decided, and the person's for the rest.
 *
 * Echoing the recommendation back is what keeps the number spent the number
 * quoted. `_recommended` on the worker now passes both `extract_semantics`
 * **and** `correct` through from what the run was started with, so the
 * recommendation *is* what the person chose before quoting — and changing
 * either here would spend against an estimate nobody was shown, which is the
 * recorded `VideoGateReview` defect in both of its directions.
 */
export function approvalOptions(row: Row, chosen: StageOptions): StageOptions {
  return {
    ...chosen,
    correct: row.gate?.recommended?.correct ?? false,
    embed: row.gate?.recommended?.embed ?? true,
  };
}

/** A recording's year, or "" when the manifest gave no date. */
export function year(row: Row): string {
  return row.object.recordedAt.slice(0, 4);
}

/** `1.2 GB`, `34 MB`. */
export function size(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${Math.round(bytes / 1024 ** 2)} MB`;
  return `${Math.round(bytes / 1024)} KB`;
}
