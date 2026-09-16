/**
 * The channel table's derivations, as numbers.
 *
 * jsdom lays out nothing, so a rendered test could assert that a checkbox
 * exists and nothing about what the total beside it says. These are the
 * assertions worth having, and the one that matters most is the last group: a
 * video with no captions starts unticked, because it is both the worst informed
 * and the most expensive row in the table.
 */
import { describe, expect, it } from "vitest";

import type {
  Candidate,
  ChannelDetail,
  ChannelVideoRow,
  Estimate,
  Preselection,
  TopicsRecord,
  VideoGateReport,
  VideoTopics,
} from "./api";
import {
  buildRows,
  cost,
  duration,
  hasCaptions,
  indexed,
  initialPicked,
  toProbe,
  toggle,
  totals,
  type Row,
} from "./channel";

const video = (id: string, over: Partial<ChannelVideoRow> = {}): ChannelVideoRow => ({
  videoId: id,
  title: `Prédica ${id}`,
  description: "d",
  descriptionTruncated: false,
  publishedAt: "2026-01-01T00:00:00Z",
  durationS: 4573,
  liveState: "none",
  thumbnail: "",
  url: `https://youtu.be/${id}`,
  documentId: null,
  activeVersionId: null,
  ...over,
});

const detail = (videos: ChannelVideoRow[]): ChannelDetail => ({
  channel: {
    channelId: "UCabcdefghijklmnopqrstuv",
    title: "Casa Sobre la Roca",
    handle: "casarocachannel",
    description: "",
    uploadsPlaylistId: "UUabc",
    url: "https://www.youtube.com/@casarocachannel",
  },
  libraryId: "lib_yt_UCabcdefghijklmnopqrstuv",
  syncedAt: "2026-09-16T00:00:00+00:00",
  videoCount: videos.length,
  unitsSpent: 3,
  videos,
});

const candidate = (id: string, over: Partial<Candidate> = {}): Candidate => ({
  video_id: id,
  relevancia: "relevante",
  puntaje: 80,
  razon: "el título lo dice",
  incertidumbre: "baja",
  ...over,
});

const preselection = (candidates: Candidate[]): Preselection => ({
  topic: "justicia social",
  model: "gemini-3.6-flash",
  prompt_version: "preselect/1",
  evaluated: candidates.map((c) => c.video_id),
  candidates,
  invented: 0,
  malformed: 0,
  unevaluated: 0,
});

const estimate = (usd: number, stages: Estimate["stages"] = []): Estimate => ({
  stages,
  totalUsd: usd,
  totalUsdHigh: usd * 1.2,
  priceSource: "terceros",
  unpricedStages: [],
});

const gate = (
  captions: boolean,
  usd: number | null = 0.15,
  extra: Estimate["stages"] = [],
): VideoGateReport =>
  ({
    runId: "video-1",
    documentId: "doc",
    versionId: "ver",
    probe: {
      chosen: captions ? { language: "es-orig", kind: "auto", ext: "vtt" } : null,
    },
    estimate:
      usd === null
        ? { ...estimate(0, extra), totalUsd: null, totalUsdHigh: null }
        : estimate(usd, extra),
    preview: null,
    transcript: null,
    warnings: [],
    recommended: null,
  }) as unknown as VideoGateReport;

const row = (over: Partial<Row> & { video: ChannelVideoRow }): Row => ({
  verdict: null,
  reading: null,
  runId: null,
  gate: null,
  ...over,
});

// --- the table ---------------------------------------------------------------

