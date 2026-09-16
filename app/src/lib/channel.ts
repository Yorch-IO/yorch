/**
 * The channel screen's arithmetic, kept out of the component.
 *
 * The same testability decision `radial.ts` and `force.ts` embody on the graph
 * side: jsdom lays out nothing, so a rendered test can assert that a row exists
 * and carries the attributes it was given, and nothing at all about what the
 * total says. What is worth asserting here is the *derivation* — which videos a
 * batch would index, what it would cost, and which ones start unticked — so it
 * lives in functions a test can call with numbers.
 */
import type {
  ChannelDetail,
  ChannelVideoRow,
  Candidate,
  Preselection,
  TopicsRecord,
  VideoGateReport,
  VideoTopics,
} from "./api";
import { titleMatches } from "./keywords";

/** How often the probes are re-read. The same figure as the import queue's,
 *  because it is the same catalog answering. */
export const PROBE_POLL_MS = 5000;

/** One row of the candidates table: everything known about one video, from all
 *  four sources that know something about it.
 *
 *  Assembled rather than fetched as one payload because the four have different
 *  owners and different lifetimes — the catalogue is a file, the verdict is an
 *  artifact, the reading is another artifact, and the gate is a live workflow
 *  query. Joining them here keeps each one's absence meaning what it means. */
export interface Row {
  video: ChannelVideoRow;
  /** The metadata verdict, or null if this video was not judged. */
  verdict: Candidate | null;
  /** What the transcript pass read, or null if it has not read this one. */
  reading: VideoTopics | null;
  /** The probe run's workflow id, once one has been started. */
  runId: string | null;
  /** The probe's gate, once it has parked at one. */
  gate: VideoGateReport | null;
}

/** Whether this video already has an index that can be asked. */
export const indexed = (row: Row): boolean => row.video.activeVersionId !== null;

/** Whether the probe found captions. **Absent captions is not absent
 *  information** — it means the transcript costs an Amazon Transcribe bill, and
 *  that there is no transcript for the topic pass to have read. */
export const hasCaptions = (row: Row): boolean =>
  row.gate?.probe.chosen != null;

/** Whether the gate has quoted a transcription. Read off the estimate rather
 *  than inferred from the captions, because the estimate is what the person is
 *  deciding against and the two must not be able to disagree. */
export const transcribes = (row: Row): boolean =>
  (row.gate?.estimate.stages ?? []).some((s) => s.stage === "transcription");

/** What this row would cost to index, low and high. `null` means the gate has
 *  not quoted it yet — never zero, which would read as free. */
export function cost(row: Row): { usd: number | null; high: number | null } {
  const estimate = row.gate?.estimate;
  if (!estimate) return { usd: null, high: null };
  return { usd: estimate.totalUsd, high: estimate.totalUsdHigh ?? estimate.totalUsd };
}

/** A row's gate is answerable: it exists and nobody has answered it yet. */
export const awaiting = (row: Row): boolean => row.gate !== null;

/** Whether this row's tick is answerable at all: not on the shelf already, and
 *  not a premiere that has not aired — `resolve_video` refuses those anyway, so
 *  offering the tick would offer a probe that cannot run. Before *and* after
 *  the probe: the tick is one choice for the whole flow, what to probe and then
 *  what to approve. It used to be enabled only once a gate had landed, which
 *  read as a checkbox that did not work. */
export const selectable = (row: Row): boolean =>
  !indexed(row) && row.video.liveState !== "upcoming";

/** The rows the table shows under a keyword filter.
 *
 *  No keys means everything. Otherwise a row is shown when its title carries
 *  any selected key — **or when it has a run**. A parked gate is a pending
 *  decision, and a filter that hid one would leave a person approving a batch
 *  with a row they could not see. Such a row is labelled as outside the filter
 *  rather than silently kept. */
export function filterRows(rows: Row[], keys: ReadonlySet<string>): Row[] {
  if (keys.size === 0) return rows;
  return rows.filter((r) => r.runId !== null || titleMatches(r.video.title, keys));
}

/** Whether a row is on screen only because it carries a run. */
export const outsideFilter = (row: Row, keys: ReadonlySet<string>): boolean =>
  keys.size > 0 && row.runId !== null && !titleMatches(row.video.title, keys);

/** How many more probes the quote allows.
 *
 *  `deepLimit` is the number of transcripts the quote priced, and the probes are
 *  what the transcript pass reads, so the budget is what is left of it after the
 *  runs already started — across rounds, not per press. Two rounds of ten
 *  against a cap of ten would read twenty transcripts quoted as ten, which is
 *  spending beyond the figure somebody approved: the one direction this product
 *  refuses. */
export function probeBudget(deepLimit: number, runs: Record<string, string>): number {
  return Math.max(0, deepLimit - Object.keys(runs).length);
}

/**
 * Which rows Discover's verdicts tick, once, when a discovery lands.
 *
 * The relevant and the doubtful — `dudoso` is included because that is the
 * whole point of the transcript pass that comes after — in the table's own
 * score order, minus anything not selectable, up to the budget. The same set
 * the probe used to compute for itself; it is a *seed* now, because the tick
 * belongs to the person and a verdict is a hypothesis about a title that they
 * may overrule in either direction.
 */
