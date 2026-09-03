import { describe, expect, it } from "vitest";

import {
  bucketWidth,
  edgeBucket,
  EDGE_BUCKETS,
  EDGE_STYLE,
  EDGE_WIDTH,
  EDGE_WIDTH_MAX,
  paintEdges,
  type Ctx,
  type PaintEdges,
} from "./graphPainter";
import { scale } from "./radial";

/**
 * What reaches the canvas, asserted through a context that records instead of
 * painting.
 *
 * jsdom's `getContext("2d")` returns **null** — it logs "not implemented"
 * through its virtual console and does not throw — so a real one is never
 * available here. That is why the painter takes its context as an argument:
 * with it injected, everything except the pixels is checkable, and the null
 * case is a test rather than a crash.
 */

interface Recorder extends Ctx {
  calls: string[];
  strokeCalls: number;
  widths: number[];
  alphas: number[];
  transform: number[] | null;
}

function recorder(): Recorder {
  const r: Recorder = {
    calls: [],
    strokeCalls: 0,
    widths: [],
    alphas: [],
    transform: null,
    strokeStyle: "",
    lineWidth: 0,
    globalAlpha: 1,
    setTransform(a, b, c, d, e, f) {
      r.calls.push("setTransform");
      r.transform = [a, b, c, d, e, f];
    },
    clearRect() {
      r.calls.push("clearRect");
    },
    beginPath() {
      r.calls.push("beginPath");
      r.widths.push(r.lineWidth);
      r.alphas.push(r.globalAlpha);
    },
    moveTo() {
      r.calls.push("moveTo");
    },
    lineTo() {
      r.calls.push("lineTo");
    },
    stroke() {
      r.calls.push("stroke");
      r.strokeCalls += 1;
    },
  };
  return r;
}

/** Four nodes: two books, two concepts, one edge each way. */
function input(over: Partial<PaintEdges> = {}): PaintEdges {
  return {
    ctx: recorder(),
    box: { width: 1200, height: 820 },
    dpr: 1,
    fit: { scale: 1, x: 0, y: 0 },
    view: { zoom: 1, pan: { x: 0, y: 0 } },
    edges: Int32Array.from([0, 1, 2]),
    edgeSrc: Int32Array.from([0, 1, 0]),
    edgeDst: Int32Array.from([2, 3, 3]),
    edgeMentions: Int32Array.from([1, 10, 20]),
    maxMentions: 20,
    positions: Float32Array.from([10, 10, 20, 20, 30, 30, 40, 40]),
    lit: null,
    style: { stroke: "#889", ...EDGE_STYLE },
    ...over,
  };
}

describe("the widths", () => {
  it("keeps the range the SVG drew", () => {
    expect(bucketWidth(0)).toBeCloseTo(EDGE_WIDTH.MIN, 6);
    expect(bucketWidth(EDGE_BUCKETS - 1)).toBeCloseTo(EDGE_WIDTH_MAX, 6);
  });

  it("is monotone in the mention count", () => {
    let last = -1;
    for (const mentions of [1, 5, 10, 20, 50, 100]) {
      const bucket = edgeBucket(mentions, 100);
      expect(bucket).toBeGreaterThanOrEqual(last);
      last = bucket;
    }
  });

  it("rounds the width the SVG would have used, not something else", () => {
    for (const mentions of [1, 7, 33, 100]) {
      // The exact width the component drew, with `scale`'s fourth argument
      // read as the span it is.
      const exact = scale(mentions, 100, EDGE_WIDTH.MIN, EDGE_WIDTH.SPAN);
      const drawn = bucketWidth(edgeBucket(mentions, 100));
      // Half a bucket is the most quantising can cost.
      expect(Math.abs(drawn - exact)).toBeLessThanOrEqual(
        EDGE_WIDTH.SPAN / (EDGE_BUCKETS - 1) / 2 + 1e-9,
      );
    }
  });
});