describe("buildRows", () => {
  it("joins the four sources that each know part of a video", () => {
    const reading: VideoTopics = {
      video_id: "a",
      topic: "t",
      model: "m",
      prompt_version: "topics/1",
      temas: [{ tema: "justicia", evidencia: "hagamos justicia", confianza: "alta" }],
      responde: true,
      motivo: "",
      characters: 100,
      truncated: false,
      verified: 1,
      failed: false,
    };
    const topics: TopicsRecord = {
      topic: "t",
      channel_id: "c",
      prompt_version: "topics/1",
      videos: [reading],
    };
    const rows = buildRows(
      detail([video("a")]),
      preselection([candidate("a")]),
      topics,
      { a: "video-1" },
      { "video-1": gate(true) },
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]!.verdict?.puntaje).toBe(80);
    expect(rows[0]!.reading?.verified).toBe(1);
    expect(rows[0]!.runId).toBe("video-1");
    expect(rows[0]!.gate).not.toBeNull();
  });

  it("keeps a discarded video in the list rather than hiding it", () => {
    // "This was examined and rejected, and here is why" is information. A list
    // that silently shrank would leave a person wondering which of their
    // hundred videos had been looked at.
    const rows = buildRows(
      detail([video("a"), video("b")]),
      preselection([
        candidate("a", { relevancia: "descartado", puntaje: 3 }),
        candidate("b"),
      ]),
      null,
      {},
      {},
    );
    expect(rows.map((r) => r.video.videoId)).toEqual(["b", "a"]);
  });

  it("orders by score so the reason the batch exists is at the top", () => {
    const rows = buildRows(
      detail([video("a"), video("b"), video("c")]),
      preselection([
        candidate("a", { puntaje: 10 }),
        candidate("b", { puntaje: 95 }),
        candidate("c", { puntaje: 50 }),
      ]),
      null,
      {},
      {},
    );
    expect(rows.map((r) => r.video.videoId)).toEqual(["b", "c", "a"]);
  });

  it("keeps the catalogue's own order before anything has been judged", () => {
    const rows = buildRows(detail([video("a"), video("b")]), null, null, {}, {});
    expect(rows.map((r) => r.video.videoId)).toEqual(["a", "b"]);
  });

  it("is empty rather than throwing before a channel is chosen", () => {
    expect(buildRows(null, null, null, {}, {})).toEqual([]);
  });
});

// --- what a row costs --------------------------------------------------------

describe("cost", () => {
  it("is null before the gate has quoted, never zero", () => {
    // Zero would read as free, and the whole point of the gate is that nobody
    // knows the figure until the probe has run.
    expect(cost(row({ video: video("a") }))).toEqual({ usd: null, high: null });
  });

  it("carries the range the gate reported", () => {
    const r = row({ video: video("a"), gate: gate(true, 0.2) });
    expect(cost(r).usd).toBeCloseTo(0.2);
    expect(cost(r).high).toBeCloseTo(0.24);
  });
});

// --- the judgement this file makes -------------------------------------------

describe("initialPicked", () => {
  it("ticks a probed video with captions", () => {
    const rows = [row({ video: video("a"), runId: "r", gate: gate(true) })];
    expect([...initialPicked(rows)]).toEqual(["a"]);
  });

  it("leaves a video with no captions unticked", () => {
    // Two measured facts point the same way: there is no transcript, so the
    // topic pass could not read it and its only evidence is a title; and at
    // the published rate a 76-minute talk is ~$1.82 against the $0.1545 that
    // same talk cost with captions.
    const rows = [row({ video: video("a"), runId: "r", gate: gate(false) })];
    expect([...initialPicked(rows)]).toEqual([]);
  });

  it("leaves an already-indexed video unticked", () => {
    const rows = [
      row({
        video: video("a", { documentId: "doc", activeVersionId: "ver" }),
        runId: "r",
        gate: gate(true),
      }),
    ];
    expect([...initialPicked(rows)]).toEqual([]);
  });

  it("leaves a video that has not reached a gate unticked", () => {
    const rows = [row({ video: video("a"), runId: "r", gate: null })];
    expect([...initialPicked(rows)]).toEqual([]);
  });
});

describe("toggle", () => {
  it("adds and removes without mutating what it was given", () => {
    const start = new Set(["a"]);
    const added = toggle(start, "b");
    expect([...added].sort()).toEqual(["a", "b"]);
    expect([...start]).toEqual(["a"]);
    expect([...toggle(added, "a")]).toEqual(["b"]);
  });
});

// --- the total ---------------------------------------------------------------

