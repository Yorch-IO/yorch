import { overlaps, type Box } from "./radial";

/**
 * Where a cluster's name goes: outside the cluster, as close to its boundary as
 * a clear patch allows.
 *
 * **The defect this replaces.** The name was written across the centroid of the
 * cluster's own drawn nodes, under them and behind a halo. That is a mitigation,
 * not a placement: the words stayed interleaved with the dots they describe, and
 * at the old 44px a two-concept name is about 540 user units against a
 * 1200-unit canvas — 45% of the width — so it reached into the neighbouring
 * region as well. Halving the type and wrapping it onto two lines is what makes
 * the requirement satisfiable at all; ten labels of 540 units cannot be placed
 * outside ten clusters by any algorithm.
 *
 * **Pure, for the reason `force.ts` and `radial.ts` give about themselves.**
 * jsdom lays out no SVG — no `getBBox`, no resolved transform — so a rendered
 * test can prove a `<text>` exists and nothing about where it landed. Collisions
 * are a property of these numbers, and `regionLabels.test.ts` asserts them
 * directly.
 *
 * **On the main thread rather than in the atlas worker**, unlike the clustering
 * it names. The footprint of a label in layout units is `measured / zoom`,
 * because a region label carries `scale(1 / zoom)` to hold a constant on-screen
 * size — and the worker knows neither the zoom nor which nodes the screen's own
 * checkboxes are drawing. Pan needs no re-solve: it is a pure translation of
 * everything at once.
 *
 * **What "another cluster's area" means here.** Two tests, because one of them
 * alone is wrong in a way the other catches. The node grid answers "is this
 * patch occupied", which is also what intruding on another group amounts to —
 * being among its concepts. The convex hulls answer the case the grid cannot
 * see: the sparse interior of a spread-out group, where a label would sit in
 * clear air and still read as belonging to the wrong region. Measured on a
 * library of dense blobs the hull test changes **one sample point of 450** —
 * anywhere inside such a cluster is already near a node — so it earns its keep
 * only once a filter thins a group, which `hideIsolated` and `onlyBook` both
 * do. `regionLabels.test.ts` names the fixture that separates them.
 *
 * **Cost, measured on 10 labels at the default threshold: 6 ms at 467 nodes,
 * 2 ms at 3,067 and 5 ms at 12,867** — every concept the real envelope holds.
 * It does not grow with the library because each ray stops at its first clear
 * distance and the grid keeps every query local; what it grows with is the
 * number of names, which is `CLUSTER_TARGET`. That is the budget it has to
 * stay inside: the solve re-runs on every zoom step.
 */

export const REGION = {
  /** Must match `.graph-canvas .region-label` in `styles.css`, the same
   *  contract `radial.ts`'s `LABEL.FONT` carries. */
  FONT: 24,
  /** Baseline to baseline for the wrapped second line. */
  LINE_H: 28,
  /** Clear air between the cluster's supporting line and the label's near
   *  edge. Below about 10 the name reads as a node's own label rather than as
   *  the region's. */
  GAP: 14,
  /** How close a label may come to any node of any cluster. */
  CLEARANCE: 10,
  /** Between two labels, on top of their own boxes. */
  LABEL_GAP: 8,
  /** How far each attempt moves outward. Finer than this buys sub-pixel
   *  placement nobody can see and costs a step per ray. */
  STEP: 12,
  /** The furthest a label may be pushed past its own boundary before the
   *  crowded fallback takes over. */
  MAX_PUSH: 260,
  /** Directions tried per cluster: 7.5 degrees apart. */
  RAYS: 48,
  /** Past this much push the label gets a leader line back to the boundary,
   *  because colour alone stops carrying the association. */
  LEADER_AFTER: 48,
  /** Occupancy grid cell. Roughly a concept's diameter at zoom 1. */
  CELL: 24,
  /** How much the outward tie-break may move a score. Strictly below `STEP`,
   *  so it orders candidates that tied on distance and can never beat a
   *  closer one. */
  TIE: 4,
} as const;

/** What the crowded fallback charges for each kind of collision. A label lying
 *  over another label is unreadable; a label over a node hides data; a label in
 *  the wrong region is merely misleading. Ordered accordingly. */
const COST = { LABEL: 40, HULL: 8, NODE: 1, BOUNDS: 4, PUSH: 0.01 } as const;