describe("one frame", () => {
  it("draws every edge it was given, once", () => {
    const ctx = recorder();
    const result = paintEdges(input({ ctx }));
    expect(result.drawn).toBe(3);
    expect(ctx.calls.filter((c) => c === "moveTo")).toHaveLength(3);
  });

  it("costs one stroke per width, not one per edge", () => {
    // The reason the edges are bucketed at all: `lineWidth` is context state.
    // On the real library this is the difference between 6 strokes and 17,814.
    const ctx = recorder();
    const result = paintEdges(input({ ctx }));
    expect(result.strokes).toBeLessThanOrEqual(EDGE_BUCKETS);
    expect(ctx.strokeCalls).toBe(result.strokes);
  });

  it("leaves out an edge whose node this layout did not place", () => {
    const positions = Float32Array.from([10, 10, 20, 20, NaN, NaN, 40, 40]);
    const result = paintEdges(input({ positions }));
    expect(result.drawn).toBe(2);
  });

  it("composes the fit, the pan and the zoom into one transform", () => {
    const ctx = recorder();
    paintEdges(
      input({
        ctx,
        dpr: 2,
        fit: { scale: 0.5, x: 12, y: 4 },
        view: { zoom: 3, pan: { x: 40, y: -20 } },
      }),
    );
    // A layout point p is drawn at ((p * zoom + pan) * fit.scale + fit.x) * dpr.
    expect(ctx.transform?.[0]).toBeCloseTo(2 * 0.5 * 3, 6);
    expect(ctx.transform?.[4]).toBeCloseTo(2 * (12 + 40 * 0.5), 6);
    expect(ctx.transform?.[5]).toBeCloseTo(2 * (4 + -20 * 0.5), 6);
  });
});

describe("when something is selected", () => {
  it("draws the faded edges before the lit ones", () => {
    // The paint-order contract, expressed as a draw order: a lit edge must
    // never end up underneath a faded one.
    const ctx = recorder();
    const lit = Uint8Array.from([1, 0, 1, 0]);
    paintEdges(input({ ctx, lit }));
    expect(ctx.alphas[0]).toBeCloseTo(EDGE_STYLE.fadedAlpha, 6);
    expect(ctx.alphas[ctx.alphas.length - 1]).toBeCloseTo(EDGE_STYLE.litAlpha, 6);
  });

  it("still costs a bounded number of strokes", () => {
    const lit = Uint8Array.from([1, 0, 1, 0]);
    const result = paintEdges(input({ lit }));
    expect(result.strokes).toBeLessThanOrEqual(2 * EDGE_BUCKETS);
  });

  it("draws every edge exactly once across the two passes", () => {
    const lit = Uint8Array.from([1, 0, 1, 0]);
    expect(paintEdges(input({ lit })).drawn).toBe(3);
  });
});

describe("what it refuses to do", () => {
  it("draws nothing where there is no 2D context, and does not throw", () => {
    // jsdom, and any webview that hands back null.
    expect(paintEdges(input({ ctx: null }))).toEqual({ strokes: 0, drawn: 0 });
  });

  it("draws nothing into a box with no size", () => {
    // A hidden canvas measures 0x0, and a transform built from it would be NaN.
    expect(paintEdges(input({ box: { width: 0, height: 0 } })).drawn).toBe(0);
  });

  it("paints nothing rather than a guess when the theme token is missing", () => {
    // `styles.css` states the rule for the whole graph: no `var(--x, #hex)`
    // fallbacks, because a missing token has to paint wrong. A plausible grey
    // here is how one survives.
    const ctx = recorder();
    const result = paintEdges(input({ ctx, style: { stroke: "", ...EDGE_STYLE } }));
    expect(result.drawn).toBe(0);
    expect(ctx.strokeCalls).toBe(0);
    // The old frame is still cleared, so a token that went missing shows as an
    // empty canvas rather than as a frozen one.
    expect(ctx.calls).toContain("clearRect");
  });
});
