import { describe, expect, it } from "vitest";

import { fit, radius, settle, SIM, type ForceEdge, type ForceNode } from "./force";

/**
 * The layout's *geometry*, which is the only place it can be checked.
 *
 * jsdom implements no SVG layout — no `getBBox`, no resolved `transform` — so a
 * rendered test can prove a circle exists and nothing at all about where it is.
 * `radial.test.ts` says the same thing about the document view's rings. What
 * makes these assertions possible for a force-directed layout is that this one
 * is *seeded*: the same nodes settle the same way every time, so "no two nodes
 * overlap" is a fact about the algorithm rather than about one lucky run.
 *
 * The counts are the ones that ship: 67 books and 1,719 concepts is the real
 * library at the default `min_documents = 2`, measured 2026-08-24.
 */

const BOOKS = 67;
const CONCEPTS = 400;

function library(
  books = BOOKS,
  concepts = CONCEPTS,
): { nodes: ForceNode[]; edges: ForceEdge[] } {
  const nodes: ForceNode[] = [];
  const edges: ForceEdge[] = [];
  for (let b = 0; b < books; b += 1) {
    nodes.push({ id: `ver_${b}`, kind: "doc", weight: 1 + (b % 9) });
  }
  for (let c = 0; c < concepts; c += 1) {
    // Each concept in two to four books, deterministically — the shape the
    // degree filter produces, where every concept joins at least two.
    const degree = 2 + (c % 3);
    nodes.push({ id: `con_${c}`, kind: "concept", weight: degree });
    for (let d = 0; d < degree; d += 1) {
      edges.push({
        source: `ver_${(c * 7 + d * 13) % books}`,
        target: `con_${c}`,
        weight: 1 + ((c + d) % 12),
      });
    }
  }
  return { nodes, edges };
}

describe("the simulation is seeded", () => {
  it("gives identical positions for identical input", () => {
    // The property every other test in this file rests on, and the reason the
    // book that was top-left is top-left after a reload.
    const { nodes, edges } = library(12, 40);
    const a = settle(nodes, edges, { ticks: 60 });
    const b = settle(nodes, edges, { ticks: 60 });
    for (const [id, point] of a.positions) {
      expect(b.positions.get(id)).toEqual(point);
    }
  });

  it("does not depend on the order the server returned the nodes in", () => {
    // A re-fetch at a different threshold returns the survivors in whatever
    // order the ORDER BY produced. Seeding on the sorted ids means the nodes it
    // kept do not all move because one was dropped from the middle.
    const { nodes, edges } = library(12, 40);
    const shuffled = [...nodes].reverse();
    const a = settle(nodes, edges, { ticks: 60 });
    const b = settle(shuffled, edges, { ticks: 60 });
    for (const [id, point] of a.positions) {
      expect(b.positions.get(id)?.x).toBeCloseTo(point.x, 6);
    }
  });
});

describe("the settled layout", () => {
  const { nodes, edges } = library();
  const layout = settle(nodes, edges);

  it("places every node it was given", () => {
    expect(layout.positions.size).toBe(nodes.length);
  });

  it("keeps every node inside the field", () => {
    for (const [id, p] of layout.positions) {
      expect(Number.isFinite(p.x), id).toBe(true);
      expect(Number.isFinite(p.y), id).toBe(true);
      expect(p.x, id).toBeGreaterThanOrEqual(0);
      expect(p.x, id).toBeLessThanOrEqual(SIM.W);
      expect(p.y, id).toBeGreaterThanOrEqual(0);
      expect(p.y, id).toBeLessThanOrEqual(SIM.H);
    }
  });

  it("leaves no two nodes on top of each other", () => {
    // The failure this rules out is not cosmetic: two circles at one point are
    // one target for a click, so one of the two nodes cannot be selected at all.
    const points = [...layout.positions.entries()];
    let closest = Infinity;
    for (let i = 0; i < points.length; i += 1) {
      for (let j = i + 1; j < points.length; j += 1) {
        const a = points[i];
        const b = points[j];
        if (a === undefined || b === undefined) continue;
        const d = Math.hypot(a[1].x - b[1].x, a[1].y - b[1].y);
        closest = Math.min(closest, d);
      }
    }
    expect(closest).toBeGreaterThan(SIM.R_MAX);
  });

  it("pulls connected nodes closer than unconnected ones", () => {
    // What makes it a graph rather than a scatter plot. Compared as means,
    // because any individual pair can land anywhere.
    const linked: number[] = [];
    for (const e of edges) {
      const a = layout.positions.get(e.source);
      const b = layout.positions.get(e.target);
      if (a && b) linked.push(Math.hypot(a.x - b.x, a.y - b.y));
    }
    const all: number[] = [];
    const points = [...layout.positions.values()];
    for (let i = 0; i < points.length; i += 17) {
      for (let j = i + 1; j < points.length; j += 23) {
        const a = points[i];
        const b = points[j];
        if (a && b) all.push(Math.hypot(a.x - b.x, a.y - b.y));
      }
    }
    const mean = (xs: number[]) => xs.reduce((s, v) => s + v, 0) / xs.length;
    expect(mean(linked)).toBeLessThan(mean(all));
  });

  it("uses the field rather than collapsing into a knot", () => {
    // The failure mode of too much gravity, and the one that makes every label
    // unreadable at any zoom.
    const { minX, minY, maxX, maxY } = layout.box;
    expect(maxX - minX).toBeGreaterThan(SIM.W * 0.4);
    expect(maxY - minY).toBeGreaterThan(SIM.H * 0.4);
  });
});

