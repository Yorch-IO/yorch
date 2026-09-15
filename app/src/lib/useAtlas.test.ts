import { describe, expect, it } from "vitest";

import { chooseLayout, type AtlasLayout } from "./useAtlas";

/**
 * Which layout stands in for a threshold whose own is still being computed.
 *
 * The chain takes about six seconds on the real library and the control must
 * not wait for it, so this rule is what a reader actually sees for those
 * seconds. Its shape follows from what a layout contains: the one for threshold
 * `t` places every node of degree >= t, so it can stand in for anything at or
 * above `t` with nothing missing and nothing moved.
 */

function layout(at: number, placed: number[]): AtlasLayout {
  const xy = new Float32Array(10).fill(NaN);
  const clusters = new Int32Array(5).fill(-1);
  for (const i of placed) {
    xy[i * 2] = at * 10 + i;
    xy[i * 2 + 1] = at * 10 + i;
    // The grouping travels with the positions, so a fallback hands back both
    // or neither — never this threshold's colours over that one's picture.
    clusters[i] = i % 3;
  }
  return { xy, clusters };
}

describe("standing in for a layout that is not ready", () => {
  it("uses the threshold's own layout when it has one", () => {
    const layouts = new Map([[3, layout(3, [0, 1])], [1, layout(1, [0, 1, 2])]]);
    expect(chooseLayout(layouts, 3)).toBe(layouts.get(3));
  });

  it("falls back to a wider layout, which has a place for everything drawn", () => {
    // Only the base exists. Every node the threshold of 5 admits has degree
    // >= 5 >= 1, so the base placed it: nothing is missing and nothing moves.
    const layouts = new Map([[1, layout(1, [0, 1, 2, 3, 4])]]);
    expect(chooseLayout(layouts, 5)).toBe(layouts.get(1));
  });

  it("prefers the closest wider one, not the widest", () => {
    // Closest, because it is the one whose picture the reader is most likely
    // already looking at.
    const layouts = new Map([
      [1, layout(1, [0, 1, 2, 3, 4])],
      [2, layout(2, [0, 1, 2, 3])],
    ]);
    expect(chooseLayout(layouts, 3)).toBe(layouts.get(2));
  });

  it("takes a narrower layout only when there is nothing wider", () => {
    // A layout at 5 has no position for a node of degree 3, so this leaves
    // gaps — which is still better than an empty canvas, and they fill in as
    // the chain runs.
    const layouts = new Map([[5, layout(5, [0, 1])]]);
    expect(chooseLayout(layouts, 3)).toBe(layouts.get(5));
  });

  it("has nothing to offer before the first layout lands", () => {
    expect(chooseLayout(new Map(), 3)).toBeNull();
  });
});