export type Anchor = "start" | "middle" | "end";

/** A drawn node, in layout units. The caller divides by zoom where the element
 *  cancels it: concepts carry `scale(1 / zoom)` and books do not. */
export interface LabelNode {
  x: number;
  y: number;
  r: number;
  /** `-1` for a book and for an unclustered concept — the same meaning `-1`
   *  carries everywhere else in this codebase. */
  cluster: number;
}

/** One name to place. `w` and `h` are the measured footprint in layout units,
 *  already divided by zoom — and `h / lines.length` is the leading the caller
 *  renders the second line at, so the box that is fitted is the box that
 *  paints. */
export interface RegionLabel {
  cluster: number;
  lines: string[];
  w: number;
  h: number;
}

export interface RegionLabelRequest {
  nodes: readonly LabelNode[];
  labels: readonly RegionLabel[];
  /** Where labels may go: the whole viewBox, gutter included. */
  bounds: Box;
}

export interface PlacedRegionLabel {
  cluster: number;
  lines: string[];
  /** The `<text>` anchor: the first line's vertical centre. */
  x: number;
  y: number;
  anchor: Anchor;
  /** The point on the cluster's boundary this label belongs to, for the
   *  leader line. */
  fromX: number;
  fromY: number;
  /** How far past that boundary it had to go. */
  pushed: number;
  /** Nothing valid was found and the least-bad candidate was taken. A name is
   *  never dropped: a missing one is worse than a crowded one. */
  crowded: boolean;
  /** The footprint that was tested. Returned so a test asserts the geometry
   *  that was actually decided rather than its own re-derivation of it. */
  box: Box;
}

export interface Point {
  x: number;
  y: number;
}

/** An edge's outward normal and the polygon's own extent along it, precomputed
 *  so a box test is one closed-form projection per edge. */
interface Axis {
  nx: number;
  ny: number;
  min: number;
  max: number;
}

interface Hull {
  cluster: number;
  cx: number;
  cy: number;
  size: number;
  points: Point[];
  box: Box;
  axes: Axis[];
}

/**
 * Convex hull, monotone chain.
 *
 * Deterministic like everything else that decides where a thing is drawn: the
 * points are sorted before the sweep, so the same cluster produces the same
 * polygon on every render. Under three points there is no polygon — the caller
 * falls back to the node grid, which covers a pair or a singleton exactly.
 */
