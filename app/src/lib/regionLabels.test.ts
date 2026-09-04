import { describe, expect, it } from "vitest";

import { fit, settle, type ForceEdge, type ForceNode } from "./force";
import { ATLAS_VIEW } from "./graphAtlas";
import {
  convexHull,
  placeRegionLabels,
  REGION,
  type LabelNode,
  type Point,
  type RegionLabel,
} from "./regionLabels";
import type { Box } from "./radial";
import { CONCEPT_RADIUS } from "../screens/graph/geometry";

/**
 * Where a group's name lands, which is the only place it can be checked.
 *
 * jsdom lays out no SVG, so a rendered test can prove a `<text>` exists and
 * carries the attributes it was handed, and nothing at all about whether it
 * covers a concept. `radial.test.ts` and `force.test.ts` say the same thing
 * about the two layouts. What makes these assertions possible is that the
 * layout underneath is *seeded*: the same library settles the same way every
 * time, so "no name covers a node" is a fact about the algorithm rather than
 * about one lucky run.
 *
 * **The checks are deliberately not the implementation's own.** Overlap is
 * re-derived here from distances, and "inside a cluster" from ray casting,
 * where `regionLabels.ts` decides both with separating axes. Two algorithms
 * agreeing on a property is evidence; one algorithm agreeing with itself is
 * not — the rule `graphModel.test.ts` states about re-folding the route's rows
 * rather than calling the route.
 *
 * Measured on this fixture and on the two dense ones: 467 nodes solve in 6 ms,
 * 3,067 in 2 ms and 12,867 — every concept the real envelope holds — in 5 ms,
 * with nothing falling to the crowded path in any of them.
 */

const BOOKS = 67;
const CLUSTERS = 10;

/** Ten groups that really are groups: each concept's books are drawn from its
 *  own band, so the cluster-aware simulation has something to pull apart. A
 *  uniform spread settles into one blob and would make every assertion below
 *  a statement about a blob. */
function library(concepts: number): { nodes: ForceNode[]; edges: ForceEdge[] } {
  const nodes: ForceNode[] = [];
  const edges: ForceEdge[] = [];
  for (let b = 0; b < BOOKS; b += 1) {
    nodes.push({ id: `ver_${b}`, kind: "doc", weight: 1 + (b % 9), cluster: -1 });
  }
  const band = Math.floor(BOOKS / CLUSTERS);
  for (let c = 0; c < concepts; c += 1) {
    const cluster = c % CLUSTERS;
    const degree = 2 + (c % 3);
    nodes.push({ id: `con_${c}`, kind: "concept", weight: degree, cluster });
    for (let d = 0; d < degree; d += 1) {
      const book = (cluster * band + ((c * 7 + d * 13) % Math.max(1, band))) % BOOKS;
      edges.push({ source: `ver_${book}`, target: `con_${c}`, weight: 1 + ((c + d) % 12) });
    }
  }
  return { nodes, edges };
}

/** The fixture as the screen hands it over: fitted to `ATLAS_VIEW`, and with
 *  only the concepts' radii divided by zoom — they carry `scale(1 / zoom)` on
 *  the canvas and books do not. */
function drawn(concepts = 400, zoom = 1): LabelNode[] {
  const { nodes, edges } = library(concepts);
  const layout = settle(nodes, edges);
  const t = fit(layout, {
    width: ATLAS_VIEW.W,
    height: ATLAS_VIEW.H,
    margin: ATLAS_VIEW.MARGIN,
  });
  const out: LabelNode[] = [];
  for (const n of nodes) {
    const p = layout.positions.get(n.id);
    if (p === undefined) continue;
    out.push({
      x: p.x * t.scale + t.x,
      y: p.y * t.scale + t.y,
      r: n.kind === "concept" ? CONCEPT_RADIUS / zoom : 9,
      cluster: n.cluster ?? -1,
    });
  }
  return out;
}

/** Real names off the corpus, wrapped one concept per line as the screen wraps
 *  them. The widths are the character estimate `textMetrics.ts` falls back to,
 *  because a footprint is what this function takes and where it came from is
 *  not its business. */
const NAMES: readonly (readonly string[])[] = [
  ["Dios", "Jesucristo"],
  ["Roma", "Imperio Romano"],
  ["Tomás de Aquino", "Platón"],
  ["Reforma Protestante", "Lutero"],
  ["Gracia", "Justificación"],
  ["Espíritu Santo", "Pentecostés"],
  ["Iglesia", "Sacramentos"],
  ["Escritura", "Hermenéutica"],
  ["Agustín", "Edad Media"],
  ["Pecado", "Redención"],
];

