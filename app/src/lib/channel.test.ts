/**
 * The channel table's derivations, as numbers.
 *
 * jsdom lays out nothing, so a rendered test could assert that a checkbox
 * exists and nothing about what the total beside it says. These are the
 * assertions worth having, and the one that matters most is `unpickOnGate`: a
 * video with no captions loses its tick the moment its gate says so, because it
 * is both the worst informed and the most expensive row in the table.
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
  filterRows,
  hasCaptions,
  indexed,
  outsideFilter,
  overCap,
  probeBudget,
  seedFromDiscovery,
  selectable,
  toProbe,
  toggle,
  totals,
  unpickOnGate,
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

describe("unpickOnGate", () => {
  it("takes the tick from a probed video with no captions", () => {
    // Two measured facts point the same way: there is no transcript, so the
    // topic pass could not read it and its only evidence is a title; and at
    // the published rate a 76-minute talk is ~$1.82 against the $0.1545 that
    // same talk cost with captions.
    const rows = [row({ video: video("a"), runId: "r", gate: gate(false) })];
    expect([...unpickOnGate(rows)]).toEqual(["a"]);
  });

  it("leaves a probed video with captions as the person ticked it", () => {
    const rows = [row({ video: video("a"), runId: "r", gate: gate(true) })];
    expect([...unpickOnGate(rows)]).toEqual([]);
  });

  it("says nothing about a video whose gate has not landed", () => {
    // Nothing is known yet, so nothing is taken away; the caller applies this
    // once per row, the moment its own gate arrives.
    const rows = [row({ video: video("a"), runId: "r", gate: null })];
    expect([...unpickOnGate(rows)]).toEqual([]);
  });
});

describe("seedFromDiscovery", () => {
  const rows = (ids: string[]) => ids.map((id) => row({ video: video(id) }));

  it("ticks the relevant and the doubtful, and never the discarded", () => {
    // `dudoso` is exactly what the transcript pass exists to settle, and
    // `sin_evaluar` is the code's word for "no usable verdict", which is not
    // a reason to tick anything.
    const pre = preselection([
      candidate("a", { relevancia: "relevante" }),
      candidate("b", { relevancia: "dudoso" }),
      candidate("c", { relevancia: "descartado" }),
      candidate("d", { relevancia: "sin_evaluar" }),
    ]);
    expect([...seedFromDiscovery(rows(["a", "b", "c", "d"]), pre, 10)]).toEqual(["a", "b"]);
  });

  it("stops at the budget, in table order", () => {
    // Table order is the score order `buildRows` produced, so the budget
    // takes the best-scored first — the reason the batch exists is at the top.
    const pre = preselection(["a", "b", "c"].map((id) => candidate(id)));
    expect([...seedFromDiscovery(rows(["a", "b", "c"]), pre, 2)]).toEqual(["a", "b"]);
  });

  it("skips what is indexed, probed or not yet aired", () => {
    const pre = preselection(["a", "b", "c"].map((id) => candidate(id)));
    const list = [
      row({ video: video("a", { documentId: "doc", activeVersionId: "ver" }) }),
      row({ video: video("b"), runId: "video-1" }),
      row({ video: video("c", { liveState: "upcoming" }) }),
    ];
    expect([...seedFromDiscovery(list, pre, 10)]).toEqual([]);
  });
});

describe("selectable", () => {
  it("is live before the probe, which is the fix for a tick that read as broken", () => {
    expect(selectable(row({ video: video("a") }))).toBe(true);
    expect(selectable(row({ video: video("a"), runId: "r", gate: gate(true) }))).toBe(true);
  });

  it("is not offered for a video already on the shelf or one that has not aired", () => {
    expect(selectable(row({ video: video("a", { documentId: "d", activeVersionId: "v" }) })))
      .toBe(false);
    expect(selectable(row({ video: video("a", { liveState: "upcoming" }) }))).toBe(false);
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

  it("probes nothing when nothing is ticked", () => {
    expect(toProbe(rows(["a"]), new Set(), 10)).toEqual([]);
  });

  it("probes what is ticked, whoever ticked it", () => {
    // A video chosen by hand from a keyword filter is probed on the same
    // footing as one a model called `relevante`: the verdict seeds the tick,
    // and the tick is what decides.
    expect(
      toProbe(rows(["a", "b", "c"]), new Set(["a", "c"]), 10).map((r) => r.video.videoId),
    ).toEqual(["a", "c"]);
  });

  it("stops at the budget the quote was computed from", () => {
    // Probing more than was quoted would spend beyond the figure somebody
    // approved, which is the one direction this product refuses.
    expect(toProbe(rows(["a", "b", "c"]), new Set(["a", "b", "c"]), 2)).toHaveLength(2);
  });

  it("skips what is already probed or already indexed", () => {
    const list = [
      row({ video: video("a"), runId: "video-1" }),
      row({ video: video("b", { activeVersionId: "ver" }) }),
    ];
    expect(toProbe(list, new Set(["a", "b"]), 10)).toEqual([]);
  });

  it("skips a premiere that has not aired", () => {
    expect(toProbe(rows(["a"], { liveState: "upcoming" }), new Set(["a"]), 10)).toEqual([]);
  });
});

describe("probeBudget", () => {
  it("is what is left of the cap after the runs already started, across rounds", () => {
    // Two rounds of ten against a cap of ten would read twenty transcripts
    // quoted as ten.
    expect(probeBudget(10, {})).toBe(10);
    expect(probeBudget(10, { a: "r1", b: "r2", c: "r3" })).toBe(7);
    expect(probeBudget(2, { a: "r1", b: "r2", c: "r3" })).toBe(0);
  });
});

describe("overCap", () => {
  it("counts the ticked rows the budget leaves unprobed", () => {
    const list = [
      row({ video: video("a") }),
      row({ video: video("b") }),
      row({ video: video("c") }),
      row({ video: video("d"), runId: "r" }),                    // already probed: not waiting
      row({ video: video("e", { activeVersionId: "v" }) }),      // indexed: not probable
    ];
    expect(overCap(list, new Set(["a", "b", "c", "d", "e"]), 2)).toBe(1);
    expect(overCap(list, new Set(["a", "b", "c"]), 10)).toBe(0);
  });
});

// --- the keyword filter --------------------------------------------------------

describe("filterRows", () => {
  const list = [
    row({ video: video("a", { title: "La justicia de Dios" }) }),
    row({ video: video("b", { title: "El perdón" }) }),
    row({ video: video("c", { title: "Sobre la oración" }), runId: "video-1" }),
  ];

  it("shows everything when no key is pressed", () => {
    expect(filterRows(list, new Set())).toHaveLength(3);
  });

  it("narrows to titles carrying any selected key", () => {
    expect(filterRows(list, new Set(["justicia"])).map((r) => r.video.videoId)).toEqual([
      "a",
      "c",
    ]);
    expect(
      filterRows(list, new Set(["justicia", "perdon"])).map((r) => r.video.videoId),
    ).toEqual(["a", "b", "c"]);
  });

  it("keeps a row with a run whatever the filter says, and labels it", () => {
    // A parked gate is a pending decision, and a filter that hid one would
    // leave a person approving a batch with a row they could not see.
    const shown = filterRows(list, new Set(["perdon"]));
    expect(shown.map((r) => r.video.videoId)).toEqual(["b", "c"]);
    expect(outsideFilter(list[2]!, new Set(["perdon"]))).toBe(true);
    expect(outsideFilter(list[1]!, new Set(["perdon"]))).toBe(false);
    // Under no filter, nothing is "outside" it.
    expect(outsideFilter(list[2]!, new Set())).toBe(false);
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