export function convexHull(points: Point[]): Point[] {
  if (points.length < 3) return points.slice();
  const sorted = points.slice().sort((a, b) => (a.x !== b.x ? a.x - b.x : a.y - b.y));
  const cross = (o: Point, a: Point, b: Point) =>
    (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  const half = (input: Point[]): Point[] => {
    const out: Point[] = [];
    for (const p of input) {
      while (
        out.length >= 2 &&
        cross(out[out.length - 2] as Point, out[out.length - 1] as Point, p) <= 0
      ) {
        out.pop();
      }
      out.push(p);
    }
    out.pop();
    return out;
  };
  const lower = half(sorted);
  const upper = half(sorted.slice().reverse());
  const hull = lower.concat(upper);
  // Every point collinear collapses both halves; the pair of extremes is still
  // a truthful description of where the cluster is.
  return hull.length >= 3 ? hull : [sorted[0] as Point, sorted[sorted.length - 1] as Point];
}

function hullOf(cluster: number, own: Point[]): Hull {
  let sx = 0;
  let sy = 0;
  for (const p of own) {
    sx += p.x;
    sy += p.y;
  }
  const cx = sx / own.length;
  const cy = sy / own.length;
  const points = convexHull(own);

  let x1 = Infinity;
  let y1 = Infinity;
  let x2 = -Infinity;
  let y2 = -Infinity;
  for (const p of points) {
    if (p.x < x1) x1 = p.x;
    if (p.y < y1) y1 = p.y;
    if (p.x > x2) x2 = p.x;
    if (p.y > y2) y2 = p.y;
  }

  const axes: Axis[] = [];
  if (points.length >= 3) {
    for (let i = 0; i < points.length; i += 1) {
      const a = points[i] as Point;
      const b = points[(i + 1) % points.length] as Point;
      const nx = -(b.y - a.y);
      const ny = b.x - a.x;
      let min = Infinity;
      let max = -Infinity;
      for (const p of points) {
        const d = p.x * nx + p.y * ny;
        if (d < min) min = d;
        if (d > max) max = d;
      }
      axes.push({ nx, ny, min, max });
    }
  }

  return { cluster, cx, cy, size: own.length, points, box: { x1, y1, x2, y2 }, axes };
}

/**
 * How far the cluster reaches in a direction — the convex support function,
 * over the hull's vertices rather than every node.
 *
 * This is the "boundary" the label is placed beyond, and it is deliberately the
 * *supporting line* rather than where the ray leaves the polygon: a box built
 * past the supporting line is outside the hull whatever its width, where one
 * built past the ray's exit point can swing back into a lobe of an elongated
 * cluster.
 */
function support(hull: Hull, ux: number, uy: number): number {
  let best = 0;
  for (const p of hull.points) {
    const d = (p.x - hull.cx) * ux + (p.y - hull.cy) * uy;
    if (d > best) best = d;
  }
  return best;
}

/** Separating axes: the box's own two (the bounding-box test) plus the
 *  polygon's edge normals. Exact for a convex polygon against an axis-aligned
 *  rectangle. */
function boxHitsHull(box: Box, hull: Hull): boolean {
  if (hull.axes.length === 0) return false;
  if (box.x2 <= hull.box.x1 || hull.box.x2 <= box.x1) return false;
  if (box.y2 <= hull.box.y1 || hull.box.y2 <= box.y1) return false;
  for (const axis of hull.axes) {
    // The box is axis-aligned, so its extent along any normal is the corner
    // each sign picks out — no loop over four corners.
    const lo =
      axis.nx * (axis.nx > 0 ? box.x1 : box.x2) + axis.ny * (axis.ny > 0 ? box.y1 : box.y2);
    const hi =
      axis.nx * (axis.nx > 0 ? box.x2 : box.x1) + axis.ny * (axis.ny > 0 ? box.y2 : box.y1);
    if (hi <= axis.min || axis.max <= lo) return false;
  }
  return true;
}

interface Grid {
  cell: number;
  cols: number;
  rows: number;
  x1: number;
  y1: number;
  buckets: number[][];
  reach: number;
}

/** The same structure `force.ts` uses for repulsion, for the same reason: the
 *  question is always "what is near this patch", and answering it by scanning
 *  every node would make the solve quadratic in the library. */
function buildGrid(nodes: readonly LabelNode[], bounds: Box): Grid {
  let x1 = bounds.x1;
  let y1 = bounds.y1;
  let x2 = bounds.x2;
  let y2 = bounds.y2;
  let reach = 0;
  for (const n of nodes) {
    if (n.x < x1) x1 = n.x;
    if (n.y < y1) y1 = n.y;
    if (n.x > x2) x2 = n.x;
    if (n.y > y2) y2 = n.y;
    if (n.r > reach) reach = n.r;
  }
  const cell = REGION.CELL;
  const cols = Math.max(1, Math.ceil((x2 - x1) / cell) + 1);
  const rows = Math.max(1, Math.ceil((y2 - y1) / cell) + 1);
  const buckets: number[][] = Array.from({ length: cols * rows }, () => []);
  nodes.forEach((n, i) => {
    const cxi = Math.min(cols - 1, Math.max(0, Math.floor((n.x - x1) / cell)));
    const cyi = Math.min(rows - 1, Math.max(0, Math.floor((n.y - y1) / cell)));
    (buckets[cyi * cols + cxi] as number[]).push(i);
  });
  return { cell, cols, rows, x1, y1, buckets, reach };
}

/** Squared distance from a point to an axis-aligned box, zero inside it. */
function distanceToBox(px: number, py: number, box: Box): number {
  const dx = Math.max(box.x1 - px, 0, px - box.x2);
  const dy = Math.max(box.y1 - py, 0, py - box.y2);
  return dx * dx + dy * dy;
}

/** How many nodes come within `clearance` of the box. Stops at the first one
 *  unless the caller is scoring rather than accepting. */
function nodesTouching(
  grid: Grid,
  nodes: readonly LabelNode[],
  box: Box,
  clearance: number,
  stopAtFirst: boolean,
): number {
  const pad = clearance + grid.reach;
  const c0 = Math.max(0, Math.floor((box.x1 - pad - grid.x1) / grid.cell));
  const c1 = Math.min(grid.cols - 1, Math.floor((box.x2 + pad - grid.x1) / grid.cell));
  const r0 = Math.max(0, Math.floor((box.y1 - pad - grid.y1) / grid.cell));
  const r1 = Math.min(grid.rows - 1, Math.floor((box.y2 + pad - grid.y1) / grid.cell));
  let hits = 0;
  for (let r = r0; r <= r1; r += 1) {
    for (let c = c0; c <= c1; c += 1) {
      for (const i of grid.buckets[r * grid.cols + c] as number[]) {
        const n = nodes[i] as LabelNode;
        const limit = n.r + clearance;
        if (distanceToBox(n.x, n.y, box) < limit * limit) {
          hits += 1;
          if (stopAtFirst) return hits;
        }
      }
    }
  }
  return hits;
}

/** The footprint of a label whose near edge touches `(px, py)` on the far side
 *  of the direction it was placed along. */
function boxAt(
  px: number,
  py: number,
  uy: number,
  anchor: Anchor,
  w: number,
  h: number,
): Box {
  const x1 = anchor === "start" ? px : anchor === "end" ? px - w : px - w / 2;
  // Sideways placements straddle the ray; vertical ones hang off it, or the
  // box would cover the boundary it was measured from.
  const y1 = anchor === "middle" ? (uy < 0 ? py - h : py) : py - h / 2;
  return { x1, y1, x2: x1 + w, y2: y1 + h };
}

function grow(box: Box, by: number): Box {
  return { x1: box.x1 - by, y1: box.y1 - by, x2: box.x2 + by, y2: box.y2 + by };
}

function inside(box: Box, bounds: Box): boolean {
  return box.x1 >= bounds.x1 && box.y1 >= bounds.y1 && box.x2 <= bounds.x2 && box.y2 <= bounds.y2;
}

interface Candidate {
  box: Box;
  anchor: Anchor;
  fromX: number;
  fromY: number;
  pushed: number;
  score: number;
}

/** One attempt: the ray, the distance past the boundary, and the box that
 *  puts. Written once because both passes below need exactly this and a
 *  second copy of it would be free to drift from the first. */
interface Attempt {
  box: Box;
  anchor: Anchor;
  fromX: number;
  fromY: number;
  /** How far this ray points away from the middle of the canvas, in `[0, 1]`.
   *  Only ever a tie-break. */
  outward: number;
}

function attemptAt(
  hull: Hull,
  label: RegionLabel,
  ray: number,
  d: number,
  awayX: number,
  awayY: number,
): Attempt {
  const theta = (ray * Math.PI * 2) / REGION.RAYS;
  const ux = Math.cos(theta);
  const uy = Math.sin(theta);
  const reach = support(hull, ux, uy);
  const anchor: Anchor = ux > 0.35 ? "start" : ux < -0.35 ? "end" : "middle";
  return {
    box: boxAt(hull.cx + ux * (reach + d), hull.cy + uy * (reach + d), uy, anchor, label.w, label.h),
    anchor,
    fromX: hull.cx + ux * reach,
    fromY: hull.cy + uy * reach,
    outward: (1 + (ux * awayX + uy * awayY)) / 2,
  };
}

/**
 * Every name placed, in the order they were solved — largest cluster first, so
 * the big regions get the best ground. The caller decides paint order for
 * itself; these two orders are separate decisions and were separate before this
 * function existed.
 */
export function placeRegionLabels(req: RegionLabelRequest): PlacedRegionLabel[] {
  const { nodes, labels, bounds } = req;
  if (labels.length === 0) return [];

  const own = new Map<number, Point[]>();
  for (const n of nodes) {
    if (n.cluster < 0) continue;
    const list = own.get(n.cluster);
    if (list === undefined) own.set(n.cluster, [{ x: n.x, y: n.y }]);
    else list.push({ x: n.x, y: n.y });
  }
  const hulls = new Map<number, Hull>();
  for (const [cluster, points] of own) hulls.set(cluster, hullOf(cluster, points));
  const allHulls = [...hulls.values()];
  const grid = buildGrid(nodes, bounds);

  const midX = (bounds.x1 + bounds.x2) / 2;
  const midY = (bounds.y1 + bounds.y2) / 2;

  const order = labels
    .slice()
    .sort((a, b) => {
      const sa = hulls.get(a.cluster)?.size ?? 0;
      const sb = hulls.get(b.cluster)?.size ?? 0;
      return sb !== sa ? sb - sa : a.cluster - b.cluster;
    });

  const taken: Box[] = [];
  const out: PlacedRegionLabel[] = [];

  for (const label of order) {
    const hull = hulls.get(label.cluster);
    if (hull === undefined) continue;

    // Which way is "away from the crowd" for this cluster, used only to break
    // ties between rays that found room at the same distance.
    let awayX = hull.cx - midX;
    let awayY = hull.cy - midY;
    const awayLen = Math.hypot(awayX, awayY);
    if (awayLen > 1e-6) {
      awayX /= awayLen;
      awayY /= awayLen;
    } else {
      awayX = 0;
      awayY = 0;
    }

    let best: Candidate | null = null;

    for (let ray = 0; ray < REGION.RAYS; ray += 1) {
      for (let d = REGION.GAP; d <= REGION.GAP + REGION.MAX_PUSH; d += REGION.STEP) {
        const at = attemptAt(hull, label, ray, d, awayX, awayY);

        // The box translates linearly along the ray, so a bound it has crossed
        // stays crossed: nothing further out on this ray can help.
        if (!inside(at.box, bounds)) break;

        // Cheapest discriminating test first. The hulls carry a bounding box
        // and reject on it, the placed labels are at most nine, and the node
        // grid is the expensive one.
        if (allHulls.some((other) => boxHitsHull(at.box, other))) continue;
        const padded = grow(at.box, REGION.LABEL_GAP);
        if (taken.some((t) => overlaps(padded, t))) continue;
        if (nodesTouching(grid, nodes, at.box, REGION.CLEARANCE, true) > 0) continue;

        const score = d + REGION.TIE * (1 - at.outward);
        if (best === null || score < best.score) best = { ...at, pushed: d, score };
        // Further out on this ray is strictly worse.
        break;
      }
    }

    const chosen =
      best ?? leastBad(label, hull, allHulls, taken, grid, nodes, bounds, awayX, awayY);
    if (chosen === null) continue;

    const lineH = label.h / Math.max(1, label.lines.length);
    taken.push(chosen.box);
    out.push({
      cluster: label.cluster,
      lines: label.lines,
      x:
        chosen.anchor === "start"
          ? chosen.box.x1
          : chosen.anchor === "end"
            ? chosen.box.x2
            : (chosen.box.x1 + chosen.box.x2) / 2,
      y: chosen.box.y1 + lineH / 2,
      anchor: chosen.anchor,
      fromX: chosen.fromX,
      fromY: chosen.fromY,
      pushed: chosen.pushed,
      crowded: best === null,
      box: chosen.box,
    });
  }

  return out;
}

/**
 * The whole ray fan again, scored rather than accepted.
 *
 * Only reached when no clean patch exists anywhere around a cluster, which on
 * the real library happens to nothing — so this pays for the node count it
 * skips on the fast path exactly when the fast path has already failed.
 */
function leastBad(
  label: RegionLabel,
  hull: Hull,
  allHulls: readonly Hull[],
  taken: readonly Box[],
  grid: Grid,
  nodes: readonly LabelNode[],
  bounds: Box,
  awayX: number,
  awayY: number,
): Candidate | null {
  let best: Candidate | null = null;
  for (let ray = 0; ray < REGION.RAYS; ray += 1) {
    for (let d = REGION.GAP; d <= REGION.GAP + REGION.MAX_PUSH; d += REGION.STEP) {
      const at = attemptAt(hull, label, ray, d, awayX, awayY);

      let penalty = (d + REGION.TIE * (1 - at.outward)) * COST.PUSH;
      if (!inside(at.box, bounds)) penalty += COST.BOUNDS;
      for (const other of allHulls) {
        if (boxHitsHull(at.box, other)) penalty += COST.HULL;
      }
      const padded = grow(at.box, REGION.LABEL_GAP);
      for (const t of taken) {
        if (overlaps(padded, t)) penalty += COST.LABEL;
      }
      penalty += nodesTouching(grid, nodes, at.box, REGION.CLEARANCE, false) * COST.NODE;

      if (best === null || penalty < best.score) best = { ...at, pushed: d, score: penalty };
    }
  }
  return best;
}