const CHAR_RATIO = 6.2 / 11;

function footprint(lines: readonly string[], zoom = 1): { w: number; h: number } {
  let w = 0;
  for (const line of lines) w = Math.max(w, line.length * CHAR_RATIO * REGION.FONT);
  return { w: (w + 4) / zoom, h: (lines.length * REGION.LINE_H + 4) / zoom };
}

function names(zoom = 1): RegionLabel[] {
  return NAMES.map((lines, cluster) => ({
    cluster,
    lines: [...lines],
    ...footprint(lines, zoom),
  }));
}

const BOUNDS: Box = { x1: 0, y1: 0, x2: ATLAS_VIEW.W, y2: ATLAS_VIEW.H };

function place(zoom = 1, concepts = 400) {
  const nodes = drawn(concepts, zoom);
  return { nodes, placed: placeRegionLabels({ nodes, labels: names(zoom), bounds: BOUNDS }) };
}

/** Distance from a point to an axis-aligned box, zero inside it. Written out
 *  rather than imported, so the assertion does not lean on the arithmetic it
 *  is checking. */
function gap(px: number, py: number, box: Box): number {
  const dx = Math.max(box.x1 - px, 0, px - box.x2);
  const dy = Math.max(box.y1 - py, 0, py - box.y2);
  return Math.hypot(dx, dy);
}

function boxesMeet(a: Box, b: Box): boolean {
  return a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;
}

/** Ray casting, which is a different algorithm from the separating axes the
 *  module decides with. */
function inPolygon(px: number, py: number, poly: readonly Point[]): boolean {
  if (poly.length < 3) return false;
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i, i += 1) {
    const a = poly[i] as Point;
    const b = poly[j] as Point;
    if (a.y > py !== b.y > py && px < ((b.x - a.x) * (py - a.y)) / (b.y - a.y) + a.x) {
      inside = !inside;
    }
  }
  return inside;
}

/** Nine points across the box: the corners, the edge midpoints and the
 *  centre. Enough to catch a box lying in a cluster, since a polygon that
 *  contains none of those and still meets the box would have to be thinner
 *  than half a label. */
function samples(box: Box): Point[] {
  const out: Point[] = [];
  for (let i = 0; i <= 2; i += 1) {
    for (let j = 0; j <= 2; j += 1) {
      out.push({
        x: box.x1 + ((box.x2 - box.x1) * i) / 2,
        y: box.y1 + ((box.y2 - box.y1) * j) / 2,
      });
    }
  }
  return out;
}

function hulls(nodes: readonly LabelNode[]): Map<number, Point[]> {
  const own = new Map<number, Point[]>();
  for (const n of nodes) {
    if (n.cluster < 0) continue;
    const list = own.get(n.cluster);
    if (list === undefined) own.set(n.cluster, [{ x: n.x, y: n.y }]);
    else list.push({ x: n.x, y: n.y });
  }
  const out = new Map<number, Point[]>();
  for (const [cluster, points] of own) out.set(cluster, convexHull(points));
  return out;
}

describe("convexHull", () => {
  it("keeps the corners and drops what is inside them", () => {
    // The check the placement's whole notion of "a cluster's ground" rests on,
    // asserted on a shape with a known answer rather than on the library.
    const hull = convexHull([
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 10, y: 10 },
      { x: 0, y: 10 },
      { x: 5, y: 5 },
      { x: 2, y: 8 },
    ]);
    expect(hull).toHaveLength(4);
    expect(inPolygon(5, 5, hull)).toBe(true);
    expect(inPolygon(11, 5, hull)).toBe(false);
  });

  it("does not lose a cluster that is a pair or a single point", () => {
    expect(convexHull([{ x: 1, y: 2 }])).toHaveLength(1);
    expect(convexHull([{ x: 1, y: 2 }, { x: 3, y: 4 }])).toHaveLength(2);
    // Three collinear points enclose nothing; the two extremes still say
    // where the cluster is, which is what `support` needs.
    expect(
      convexHull([{ x: 0, y: 0 }, { x: 1, y: 1 }, { x: 2, y: 2 }]),
    ).toHaveLength(2);
  });
});

