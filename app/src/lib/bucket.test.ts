import { describe, expect, it } from "vitest";

import type { BucketObjectRow, VideoGateReport } from "./api";
import {
  NO_FILTERS,
  approvalOptions,
  bucketIdFor,
  buildRows,
  filterRows,
  quotable,
  size,
  sources,
  toApprove,
  toProbe,
  toggle,
  totals,
} from "./bucket";
import { DEFAULT_STAGES } from "./api";

function object(key: string, over: Partial<BucketObjectRow> = {}): BucketObjectRow {
  return {
    key, etag: "e1", size: 34_000_000, lastModified: "", container: "mp3", durationS: 3600,
    durationEstimated: false, title: key.split("/").pop() ?? key, author: "",
    recordedAt: "1995-04-02", publishedAt: "", source: "iVoox", url: "", available: true,
    warnings: [], documentId: null, activeVersionId: null, runId: null, runState: null,
    state: "unindexed", ...over,
  };
}

function gate(usd: number, correct = false): VideoGateReport {
  return {
    runId: "audio-1", documentId: "doc", versionId: "ver",
    probe: {} as VideoGateReport["probe"],
    estimate: {
      stages: [{ stage: "transcription", model: "aws-transcribe-batch", inputTokens: 0,
                 outputTokens: 0, usd, outputTokensHigh: 0, usdHigh: usd }],
      totalUsd: usd, totalUsdHigh: usd, priceSource: "published", unpricedStages: [],
    } as unknown as VideoGateReport["estimate"],
    preview: null, transcript: null, warnings: [],
    recommended: { ...DEFAULT_STAGES, correct, embed: true },
  };
}

describe("what may be ticked", () => {
  it("excludes what is gone, what is in flight, and — unless re-indexing — what is indexed", () => {
    const [gone, flying, done, fresh] = buildRows([
      object("a", { available: false }),
      object("b", { state: "pending", runId: "audio-1" }),
      object("c", { state: "indexed", activeVersionId: "ver" }),
      object("d"),
    ], {});
    expect([gone!, flying!, done!, fresh!].map((r) => quotable(r))).toEqual([false, false, false, true]);
    expect(quotable(done!, true)).toBe(true);
    expect(quotable(gone!, true)).toBe(false);
  });

  it("toggles without mutating", () => {
    const a = new Set(["x"]);
    const b = toggle(a, "y");
    expect([...b]).toEqual(["x", "y"]);
    expect([...toggle(b, "x")]).toEqual(["y"]);
    expect([...a]).toEqual(["x"]);
  });
});

describe("the filters", () => {
  const rows = buildRows([
    object("audios/ivoox/1995-04-02_Si-Se-Humillare.mp3", { source: "iVoox" }),
    object("audios/anchor/2021-05-06_Domingo.mp3", { source: "Anchor", recordedAt: "2021-05-06" }),
    object("audios/podcast/2026-01-01_Ano.mp3", { source: "Podcast", recordedAt: "", state: "indexed" }),
  ], {});

  it("match text with accents folded, source, state and year", () => {
    expect(filterRows(rows, { ...NO_FILTERS, text: "humillare" })).toHaveLength(1);
    expect(filterRows(rows, { ...NO_FILTERS, text: "ANO" })).toHaveLength(1);
    expect(filterRows(rows, { ...NO_FILTERS, source: "Anchor" }).map((r) => r.object.key)).toEqual([
      "audios/anchor/2021-05-06_Domingo.mp3",
    ]);
    expect(filterRows(rows, { ...NO_FILTERS, state: "indexed" })).toHaveLength(1);
    expect(filterRows(rows, { ...NO_FILTERS, yearFrom: "2000" })).toHaveLength(1);
    // An undated row matches no year bound: it cannot be said to be inside.
    expect(filterRows(rows, { ...NO_FILTERS, yearTo: "2030" })).toHaveLength(2);
    expect(sources(rows)).toEqual(["iVoox", "Anchor", "Podcast"]);
  });

  it("`awaiting` means a gate has parked, whatever the catalog's state", () => {
    const withGate = buildRows(
      [object("a", { state: "pending", runId: "audio-1" }), object("b", { state: "pending", runId: "audio-2" })],
      { "audio-1": gate(1.44) },
    );
    expect(filterRows(withGate, { ...NO_FILTERS, state: "awaiting" }).map((r) => r.object.key)).toEqual(["a"]);
  });
});

describe("the sum on the screen", () => {
  it("adds minutes before any gate has parked, and dollars only from gates", () => {
    const rows = buildRows(
      [object("a", { durationS: 3600 }), object("b", { durationS: 1800, durationEstimated: true }),
       object("c", { state: "pending", runId: "audio-3", durationS: 600 })],
      { "audio-3": gate(0.24) },
    );
    const picked = new Set(["a", "b", "c"]);
    const sum = totals(rows, picked);
    expect(sum.count).toBe(3);
    expect(sum.seconds).toBe(6000);
    expect(sum.estimated).toBe(1);
    // Two of the three have no gate yet: unpriced, never zero.
    expect(sum.unpriced).toBe(2);
    expect(sum.usd).toBe(0.24);
    expect(sum.transcription).toBe(0.24);
  });

  it("is null, not zero, when nothing ticked has a price", () => {
    const rows = buildRows([object("a")], {});
    const sum = totals(rows, new Set(["a"]));
    expect(sum.usd).toBeNull();
    expect(sum.transcription).toBeNull();
  });
});

describe("what a quote and an approval send", () => {
  it("quotes only ticked, quotable, not-yet-started rows; approves only parked ones", () => {
    const rows = buildRows(
      [object("a"), object("b", { state: "pending", runId: "audio-2" }),
       object("c", { state: "pending", runId: "audio-3" }), object("d", { available: false })],
      { "audio-3": gate(1.44) },
    );
    const picked = new Set(["a", "b", "c", "d"]);
    expect(toProbe(rows, picked)).toEqual(["a"]);
    expect(toApprove(rows, picked).map((r) => r.object.key)).toEqual(["c"]);
  });

  it("echoes the gate's own recommendation for the stages it decided", () => {
    // Correction off for Transcribe output is the gate's decision; the person's
    // choice reaches the rest — semantics above all — so the number spent is
    // the number quoted.
    const [row] = buildRows([object("c", { state: "pending", runId: "audio-3" })], { "audio-3": gate(1.44) });
    const options = approvalOptions(row!, { ...DEFAULT_STAGES, extractSemantics: true, correct: true });
    expect(options.correct).toBe(false);
    expect(options.embed).toBe(true);
    expect(options.extractSemantics).toBe(true);
  });
});

describe("small helpers", () => {
  it("reads a bucket id off a library id and nothing else", () => {
    expect(bucketIdFor("lib_s3_ce2d1a0695b0")).toBe("ce2d1a0695b0");
    expect(bucketIdFor("lib_yt_UCabc")).toBeNull();
    expect(bucketIdFor(null)).toBeNull();
  });

  it("prints sizes a person reads", () => {
    expect(size(34_000_000)).toBe("32 MB");
    expect(size(5_020_000_000)).toBe("4.7 GB");
    expect(size(12_000)).toBe("12 KB");
  });
});