export function seedFromDiscovery(
  rows: Row[],
  preselection: Preselection,
  budget: number,
): Set<string> {
  const considered = new Set(
    preselection.candidates
      .filter((c) => c.relevancia === "relevante" || c.relevancia === "dudoso")
      .map((c) => c.video_id),
  );
  return new Set(
    rows
      .filter((r) => considered.has(r.video.videoId) && selectable(r) && r.runId === null)
      .slice(0, Math.max(0, budget))
      .map((r) => r.video.videoId),
  );
}

/**
 * Which ticks a landed gate takes away.
 *
 * **A video with no captions is unticked**, and that is the one judgement in
 * this file. Two measured facts make it, and they point the same way: there is
 * no transcript, so the topic pass could not read it and its only evidence is a
 * title; and at the published batch rate a 76-minute talk is about $1.82
 * against the $0.1545 that same talk cost with captions — roughly twelve times.
 * Twenty ticked boxes of which three cost twelve times the rest, in a list
 * nobody has read, is exactly the shape of thing that gets approved: the
 * recorded case is a gate that offered to index a machine translation into
 * Abkhazian, named in small print, under a heading saying nothing had been paid
 * for yet.
 *
 * A tick a gate *with* captions finds is left alone: the person put it there to
 * probe the video, and probing it is what made it approvable. The caller
 * applies this once per row, the moment its own gate arrives, and after that
 * the selection is the person's — re-ticking a transcribed video is a choice
 * the totals then print the price of.
 */
export function unpickOnGate(rows: Row[]): Set<string> {
  return new Set(
    rows.filter((r) => r.gate !== null && !hasCaptions(r)).map((r) => r.video.videoId),
  );
}

/** Ticked rows that would not be probed because the budget ran out — the
 *  figure the caveat beside the probe button prints, so a person who ticked
 *  thirty against a cap of ten is told rather than left counting. */
export function overCap(rows: Row[], picked: ReadonlySet<string>, budget: number): number {
  const waiting = rows.filter(
    (r) => picked.has(r.video.videoId) && r.runId === null && selectable(r),
  ).length;
  return Math.max(0, waiting - Math.max(0, budget));
}

export function toggle(picked: Set<string>, videoId: string): Set<string> {
  const next = new Set(picked);
  if (!next.delete(videoId)) next.add(videoId);
  return next;
}

/** What approving this selection would cost.
 *
 *  `unpriced` is the count of picked rows whose gate reports no price, and it
 *  is reported rather than folded in as zero — the rule every figure in this
 *  product follows. `transcription` is broken out because it is the line that
 *  can be an order of magnitude larger than the rest, and a total that hid it
 *  would be true and useless.
 */
export function totals(
  rows: Row[],
  picked: Set<string>,
): {
  count: number;
  usd: number | null;
  usdHigh: number | null;
  transcription: number | null;
  unpriced: number;
  withoutCaptions: number;
} {
  const chosen = rows.filter((r) => picked.has(r.video.videoId));
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
    usd,
    usdHigh: high,
    transcription,
    unpriced,
    withoutCaptions: chosen.filter((r) => r.gate !== null && !hasCaptions(r)).length,
  };
}

/**
 * Build the table from everything currently known.
 *
 * Order is the preselection's when there is one — best score first, so the
 * reason the batch exists is at the top — and the catalogue's otherwise, which
 * is newest first. A video the preselection discarded is **kept in the list**
 * rather than filtered out: "this was examined and rejected, and here is why" is
 * information, and a list that silently shrank would leave a person wondering
 * which of their hundred videos had been looked at.
 */
export function buildRows(
  detail: ChannelDetail | null,
  preselection: Preselection | null,
  topics: TopicsRecord | null,
  runs: Record<string, string>,
  gates: Record<string, VideoGateReport>,
): Row[] {
  if (!detail) return [];
  const verdicts = new Map<string, Candidate>(
    (preselection?.candidates ?? []).map((c) => [c.video_id, c]),
  );
  const readings = new Map<string, VideoTopics>(
    (topics?.videos ?? []).map((v) => [v.video_id, v]),
  );
  const rows: Row[] = detail.videos.map((video) => {
    const runId = runs[video.videoId] ?? null;
    return {
      video,
      verdict: verdicts.get(video.videoId) ?? null,
      reading: readings.get(video.videoId) ?? null,
      runId,
      gate: (runId && gates[runId]) || null,
    };
  });
  if (!preselection) return rows;
  const rank = new Map(preselection.evaluated.map((id, i) => [id, i]));
  return rows.sort((a, b) => {
    const sa = a.verdict?.puntaje ?? -1;
    const sb = b.verdict?.puntaje ?? -1;
    if (sa !== sb) return sb - sa;
    return (rank.get(a.video.videoId) ?? 0) - (rank.get(b.video.videoId) ?? 0);
  });
}

/** Which videos a probe would be started for.
 *
 *  The ticked rows that have no run yet, in table order, up to the budget. The
 *  preselection no longer decides this — it seeds the ticks and the person
 *  edits them — so a video chosen by hand from a keyword filter is probed on
 *  exactly the same footing as one a model called `relevante`. Capped because
 *  the budget is what the quote was computed from: probing more would spend
 *  beyond the figure somebody approved. */
export function toProbe(rows: Row[], picked: ReadonlySet<string>, budget: number): Row[] {
  return rows
    .filter((r) => picked.has(r.video.videoId) && r.runId === null && selectable(r))
    .slice(0, Math.max(0, budget));
}

/** `12:34` under an hour, `1:02:34` over it. Mirrors `videosource.hhmmss`. */
export function duration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