describe("degenerate graphs", () => {
  it("returns nothing for no nodes, rather than NaN", () => {
    const layout = settle([], []);
    expect(layout.positions.size).toBe(0);
    expect(fit(layout, { width: 800, height: 600 })).toEqual({
      scale: 1,
      x: 0,
      y: 0,
    });
  });

  it("places a single node without dividing by its own extent", () => {
    const layout = settle([{ id: "ver_1", kind: "doc", weight: 1 }], []);
    const p = layout.positions.get("ver_1");
    expect(Number.isFinite(p?.x)).toBe(true);
    expect(Number.isFinite(p?.y)).toBe(true);
    const t = fit(layout, { width: 800, height: 600 });
    expect(Number.isFinite(t.scale)).toBe(true);
    expect(Number.isFinite(t.x)).toBe(true);
  });

  it("ignores an edge naming a node that is not in the graph", () => {
    // The concept list and the edge list arrive together, but a caller that
    // filtered one of them should get a layout rather than an exception.
    const layout = settle(
      [{ id: "ver_1", kind: "doc", weight: 1 }],
      [{ source: "ver_1", target: "con_missing", weight: 3 }],
      { ticks: 10 },
    );
    expect(layout.positions.size).toBe(1);
  });

  it("separates two nodes that start at the same point", () => {
    const layout = settle(
      [
        { id: "a", kind: "doc", weight: 1 },
        { id: "b", kind: "doc", weight: 1 },
      ],
      [],
      { ticks: 40 },
    );
    const a = layout.positions.get("a");
    const b = layout.positions.get("b");
    expect(Math.hypot((a?.x ?? 0) - (b?.x ?? 0), (a?.y ?? 0) - (b?.y ?? 0)))
      .toBeGreaterThan(SIM.R_MAX);
  });
});

describe("fit", () => {
  it("puts the whole layout inside the viewport with its margin", () => {
    const { nodes, edges } = library(20, 60);
    const layout = settle(nodes, edges, { ticks: 80 });
    const view = { width: 800, height: 600, margin: 24 };
    const t = fit(layout, view);
    for (const p of layout.positions.values()) {
      const x = p.x * t.scale + t.x;
      const y = p.y * t.scale + t.y;
      expect(x).toBeGreaterThanOrEqual(view.margin - 1);
      expect(x).toBeLessThanOrEqual(view.width - view.margin + 1);
      expect(y).toBeGreaterThanOrEqual(view.margin - 1);
      expect(y).toBeLessThanOrEqual(view.height - view.margin + 1);
    }
  });
});

describe("radius", () => {
  it("spans the declared range and never leaves it", () => {
    expect(radius(0, 100)).toBe(SIM.R_MIN);
    expect(radius(100, 100)).toBe(SIM.R_MAX);
    expect(radius(150, 100)).toBe(SIM.R_MAX);
    expect(radius(-5, 100)).toBe(SIM.R_MIN);
  });

  it("does not divide by a maximum of zero", () => {
    // A library where every concept is mentioned once has a max of zero only if
    // something upstream is wrong, but the layout must still draw.
    expect(radius(0, 0)).toBe(SIM.R_MIN);
  });
});

/** Not an assertion about speed — a guard that the real library's shape does not
 *  make the layout hang the window. The numbers are the measured ones: 67 books
 *  and 1,719 concepts at the default threshold, 10,835 with no threshold. */
function graph(books: number, concepts: number, degree = 3) {
  const nodes: ForceNode[] = [];
  const edges: ForceEdge[] = [];
  for (let b = 0; b < books; b += 1) {
    nodes.push({ id: `ver_${b}`, kind: "doc", weight: 20 });
  }
  for (let c = 0; c < concepts; c += 1) {
    nodes.push({ id: `con_${c}`, kind: "concept", weight: degree });
    for (let d = 0; d < degree; d += 1) {
      edges.push({
        source: `ver_${(c * 7 + d * 13) % books}`,
        target: `con_${c}`,
        weight: 1 + (c % 9),
      });
    }
  }
  return { nodes, edges };
}

describe("the real library's shape", () => {
  it("settles the default view in a time a window can absorb", () => {
    const { nodes, edges } = graph(67, 1719);
    const started = Date.now();
    const layout = settle(nodes, edges);
    const elapsed = Date.now() - started;
    expect(layout.positions.size).toBe(nodes.length);
    // Generous on purpose: what this rules out is the quadratic version, which
    // takes minutes at this size, not a slow machine.
    expect(elapsed).toBeLessThan(20_000);
  });
});
