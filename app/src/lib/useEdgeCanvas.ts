import { useCallback, useEffect, useRef, useState } from "react";

import { ATLAS_VIEW } from "./graphAtlas";
import { EDGE_STYLE, paintEdges, type Ctx } from "./graphPainter";
import type { GraphIndex, Subgraph } from "./graphModel";
import { fitBox, type View } from "./viewport";

/**
 * The plumbing between the canvas element and the painter.
 *
 * Everything decidable lives in `graphPainter.ts` and is tested there against a
 * recording context; this file is the part that has to touch the DOM — the
 * backing store, the box, the theme tokens and the resize — and it is written
 * so that each of those failing is ordinary rather than fatal.
 */

/** Two device pixels per CSS pixel is enough for a hairline. At 3 the backing
 *  store for a 1200x820 box is 8.9 megapixels, and clearing plus stroking it
 *  costs three times as much for nothing anyone can see. */
const MAX_DPR = 2;

/** The canvas cannot resolve a custom property, so it is read here.
 *
 *  Empty when the token is missing, which the painter renders as "draw
 *  nothing" — `styles.css` forbids `var(--x, #hex)` fallbacks across the whole
 *  graph, because a missing token has to paint wrong rather than plausibly. */
function readToken(name: string): string {
  if (typeof window === "undefined" || typeof getComputedStyle !== "function") return "";
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  } catch {
    return "";
  }
}

/** Re-read when the theme changes, so switching it does not leave the canvas
 *  painting last theme's grey until something else forces a repaint. */
function useThemeToken(name: string): string {
  const [value, setValue] = useState(() => readToken(name));
  useEffect(() => {
    setValue(readToken(name));
    if (typeof matchMedia !== "function") return;
    const query = matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setValue(readToken(name));
    // Guarded: older WebKit exposes only the deprecated `addListener`.
    query.addEventListener?.("change", onChange);
    return () => query.removeEventListener?.("change", onChange);
  }, [name]);
  return value;
}

/** The canvas's CSS box, tracked so the backing store can match it.
 *
 *  `ResizeObserver` is absent from jsdom and from older webviews, so a `resize`
 *  listener stands in; and both graph views stay mounted inside `hidden`
 *  wrappers, which measure 0x0 — an observer notices when one becomes visible
 *  and a one-shot measurement never would. A zero box is not an error: the
 *  painter draws nothing into it. */
function useBox(element: HTMLElement | null): { width: number; height: number } {
  const [box, setBox] = useState({ width: 0, height: 0 });
  useEffect(() => {
    if (element === null) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      setBox((was) =>
        was.width === rect.width && was.height === rect.height
          ? was
          : { width: rect.width, height: rect.height },
      );
    };
    measure();
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(measure);
      observer.observe(element);
      return () => observer.disconnect();
    }
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [element]);
  return box;
}

export interface EdgeCanvas {
  /** Repaint at a view the caller holds imperatively, optionally at positions
   *  it holds imperatively too.
   *
   *  This is what lets a drag avoid React entirely: the gesture writes one
   *  `transform` on the SVG group and calls this, and neither costs a render.
   *  The positions override is the same story for the layout tween, whose
   *  intermediate frames exist only in a scratch array — without it the edges
   *  would snap to the destination while the nodes were still travelling.
   *  Stable across renders, so an effect may depend on it. */
  repaint: (view: View, positions?: Float32Array) => void;
}

export function useEdgeCanvas(opts: {
  canvas: HTMLCanvasElement | null;
  stage: HTMLElement | null;
  index: GraphIndex | null;
  sub: Subgraph | null;
  positions: Float32Array | null;
  lit: Uint8Array | null;
  view: View;
}): EdgeCanvas {
  const { canvas, stage, index, sub, positions, lit, view } = opts;
  const stroke = useThemeToken("--graph-edge");
  const box = useBox(stage);

  // The inputs the painter needs, read at call time rather than captured, so
  // `repaint` never goes stale and never changes identity.
  const latest = useRef({ canvas, index, sub, positions, lit, box, stroke });
  latest.current = { canvas, index, sub, positions, lit, box, stroke };

  const repaint = useCallback((at: View, override?: Float32Array) => {
    const now = latest.current;
    if (now.canvas === null || now.index === null || now.sub === null || now.positions === null) {
      return;
    }
    const xy = override ?? now.positions;
    const dpr = Math.min(typeof devicePixelRatio === "number" ? devicePixelRatio : 1, MAX_DPR);
    // Rounded: a fractional display scale otherwise leaves the canvas a
    // subpixel off the SVG, and every line lands beside its own nodes.
    const width = Math.round(now.box.width * dpr);
    const height = Math.round(now.box.height * dpr);
    if (now.canvas.width !== width) now.canvas.width = width;
    if (now.canvas.height !== height) now.canvas.height = height;

    // jsdom returns null here, and so does a webview with no 2D context.
    const ctx = now.canvas.getContext("2d") as Ctx | null;
    paintEdges({
      ctx,
      box: now.box,
      dpr,
      fit: fitBox(now.box, { width: ATLAS_VIEW.W, height: ATLAS_VIEW.H }),
      view: at,
      edges: now.sub.edges,
      edgeSrc: now.index.edgeSrc,
      edgeDst: now.index.edgeDst,
      edgeMentions: now.index.edgeMentions,
      maxMentions: now.index.maxEdgeMentions,
      positions: xy,
      lit: now.lit,
      style: { stroke: now.stroke, ...EDGE_STYLE },
    });
  }, []);

  // Everything that is not a gesture still goes through React, and lands here.
  useEffect(() => {
    repaint(view);
  }, [repaint, view, canvas, index, sub, positions, lit, box, stroke]);

  return { repaint };
}