describe("placeRegionLabels", () => {
  it("names every group that has one, and none of them crowded", () => {
    const { placed } = place();
    expect(placed).toHaveLength(NAMES.length);
    expect(placed.filter((p) => p.crowded)).toHaveLength(0);
  });

  it("leaves every drawn node clear of every name", () => {
    // Requirement: a label must never overlap a node. Asserted against the
    // node's own radius rather than the clearance the module works to, so the
    // test still means something if that constant is tuned.
    const { nodes, placed } = place();
    for (const label of placed) {
      for (const n of nodes) {
        expect(gap(n.x, n.y, label.box)).toBeGreaterThanOrEqual(n.r);
      }
    }
  });

  it("keeps two names apart", () => {
    const { placed } = place();
    for (let i = 0; i < placed.length; i += 1) {
      for (let j = i + 1; j < placed.length; j += 1) {
        expect(boxesMeet(placed[i]?.box as Box, placed[j]?.box as Box)).toBe(false);
      }
    }
  });

  it("keeps them apart when the canvas has barely room for them", () => {
    // The names off the corpus are 126 to 261 units wide, and at that size
    // this library has enough clear ground that they miss each other without
    // being told to. Swept to find where the rule starts doing work: at 350
    // units the guard is holding **4 overlapping pairs** apart, at 400 it is
    // 7, and at 500 it is 20. So the assertion is made at a width where it
    // discriminates, not at the one that ships.
    const nodes = drawn();
    const wide: RegionLabel[] = Array.from({ length: NAMES.length }, (_, cluster) => ({
      cluster,
      lines: ["Primera línea", "Segunda línea"],
      w: 350,
      h: 60,
    }));
    const placed = placeRegionLabels({ nodes, labels: wide, bounds: BOUNDS });
    expect(placed).toHaveLength(NAMES.length);
    for (let i = 0; i < placed.length; i += 1) {
      for (let j = i + 1; j < placed.length; j += 1) {
        expect(boxesMeet(placed[i]?.box as Box, placed[j]?.box as Box)).toBe(false);
      }
    }
  });

  it("puts each name outside its own group, and inside nobody else's", () => {
    // The two halves of the same rule, and they are one assertion because the
    // module makes them one: a label may not meet *any* cluster's ground,
    // starting with the one it names.
    const { nodes, placed } = place();
    const polygons = hulls(nodes);
    for (const label of placed) {
      for (const point of samples(label.box)) {
        for (const [cluster, poly] of polygons) {
          expect({ cluster, inside: inPolygon(point.x, point.y, poly) }).toEqual({
            cluster,
            inside: false,
          });
        }
      }
    }
  });

  it("never lands in the hollow of a group that has one", () => {
    // The node grid and the hulls answer two different questions, and this is
    // the one only the hulls can answer. Swept on the library fixture, the
    // hull test changes **one sample point of 450** — its clusters are dense
    // blobs, so anywhere inside one is already near a node. A cluster with a
    // sparse middle is where it earns its keep, and the screen produces those
    // whenever a filter thins a group: `hideIsolated` and `onlyBook` both
    // leave a ring of survivors around an empty centre.
    //
    // Measured on this fixture: with the rule, the inner group's name is
    // pushed 218 units and clears the ring entirely. Without it, the name sits
    // 26 units out, in the hollow, with all five sample points inside another
    // group's ground and not one node anywhere near it.
    const nodes: LabelNode[] = [];
    for (let i = 0; i < 40; i += 1) {
      const t = (i * Math.PI * 2) / 40;
      nodes.push({ x: 600 + Math.cos(t) * 200, y: 410 + Math.sin(t) * 200, r: 9, cluster: 0 });
    }
    for (let i = 0; i < 8; i += 1) {
      const t = (i * Math.PI * 2) / 8;
      nodes.push({ x: 600 + Math.cos(t) * 12, y: 410 + Math.sin(t) * 12, r: 9, cluster: 1 });
    }
    const placed = placeRegionLabels({
      nodes,
      labels: [
        { cluster: 0, lines: ["Anillo", "Exterior"], w: 200, h: 60 },
        { cluster: 1, lines: ["Centro", "Interior"], w: 200, h: 60 },
      ],
      bounds: BOUNDS,
    });
    expect(placed).toHaveLength(2);
    const ring = convexHull(
      nodes.filter((n) => n.cluster === 0).map((n) => ({ x: n.x, y: n.y })),
    );
    for (const label of placed) {
      for (const point of samples(label.box)) {
        expect(inPolygon(point.x, point.y, ring)).toBe(false);
      }
    }
  });

  it("reserves the footprint it was given, not the anchor point", () => {
    // A long name needs more clearance than a short one, and this is what says
    // the placement knows that: the box it decided is exactly the box it was
    // asked to fit, so a name measured wider is placed wider.
    const { placed } = place();
    const asked = new Map(names().map((l) => [l.cluster, l]));
    for (const label of placed) {
      const want = asked.get(label.cluster) as RegionLabel;
      expect(label.box.x2 - label.box.x1).toBeCloseTo(want.w, 6);
      expect(label.box.y2 - label.box.y1).toBeCloseTo(want.h, 6);
    }
    // And the text grows *away* from the cluster. Asserting the anchor against
    // the box it was derived from would be the implementation agreeing with
    // itself; what can actually be wrong is which side the box went to, and
    // this is the form of that: the boundary point the label was placed past
    // must lie outside the label's own box. A box that swallowed it would be a
    // name back inside the region it was moved out of.
    for (const label of placed) {
      expect(gap(label.fromX, label.fromY, label.box)).toBeGreaterThan(0);
    }
  });

  it("places every name past its own boundary and no further than the cap", () => {
    const { placed } = place();
    for (const label of placed) {
      expect(label.pushed).toBeGreaterThanOrEqual(REGION.GAP);
      expect(label.pushed).toBeLessThanOrEqual(REGION.GAP + REGION.MAX_PUSH);
    }
  });

  it("gives the same library the same names in the same places", () => {
    // A name that moved on every render would be a different picture of one
    // library each time — the rule `graphClusters.ts` states about its own
    // ordering, applied to what is drawn from it.
    const nodes = drawn();
    const once = placeRegionLabels({ nodes, labels: names(), bounds: BOUNDS });
    const twice = placeRegionLabels({ nodes, labels: names(), bounds: BOUNDS });
    expect(twice).toEqual(once);
  });

  it("holds every one of those properties when zoom halves the footprints", () => {
    // A region label carries `scale(1 / zoom)`, so its footprint in layout
    // units is `measured / zoom` and the solve is a different problem at every
    // zoom. This is that problem at 2x.
    const { nodes, placed } = place(2);
    expect(placed).toHaveLength(NAMES.length);
    expect(placed.filter((p) => p.crowded)).toHaveLength(0);
    for (const label of placed) {
      for (const n of nodes) expect(gap(n.x, n.y, label.box)).toBeGreaterThanOrEqual(n.r);
    }
    for (let i = 0; i < placed.length; i += 1) {
      for (let j = i + 1; j < placed.length; j += 1) {
        expect(boxesMeet(placed[i]?.box as Box, placed[j]?.box as Box)).toBe(false);
      }
    }
  });

  it("still names every group when there is nowhere clean to put one", () => {
    // The crowded fallback. A missing name is worse than a crowded one, so
    // nothing is ever dropped — and the flag is what lets the caller paint the
    // biggest group last, which is the only case two names can still meet.
    const nodes: LabelNode[] = [];
    for (let i = 0; i < 400; i += 1) {
      nodes.push({
        x: 10 + (i % 20) * 5,
        y: 10 + Math.floor(i / 20) * 5,
        r: 6,
        cluster: i % 3,
      });
    }
    const labels: RegionLabel[] = [0, 1, 2].map((cluster) => ({
      cluster,
      lines: ["Nombre larguísimo", "Segunda línea"],
      w: 300,
      h: 60,
    }));
    const placed = placeRegionLabels({
      nodes,
      labels,
      bounds: { x1: 0, y1: 0, x2: 130, y2: 130 },
    });
    expect(placed).toHaveLength(3);
    expect(placed.every((p) => p.crowded)).toBe(true);
    expect(placed.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y))).toBe(true);
  });

  it("has nothing to say about a group with no label", () => {
    const nodes = drawn();
    expect(placeRegionLabels({ nodes, labels: [], bounds: BOUNDS })).toEqual([]);
    // A label naming a cluster nothing drawn belongs to is dropped rather than
    // placed at the origin: `-1` means "this control has nothing to say about
    // that node" everywhere else in this codebase, and an absent cluster is
    // the same answer.
    expect(
      placeRegionLabels({
        nodes,
        labels: [{ cluster: 99, lines: ["Nadie"], w: 60, h: 30 }],
        bounds: BOUNDS,
      }),
    ).toEqual([]);
  });
});
