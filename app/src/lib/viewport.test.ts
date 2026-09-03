import { describe, expect, it } from "vitest";

import { GRAPH_ZOOM } from "../screens/graph/geometry";
import { clampZoom, clientToView, fitBox, panBy, zoomAt, type View } from "./viewport";

/**
 * The two shipped defects this file exists to fix, asserted as numbers.
 *
 * Neither could be caught by a rendered test: jsdom returns zeros from
 * `getBoundingClientRect` and implements no SVG geometry at all, so a component
 * test can prove a `transform` attribute exists and nothing about whether it
 * puts the graph where the pointer asked.
 */

const VIEW = { width: 1200, height: 820 };

describe("fitting a viewBox into the box it is drawn in", () => {
  it("letterboxes rather than stretching, as the SVG itself does", () => {
    // `preserveAspectRatio` defaults to `xMidYMid meet` and the overview's
    // `max-height` does bite on a wide window, so the bands are real.
    const fit = fitBox({ width: 1600, height: 660 }, VIEW);
    expect(fit.scale).toBeCloseTo(660 / 820, 6);
    expect(fit.x).toBeGreaterThan(0);
    expect(fit.y).toBeCloseTo(0, 6);
  });

  it("survives a box with no size, which is what jsdom reports", () => {
    const fit = fitBox({ width: 0, height: 0 }, VIEW);
    expect(fit).toEqual({ scale: 1, x: 0, y: 0 });
    expect(Number.isFinite(clientToView({ x: 5, y: 5 }, { left: 0, top: 0, width: 0, height: 0 }, VIEW).x)).toBe(true);
  });

  it("maps a client point back through the letterbox", () => {
    const rect = { left: 20, top: 10, width: 1600, height: 660 };
    const fit = fitBox({ width: rect.width, height: rect.height }, VIEW);
    // The centre of the canvas is the centre of the viewBox.
    const centre = clientToView(
      { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
      rect,
      VIEW,
    );
    expect(centre.x).toBeCloseTo(VIEW.width / 2, 4);
    expect(centre.y).toBeCloseTo(VIEW.height / 2, 4);
    expect(fit.scale).toBeLessThan(1);
  });
});

describe("zooming towards the pointer", () => {
  it("leaves whatever is under the pointer where it is", () => {
    // The property the wheel never had: it zoomed about the centre, so zooming
    // in on something meant zooming and then panning it back.
    const before = { zoom: 1, pan: { x: 0, y: 0 } };
    const at = { x: 900, y: 200 };
    const after = zoomAt(before, at, GRAPH_ZOOM.STEP);

    const under = (v: typeof before) => ({
      x: (at.x - v.pan.x) / v.zoom,
      y: (at.y - v.pan.y) / v.zoom,
    });
    expect(under(after).x).toBeCloseTo(under(before).x, 6);
    expect(under(after).y).toBeCloseTo(under(before).y, 6);
  });

  it("still holds the point after several steps from an off-centre pan", () => {
    let view: View = { zoom: 1.7, pan: { x: -240, y: 88 } };
    const at = { x: 310, y: 640 };
    const before = { x: (at.x - view.pan.x) / view.zoom, y: (at.y - view.pan.y) / view.zoom };
    for (const f of [GRAPH_ZOOM.STEP, GRAPH_ZOOM.STEP, 1 / GRAPH_ZOOM.STEP]) {
      view = zoomAt(view, at, f);
    }
    expect((at.x - view.pan.x) / view.zoom).toBeCloseTo(before.x, 4);
    expect((at.y - view.pan.y) / view.zoom).toBeCloseTo(before.y, 4);
  });

  it("clamps at the bounds the buttons already used", () => {
    expect(clampZoom(99)).toBe(GRAPH_ZOOM.MAX);
    expect(clampZoom(0.001)).toBe(GRAPH_ZOOM.MIN);
    let view: View = { zoom: GRAPH_ZOOM.MAX, pan: { x: 0, y: 0 } };
    view = zoomAt(view, { x: 600, y: 410 }, GRAPH_ZOOM.STEP);
    expect(view.zoom).toBe(GRAPH_ZOOM.MAX);
  });
});

describe("panning", () => {
  it("moves the graph as far as the pointer went, not further", () => {
    // The defect: `pan` is added inside a group whose units are viewBox units,
    // and it was fed client pixels. At the narrowest two-column width the
    // canvas is ~630 CSS px for a 1,200-unit viewBox, so a drag ran 1.9x fast.
    const rect = { width: 630, height: 430 };
    const fit = fitBox(rect, VIEW);
    const view = { zoom: 1, pan: { x: 0, y: 0 } };
    const moved = panBy(
      view,
      { x: 163, y: 100 },
      { x: 63, y: 100, pan: { x: 0, y: 0 } },
      rect,
      VIEW,
    );
    // 100 pixels of pointer travel is 100 / fit units of graph.
    expect(moved.x).toBeCloseTo(100 / fit.scale, 4);
    expect(moved.y).toBeCloseTo(0, 6);
    // And the un-divided version, which is what shipped, would be 1.9x this.
    expect(moved.x / 100).toBeGreaterThan(1.5);
  });

  it("tracks the pointer at any zoom", () => {
    const rect = { width: 1200, height: 820 };
    for (const zoom of [0.5, 1, 3, 6]) {
      const moved = panBy(
        { zoom, pan: { x: 0, y: 0 } },
        { x: 90, y: 40 },
        { x: 0, y: 0, pan: { x: 0, y: 0 } },
        rect,
        VIEW,
      );
      // At fit 1, the drawn displacement is pan * zoom, which must equal the
      // pointer's own.
      expect(moved.x * zoom).toBeCloseTo(90, 6);
      expect(moved.y * zoom).toBeCloseTo(40, 6);
    }
  });
});
