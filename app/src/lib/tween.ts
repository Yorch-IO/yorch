/**
 * Moving from one layout to another, as arithmetic.
 *
 * The chain of atlases keeps consecutive thresholds close — 40-48 px of a
 * 1,452 px diagonal, against 162 px for independent runs — but "close" is not
 * "the same", and a node that teleports 40 px reads as a different node. So the
 * change is animated, and the animation is a function of two arrays and a
 * number, which is the only form of it a test can check: jsdom lays out no SVG
 * and would let a wrong easing pass as happily as a right one.
 *
 * The NaN rules are the interesting part, and they follow from what the arrays
 * mean. A node the source layout never placed has nowhere to travel *from*, so
 * it appears at its destination rather than flying in from the origin. A node
 * the destination does not place is leaving, so it stays unplaced and the
 * caller stops drawing it — it does not slide away to a corner it was never in.
 */

/** Long enough to read as a movement, short enough not to be a wait. */
export const TWEEN_MS = 300;

/** Slow at both ends, which is what makes it read as the picture settling
 *  rather than as a slide. */
export function easeInOutCubic(t: number): number {
  const x = Math.max(0, Math.min(1, t));
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
}

/**
 * Write the interpolated positions into `out`.
 *
 * `out` is supplied by the caller and reused across frames: a 12,626-node
 * library is a 100 KB array, and allocating one per frame is 6 MB a second of
 * garbage for a 300 ms animation.
 */
export function lerpPositions(
  from: Float32Array | null,
  to: Float32Array,
  t: number,
  out: Float32Array,
): Float32Array {
  const e = easeInOutCubic(t);
  for (let i = 0; i < to.length; i += 1) {
    const b = to[i] as number;
    if (from === null) {
      out[i] = b;
      continue;
    }
    const a = from[i] as number;
    // Arriving: nowhere to come from, so it is simply there.
    // Leaving: stays unplaced, and the caller draws nothing.
    out[i] = Number.isFinite(a) && Number.isFinite(b) ? a + (b - a) * e : b;
  }
  return out;
}
