import { scale } from "./radial";
import type { View } from "./viewport";

/**
 * The edges, drawn on a canvas instead of as SVG elements.
 *
 * **Why they left the DOM.** At the widest threshold the real library holds
 * 17,814 mentions, and one `<line>` each put the canvas past 50,000 elements in
 * a single SVG. Edges carry no interaction — no click, no hover, no accessible
 * name — so nothing is lost by painting them, and the nodes stay SVG with their
 * `aria-label`, their `role` and every assertion that already reads them.
 *
 * **Why the widths are quantised.** `lineWidth` is context state, so a width per
 * edge forces one `stroke()` per edge: 17,814 of them, which is where 10-25 ms
 * a frame goes. Six buckets means six `stroke()` calls for the whole picture,
 * and the widths are drawn from the same `scale(mentions, max, 0.6, 2.4)` the
 * SVG used, so the picture does not change — it is the same six values it
 * mostly used anyway.
 *
 * **Why the colour is read rather than written.** A canvas cannot resolve a CSS
 * custom property, so the caller passes what `getComputedStyle` gave it. If the
 * token is missing the painter draws nothing rather than substituting a
 * plausible hex — `styles.css` states that rule for the whole graph, and a
 * fallback is exactly how a missing token survives review.
 */

/** Enough to keep the weight legible, few enough to keep the strokes cheap. */
export const EDGE_BUCKETS = 6;

/** The widths the SVG used, so moving the edges to a canvas is not a redesign.
 *
 *  `scale(value, max, min, span)` in `radial.ts` takes a **span**, not an upper
 *  bound — it returns `min + (value / max) * span`. So the call the component
 *  made, `scale(e.mentions, maxEdge, 0.6, 2.4)`, draws widths from 0.6 to
 *  **3.0**, not to 2.4. Named here rather than re-derived, because reading that
 *  argument as a maximum is a mistake worth making only once. */
export const EDGE_WIDTH = { MIN: 0.6, SPAN: 2.4 } as const;
export const EDGE_WIDTH_MAX = EDGE_WIDTH.MIN + EDGE_WIDTH.SPAN;

export interface EdgeStyle {
  /** `--graph-edge`, resolved by the caller. Empty means "do not draw". */
  stroke: string;
  /** `.graph-canvas .edge { opacity: 0.45 }` with nothing selected. */
  alpha: number;
  /** `.is-probing .edge`, for the ones the selection did not light. */
  fadedAlpha: number;
  /** `.is-probing .edge.is-active`. */
  litAlpha: number;
}

export const EDGE_STYLE: Omit<EdgeStyle, "stroke"> = {
  alpha: 0.45,
  fadedAlpha: 0.1,
  litAlpha: 0.75,
};

/** Which of the `EDGE_BUCKETS` widths this edge is drawn at. */
export function edgeBucket(mentions: number, max: number, buckets = EDGE_BUCKETS): number {
  const width = scale(mentions, max, EDGE_WIDTH.MIN, EDGE_WIDTH.SPAN);
  const t = (width - EDGE_WIDTH.MIN) / EDGE_WIDTH.SPAN;
  return Math.max(0, Math.min(buckets - 1, Math.round(t * (buckets - 1))));
}

export function bucketWidth(bucket: number, buckets = EDGE_BUCKETS): number {
  if (buckets <= 1) return EDGE_WIDTH.MIN;
  return EDGE_WIDTH.MIN + (bucket / (buckets - 1)) * EDGE_WIDTH.SPAN;
}

/** A minimal 2D context, so a test can pass a recorder and jsdom's `null` is
 *  simply a context this never gets. */
export interface Ctx {
  setTransform: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
  clearRect: (x: number, y: number, w: number, h: number) => void;
  beginPath: () => void;
  moveTo: (x: number, y: number) => void;
  lineTo: (x: number, y: number) => void;
  stroke: () => void;
  strokeStyle: string;
  lineWidth: number;
  globalAlpha: number;
}

