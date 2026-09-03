import { describe, expect, it } from "vitest";

import { easeInOutCubic, lerpPositions, TWEEN_MS } from "./tween";

describe("the easing", () => {
  it("starts where it starts and ends where it ends", () => {
    // Catches an easing applied twice, which is the usual way this goes wrong.
    expect(easeInOutCubic(0)).toBe(0);
    expect(easeInOutCubic(1)).toBe(1);
    expect(easeInOutCubic(0.5)).toBeCloseTo(0.5, 6);
  });

  it("clamps rather than overshooting, so a late frame cannot fly past", () => {
    expect(easeInOutCubic(-0.4)).toBe(0);
    expect(easeInOutCubic(1.4)).toBe(1);
  });

  it("is slower at the ends than in the middle", () => {
    const early = easeInOutCubic(0.1) - easeInOutCubic(0);
    const middle = easeInOutCubic(0.55) - easeInOutCubic(0.45);
    expect(middle).toBeGreaterThan(early);
  });

  it("takes long enough to read and short enough not to wait for", () => {
    expect(TWEEN_MS).toBeGreaterThanOrEqual(150);
    expect(TWEEN_MS).toBeLessThanOrEqual(500);
  });
});

describe("interpolating two layouts", () => {
  const from = Float32Array.from([0, 0, 100, 100, NaN, NaN, 10, 10]);
  const to = Float32Array.from([200, 200, 300, 300, 50, 50, NaN, NaN]);

  it("is the source at nought and the destination at one", () => {
    const out = new Float32Array(to.length);
    expect(Array.from(lerpPositions(from, to, 0, out)).slice(0, 4)).toEqual([0, 0, 100, 100]);
    expect(Array.from(lerpPositions(from, to, 1, out)).slice(0, 4)).toEqual([200, 200, 300, 300]);
  });

  it("puts a node that has just appeared straight at its place", () => {
    // It was not in the previous layout, so it has nowhere to travel from.
    // Flying it in from the origin would invent a movement.
    const out = new Float32Array(to.length);
    lerpPositions(from, to, 0.5, out);
    expect(out[4]).toBe(50);
    expect(out[5]).toBe(50);
  });

  it("leaves a node that has just gone unplaced, rather than sliding it away", () => {
    const out = new Float32Array(to.length);
    lerpPositions(from, to, 0.5, out);
    expect(Number.isNaN(out[6] as number)).toBe(true);
  });

  it("is the destination outright when there is nothing to come from", () => {
    // The first layout of a library: it snaps, because there is no previous
    // picture for a reader to have been looking at.
    const out = new Float32Array(to.length);
    expect(Array.from(lerpPositions(null, to, 0.3, out))).toEqual(Array.from(to));
  });

  it("writes into the array it was given instead of allocating one", () => {
    // 12,626 nodes is a 100 KB array; one per frame is 6 MB a second of
    // garbage for a 300 ms animation.
    const out = new Float32Array(to.length);
    expect(lerpPositions(from, to, 0.5, out)).toBe(out);
  });
});
