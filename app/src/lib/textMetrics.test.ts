import { afterEach, describe, expect, it, vi } from "vitest";

import { measureText, resetTextMetrics, UI_FONT_STACK } from "./textMetrics";

/**
 * Both branches, because only one of them ever runs here.
 *
 * jsdom has no 2D context — `getContext` reports "Not implemented" and answers
 * `null` — so every other test in this suite exercises the estimate and nothing
 * exercises the measurement. A stub is the only way to see the half that runs
 * in a window, and the half that runs in a window is the one the placement
 * depends on being right.
 */

afterEach(() => {
  vi.restoreAllMocks();
  resetTextMetrics();
});

describe("measureText", () => {
  it("estimates from the character count when there is no context to ask", () => {
    // The path jsdom takes, and a webview built without a 2D context.
    resetTextMetrics();
    const short = measureText("Roma", { size: 24 });
    const long = measureText("Imperio Romano", { size: 24 });
    expect(short).toBeGreaterThan(0);
    expect(long).toBeGreaterThan(short);
    // 6.2px per character at 11px, which is what `radial.ts` measured.
    expect(short).toBeCloseTo(4 * (6.2 / 11) * 24, 6);
  });

  it("scales with the font size", () => {
    resetTextMetrics();
    const small = measureText("Concilio", { size: 12 });
    resetTextMetrics();
    const big = measureText("Concilio", { size: 24 });
    expect(big).toBeCloseTo(small * 2, 6);
  });

  it("adds the letter spacing the stylesheet asks for, which canvas ignores", () => {
    // `.region-label` sets `letter-spacing: 0.02em`, and `measureText` does not
    // apply it — so a footprint that trusted the canvas alone would be short by
    // 0.02em per gap, which on a 22-character line at 24px is 10 units.
    resetTextMetrics();
    const plain = measureText("Reforma Protestante", { size: 24 });
    resetTextMetrics();
    const tracked = measureText("Reforma Protestante", { size: 24, letterSpacing: 0.02 });
    expect(tracked - plain).toBeCloseTo(0.02 * 24 * ("Reforma Protestante".length - 1), 6);
  });

  it("prefers what the context says over the estimate", () => {
    const measure = vi.fn(() => ({ width: 321 }) as TextMetrics);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      font: "",
      measureText: measure,
    } as unknown as CanvasRenderingContext2D);
    resetTextMetrics();

    expect(measureText("Gracia", { size: 24, weight: 600 })).toBe(321);
    // The font string is what a canvas measures against, so a wrong family or
    // weight is a wrong number rather than an error.
    expect(measure).toHaveBeenCalledWith("Gracia");
  });

  it("falls back to the estimate when the context answers zero", () => {
    // A stub context answers 0 for everything, and so does a real one whose
    // font string did not parse. Zero is not a width.
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      font: "",
      measureText: () => ({ width: 0 }) as TextMetrics,
    } as unknown as CanvasRenderingContext2D);
    resetTextMetrics();

    expect(measureText("Gracia", { size: 24 })).toBeCloseTo(6 * (6.2 / 11) * 24, 6);
  });

  it("survives a context that throws", () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => {
      throw new Error("no canvas here");
    });
    resetTextMetrics();
    expect(measureText("Gracia", { size: 24 })).toBeGreaterThan(0);
  });

  it("remembers a measurement, and keys it on everything that changes one", () => {
    const measure = vi.fn(() => ({ width: 100 }) as TextMetrics);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      font: "",
      measureText: measure,
    } as unknown as CanvasRenderingContext2D);
    resetTextMetrics();

    measureText("Gracia", { size: 24 });
    measureText("Gracia", { size: 24 });
    expect(measure).toHaveBeenCalledTimes(1);
    // The solve re-runs at every zoom, so a name is measured over and over
    // with the same arguments — and at a different size it is a different
    // number that must not come back from the memo.
    measureText("Gracia", { size: 12 });
    measureText("Gracia", { size: 24, family: UI_FONT_STACK, weight: 600 });
    expect(measure).toHaveBeenCalledTimes(3);
  });
});