export interface PaintEdges {
  ctx: Ctx | null;
  /** The canvas's CSS box. */
  box: { width: number; height: number };
  /** Device pixels per CSS pixel, already capped by the caller. */
  dpr: number;
  /** The viewBox-to-box mapping the SVG applies. */
  fit: { scale: number; x: number; y: number };
  view: View;
  /** Indices into the edge arrays: what this threshold draws. */
  edges: Int32Array;
  edgeSrc: Int32Array;
  edgeDst: Int32Array;
  edgeMentions: Int32Array;
  maxMentions: number;
  /** Fitted layout coordinates, stride 2, `NaN` where unplaced. */
  positions: Float32Array;
  /** 1 where the node is lit by the current selection, or null when nothing is
   *  selected and every edge is drawn at the plain alpha. */
  lit: Uint8Array | null;
  style: EdgeStyle;
}

export interface PaintResult {
  /** How many `stroke()` calls the frame cost. Bounded by `2 * EDGE_BUCKETS`. */
  strokes: number;
  drawn: number;
}

/**
 * One frame.
 *
 * With a selection the faded pass is drawn first and the lit pass second, so a
 * lit edge is never underneath a faded one — the same ordering contract the SVG
 * had, expressed as a draw order instead of a document order.
 */
export function paintEdges(input: PaintEdges): PaintResult {
  const { ctx, box, dpr, fit, view, positions } = input;
  const none: PaintResult = { strokes: 0, drawn: 0 };
  // jsdom returns null from `getContext`, and a hidden canvas measures 0x0 —
  // both are ordinary, and neither may throw or write NaN into a transform.
  if (ctx === null || box.width < 1 || box.height < 1) return none;
  // A missing token paints wrong on purpose; it does not paint a guess.
  if (input.style.stroke === "") {
    ctx.clearRect(0, 0, box.width * dpr, box.height * dpr);
    return none;
  }

  const a = dpr * fit.scale * view.zoom;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, box.width * dpr, box.height * dpr);
  ctx.setTransform(
    a,
    0,
    0,
    a,
    dpr * (fit.x + view.pan.x * fit.scale),
    dpr * (fit.y + view.pan.y * fit.scale),
  );
  ctx.strokeStyle = input.style.stroke;

  // Two passes when something is selected, one when nothing is.
  const passes: { alpha: number; wants: (litEnds: boolean) => boolean }[] =
    input.lit === null
      ? [{ alpha: input.style.alpha, wants: () => true }]
      : [
          { alpha: input.style.fadedAlpha, wants: (ends) => !ends },
          { alpha: input.style.litAlpha, wants: (ends) => ends },
        ];

  let strokes = 0;
  let drawn = 0;
  for (const pass of passes) {
    ctx.globalAlpha = pass.alpha;
    for (let bucket = 0; bucket < EDGE_BUCKETS; bucket += 1) {
      let opened = false;
      for (const e of input.edges) {
        const src = input.edgeSrc[e] as number;
        const dst = input.edgeDst[e] as number;
        if (edgeBucket(input.edgeMentions[e] as number, input.maxMentions) !== bucket) continue;
        const litEnds =
          input.lit === null ||
          ((input.lit[src] as number) === 1 && (input.lit[dst] as number) === 1);
        if (!pass.wants(litEnds)) continue;
        const x1 = positions[src * 2] as number;
        const y1 = positions[src * 2 + 1] as number;
        const x2 = positions[dst * 2] as number;
        const y2 = positions[dst * 2 + 1] as number;
        if (!Number.isFinite(x1) || !Number.isFinite(x2)) continue;
        if (!opened) {
          ctx.beginPath();
          ctx.lineWidth = bucketWidth(bucket);
          opened = true;
        }
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        drawn += 1;
      }
      if (opened) {
        ctx.stroke();
        strokes += 1;
      }
    }
  }
  ctx.globalAlpha = 1;
  return { strokes, drawn };
}
