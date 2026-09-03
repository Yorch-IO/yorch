import { useLayoutEffect, useRef } from "react";

import { lerpPositions, TWEEN_MS } from "./tween";

/**
 * Animate the nodes from the layout they were at to the layout they are at now.
 *
 * **`useLayoutEffect`, not `useEffect`.** React has already rendered the new
 * coordinates by the time an effect runs, and a passive effect runs *after* the
 * browser paints — so the picture would jump to the destination, then animate
 * back from where it used to be. Running before paint means the first thing
 * anyone sees is the start of the movement.
 *
 * **Written by hand rather than as a CSS transition.** A transition on
 * `transform` would be shorter code and would animate the wrong thing: a node
 * that was not in the previous layout has no start value, so it would fly in
 * from the origin, and a node that has left would slide to a corner it was
 * never at. Those two cases are the rules in `tween.ts`, and CSS has no way to
 * express them.
 *
 * The elements are found once per animation, not once per frame, and the whole
 * loop is attribute writes on nodes React is not re-rendering — so a change of
 * threshold costs one commit and then twenty frames of `setAttribute`.
 */
export function useLayoutTween(opts: {
  /** The panned group; every node under it carries `data-node`. */
  group: SVGGElement | null;
  positions: Float32Array | null;
  /** Concepts cancel the zoom on themselves, so their transform carries it. */
  zoom: number;
  /** Where the concepts begin in index space. */
  docCount: number;
  /** Called per frame with the interpolated array, so the edges move with the
   *  nodes rather than snapping ahead of them. */
  onFrame?: (positions: Float32Array) => void;
}): void {
  const { group, positions, zoom, docCount, onFrame } = opts;
  const previous = useRef<Float32Array | null>(null);
  const frame = useRef(0);
  const scratch = useRef<Float32Array | null>(null);
  // Read at call time so the loop never captures a stale zoom or callback.
  const latest = useRef({ zoom, docCount, onFrame });
  latest.current = { zoom, docCount, onFrame };

  useLayoutEffect(() => {
    if (positions === null || group === null) return;
    const from = previous.current;
    previous.current = positions;

    // Nothing to move from — the first layout of a library snaps, because there
    // is no picture a reader was looking at.
    if (from === null || from.length !== positions.length) return;

    const nodes = Array.from(group.querySelectorAll<SVGGElement>("[data-node]"));
    if (nodes.length === 0) return;
    if (scratch.current === null || scratch.current.length !== positions.length) {
      scratch.current = new Float32Array(positions.length);
    }
    const out = scratch.current;

    const started =
      typeof performance === "object" && typeof performance.now === "function"
        ? performance.now()
        : Date.now();

    const step = () => {
      const now =
        typeof performance === "object" && typeof performance.now === "function"
          ? performance.now()
          : Date.now();
      const t = Math.min(1, (now - started) / TWEEN_MS);
      lerpPositions(from, positions, t, out);

      const { zoom: z, docCount: docs, onFrame: paint } = latest.current;
      for (const node of nodes) {
        const i = Number(node.dataset.node);
        const x = out[i * 2] as number;
        const y = out[i * 2 + 1] as number;
        if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
        node.setAttribute(
          "transform",
          i < docs ? `translate(${x} ${y})` : `translate(${x} ${y}) scale(${1 / z})`,
        );
      }
      paint?.(out);

      if (t < 1) {
        frame.current = requestAnimationFrame(step);
        return;
      }
      frame.current = 0;
    };

    frame.current = requestAnimationFrame(step);
    return () => {
      if (frame.current !== 0) {
        cancelAnimationFrame(frame.current);
        frame.current = 0;
      }
    };
  }, [group, positions]);
}
