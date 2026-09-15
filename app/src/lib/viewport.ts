import { GRAPH_ZOOM } from "../screens/graph/geometry";

/**
 * The arithmetic of looking at the canvas, kept out of the component so it can
 * be asserted.
 *
 * This is the same decision `radial.ts` and `force.ts` record about geometry:
 * jsdom lays out no SVG, implements neither `getScreenCTM` nor
 * `createSVGPoint`, and returns zeros from `getBoundingClientRect`. A zoom that
 * reads the pointer through the SVG's own matrix would therefore be
 * untestable — so the mapping is done here, from numbers a test can supply.
 *
 * **Two shipped defects live in this file's absence.**
 *
 * The first: pan is applied inside a `<g>` whose coordinates are viewBox units,
 * and it was being fed raw client pixels. The canvas is about 630 CSS px wide
 * for a 1,200-unit viewBox at the narrowest two-column width the layout allows,
 * so one pixel of pointer travel moved the graph 1.9 units. `panBy` divides by
 * the scale, which is what makes a drag track the pointer.
 *
 * The second: the wheel ignored the pointer entirely and zoomed about the
 * centre, so zooming in on something meant zooming and then panning it back.
 * `zoomAt` keeps the point under the cursor where it is.
 */

export interface View {
  zoom: number;
  pan: { x: number; y: number };
}

export interface Box {
  width: number;
  height: number;
}

/**
 * The scale and offset an SVG applies to its viewBox.
 *
 * `preserveAspectRatio` defaults to `xMidYMid meet`, and the overview's
 * `max-height` does bite on a wide window, so the viewBox is letterboxed rather
 * than stretched. Anything mapping a client point into graph coordinates has to
 * reproduce that or it is wrong by the width of the bands.
 */
export function fitBox(box: Box, view: Box): { scale: number; x: number; y: number } {
  if (box.width <= 0 || box.height <= 0) return { scale: 1, x: 0, y: 0 };
  const scale = Math.min(box.width / view.width, box.height / view.height);
  return {
    scale,
    x: (box.width - view.width * scale) / 2,
    y: (box.height - view.height * scale) / 2,
  };
}

/** A point in the element's own client rectangle, in viewBox units. */
export function clientToView(
  client: { x: number; y: number },
  rect: { left: number; top: number; width: number; height: number },
  view: Box,
): { x: number; y: number } {
  const fit = fitBox({ width: rect.width, height: rect.height }, view);
  if (fit.scale === 0) return { x: 0, y: 0 };
  return {
    x: (client.x - rect.left - fit.x) / fit.scale,
    y: (client.y - rect.top - fit.y) / fit.scale,
  };
}

export function clampZoom(zoom: number): number {
  return Math.min(GRAPH_ZOOM.MAX, Math.max(GRAPH_ZOOM.MIN, zoom));
}

/**
 * Zoom about a point, leaving whatever is under it where it is.
 *
 * The graph is drawn as `translate(pan) scale(zoom)`, so a point `u` in layout
 * space appears at `u * zoom + pan`. Holding that fixed while the zoom changes
 * gives the new pan directly.
 */
export function zoomAt(view: View, at: { x: number; y: number }, factor: number): View {
  const zoom = clampZoom(view.zoom * factor);
  const u = { x: (at.x - view.pan.x) / view.zoom, y: (at.y - view.pan.y) / view.zoom };
  return { zoom, pan: { x: at.x - u.x * zoom, y: at.y - u.y * zoom } };
}

/**
 * Move the graph by a pointer displacement measured in client pixels.
 *
 * The division is the whole point: `pan` lives inside the scaled group, so a
 * drag of N screen pixels is N / (fit * zoom) units there.
 */
export function panBy(
  view: View,
  from: { x: number; y: number },
  origin: { x: number; y: number; pan: { x: number; y: number } },
  rect: { width: number; height: number },
  viewBox: Box,
): { x: number; y: number } {
  const fit = fitBox({ width: rect.width, height: rect.height }, viewBox);
  const scale = fit.scale * view.zoom;
  if (scale === 0) return origin.pan;
  return {
    x: origin.pan.x + (from.x - origin.x) / scale,
    y: origin.pan.y + (from.y - origin.y) / scale,
  };
}