describe("totals", () => {
  const rows = [
    row({ video: video("a"), runId: "r1", gate: gate(true, 0.15) }),
    row({
      video: video("b"),
      runId: "r2",
      gate: gate(false, 1.9, [
        {
          stage: "transcription",
          model: "aws-transcribe-batch",
          inputTokens: 0,
          outputTokens: 0,
          usd: 1.82,
          outputTokensHigh: 0,
          usdHigh: 1.82,
        },
      ]),
    }),
    row({ video: video("c"), runId: "r3", gate: gate(true, null) }),
  ];

  it("adds only what is ticked", () => {
    expect(totals(rows, new Set(["a"])).usd).toBeCloseTo(0.15);
    expect(totals(rows, new Set()).count).toBe(0);
    expect(totals(rows, new Set()).usd).toBeNull();
  });

  it("breaks transcription out, because it can dwarf everything else", () => {
    // A total that hid it would be true and useless: one row at twelve times
    // the rest is the fact a person is deciding about.
    const t = totals(rows, new Set(["a", "b"]));
    expect(t.usd).toBeCloseTo(2.05);
    expect(t.transcription).toBeCloseTo(1.82);
    expect(t.withoutCaptions).toBe(1);
  });

  it("counts an unpriced row rather than folding it in as zero", () => {
    const t = totals(rows, new Set(["a", "c"]));
    expect(t.unpriced).toBe(1);
    expect(t.usd).toBeCloseTo(0.15);
  });

  it("reports no transcription line when nothing ticked needs one", () => {
    expect(totals(rows, new Set(["a"])).transcription).toBeNull();
  });
});

// --- which videos get probed -------------------------------------------------

describe("toProbe", () => {
  const rows = (ids: string[], over: Partial<ChannelVideoRow> = {}) =>
    ids.map((id) => row({ video: video(id, over) }));

  it("probes nothing before anything has been judged", () => {
    expect(toProbe(rows(["a"]), null, 10)).toEqual([]);
  });

  it("probes the relevant and the doubtful, and not the discarded", () => {
    // `dudoso` is exactly what the transcript pass exists to settle.
    const pre = preselection([
      candidate("a", { relevancia: "relevante" }),
      candidate("b", { relevancia: "dudoso" }),
      candidate("c", { relevancia: "descartado" }),
      candidate("d", { relevancia: "sin_evaluar" }),
    ]);
    expect(toProbe(rows(["a", "b", "c", "d"]), pre, 10).map((r) => r.video.videoId))
      .toEqual(["a", "b"]);
  });

  it("stops at the limit the quote was computed from", () => {
    // Probing more than was quoted would spend beyond the figure somebody
    // approved, which is the one direction this product refuses.
    const pre = preselection(["a", "b", "c"].map((id) => candidate(id)));
    expect(toProbe(rows(["a", "b", "c"]), pre, 2)).toHaveLength(2);
  });

  it("skips what is already probed or already indexed", () => {
    const pre = preselection([candidate("a"), candidate("b")]);
    const list = [
      row({ video: video("a"), runId: "video-1" }),
      row({ video: video("b", { activeVersionId: "ver" }) }),
    ];
    expect(toProbe(list, pre, 10)).toEqual([]);
  });

  it("skips a premiere that has not aired", () => {
    const pre = preselection([candidate("a")]);
    expect(toProbe(rows(["a"], { liveState: "upcoming" }), pre, 10)).toEqual([]);
  });
});

// --- small things ------------------------------------------------------------

describe("helpers", () => {
  it("formats a duration the way the worker does", () => {
    expect(duration(4573)).toBe("1:16:13");
    expect(duration(74)).toBe("1:14");
    expect(duration(0)).toBe("0:00");
  });

  it("reads indexed off the active version and not the document", () => {
    // A document with no active version is a real state — a cancelled run, or
    // an activation withheld over a structural mismatch — not an index.
    expect(indexed(row({ video: video("a", { documentId: "doc" }) }))).toBe(false);
    expect(
      indexed(row({ video: video("a", { documentId: "doc", activeVersionId: "v" }) })),
    ).toBe(true);
  });

  it("reads captions off the probe", () => {
    expect(hasCaptions(row({ video: video("a"), gate: gate(true) }))).toBe(true);
    expect(hasCaptions(row({ video: video("a"), gate: gate(false) }))).toBe(false);
    expect(hasCaptions(row({ video: video("a") }))).toBe(false);
  });
});
