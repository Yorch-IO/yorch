/**
 * A seeded force-directed layout, written as arithmetic so it can be tested.
 *
 * The reason this is a module and not a block inside `GraphScreen` is the one
 * `radial.ts` already records: **jsdom implements no SVG layout.** A rendered
 * test can assert that a node exists and carries the attributes it was given,
 * and nothing whatsoever about where it ended up. Positions are only checkable
 * as numbers, so they are produced by a pure function that takes nodes and
 * edges and returns points.
 *
 * **Seeded, and that is what makes it testable at all.** Initial placement comes
 * from a PRNG keyed on the node ids themselves, so the same library lays out the
 * same way on every launch: the book that was top-left is top-left tomorrow, a
 * reader's mental map survives a reload, and `force.test.ts` can assert
 * properties of the result rather than merely that it did not throw. It is still
 * force-directed — the positions are what the simulation settles into, not what
 * a formula placed — but nothing about it is random from one run to the next.
 *
 * The model is Fruchterman-Reingold: repulsion between every nearby pair,
 * attraction along every edge, and a temperature that cools so late ticks move
 * things less than early ones. Repulsion is evaluated through a uniform grid
 * rather than over all pairs, which is what makes 1,700 concepts feasible on the
 * main thread — the all-pairs form is quadratic and this library really does
 * hold 10,835 concepts when nothing is filtered.
 */

export interface ForceNode {
  id: string;
  kind: "doc" | "concept";
  /** How much the node weighs in the layout — a book with many concepts and a
   *  concept in many books both deserve more room. Used for the drawn radius
   *  and, through it, for the separation the tests assert. */
  weight: number;
  /**
   * The colour group this node belongs to, or `-1`/absent for none.
   *
   * A colour that is scattered over the whole canvas is not a grouping anyone
   * can see, so the grouping has to reach the layout: a node with a cluster is
   * pulled toward that cluster's own centre, repelled harder from the nodes of
   * other clusters and less from its own. Books are deliberately left without
   * one — a book's concepts belong to several groups, and the edges already
   * put it among them.
   */
  cluster?: number;
}

export interface ForceEdge {
  source: string;
  target: string;
  /** Mentions. A strongly-mentioned concept is pulled closer to its book. */
  weight: number;
}

export interface Point {
  x: number;
  y: number;
}

export const SIM = {
  /** The simulation's own coordinate space. `bounds()` maps the settled result
   *  onto whatever viewBox the canvas uses, so this is not a viewport. */
  W: 1600,
  H: 1200,
  /** Enough for a graph of this size to stop moving visibly. Measured by eye on
   *  the real library rather than derived; the cooling schedule below is what
   *  actually decides when it has settled. */
  TICKS: 320,
  /** `k` is scaled by this. Larger spreads the graph out. */
  SPACING: 1.05,
  /** Repulsion is ignored beyond this many `k`. Below about 2 the layout starts
   *  to fold in on itself, because a node stops seeing the cluster next to it. */
  CUTOFF: 2.2,
  /** Fraction of the smaller dimension a node may move on the first tick. */
  STEP: 0.12,
  /** Temperature decay per tick. 0.985^320 is ~0.008, so the last tenth of the
   *  run is polish rather than movement. */
  COOL: 0.985,
  /** How hard the whole graph is held together. Without it, components with no
   *  edge between them drift apart forever — and a library always has some. */
  GRAVITY: 0.012,
  /** Drawn radius, which is also the separation the collision test asserts. */
  R_MIN: 4,
  R_MAX: 16,

  // -- the cluster forces, which only apply to nodes that carry a cluster ----

  /** Pull toward the node's own cluster centroid, as a fraction of `k` — the
   *  same units the global `GRAVITY` uses, and an order of magnitude larger:
   *  gravity holds the picture together, this holds a group together.
   *
   *  Swept against the real corpus (903 concepts, threshold 3), scoring how
   *  often a node's own group is the *nearest* group — 1/10 if a colour is
   *  sprinkled over the canvas, 100% if it is a region:
   *
   *  | pull | own group nearest | pairs drawn overlapping |
   *  |---|---|---|
   *  | none | 30.8% | 588 |
   *  | 0.035 | 65.0% | 562 |
   *  | 0.08 | 88.7% | 664 |
   *  | **0.11** | **92.6%** | **568** |
   *  | 0.2 | 92.1% | 479 |
   *
   *  0.2 buys no more coherence, which is the sign that the pull has stopped
   *  doing useful work and started overruling the edges — and what places a
   *  concept *within* its group is the edges. */
  CLUSTER_PULL: 0.11,
  /** Repulsion between two nodes of *different* clusters is multiplied by
   *  this, and by `INTRA_CLUSTER` when they share one. The pair is what
   *  separates groups: raising only the first pushes the whole layout apart
   *  and gains no contrast, since every distance grows together.
   *
   *  Gentle on purpose. The same sweep at pull 0.11 gave 83.5% for a
   *  neutral 1.0/1.0 and 92.6% here, but a harder 2.2/0.6 scored *worse*
   *  (68.3% at pull 0.05 against 78.0% for 1.6/0.7): strong repulsion fights
   *  the edge attraction that carries the actual structure, and the layout
   *  ends up scrambled rather than grouped. */
  INTER_CLUSTER: 1.3,
  INTRA_CLUSTER: 0.85,
  /** Passes of overlap resolution after the run. Six is where the count of
   *  overlapping pairs stops falling on the real library; the passes are
   *  O(n) over a grid, so they cost far less than a tick. */
  COLLIDE_PASSES: 6,
  /** Room left between two node discs, on top of both radii. */
  COLLIDE_PAD: 2,
} as const;

/** mulberry32: 32 bits of state, good enough for scattering start positions and
 *  short enough to read. Nothing here is cryptographic. */
function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** FNV-1a over one id.
 *
 *  Each node is seeded from its *own* id, not from the array it arrived in, so
 *  the layout does not depend on the order the server returned things in. The
 *  index-based version worked until the response was re-sorted, at which point
 *  every node started somewhere else and the whole picture changed for a reason
 *  no reader could see. */
function hash(id: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i += 1) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** The drawn radius of a node, from its weight against the largest in its own
 *  class. Books and concepts are scaled separately: one book mentioning 300
 *  concepts would otherwise flatten every concept to the minimum. */
export function radius(weight: number, max: number): number {
  if (max <= 0) return SIM.R_MIN;
  const t = Math.sqrt(Math.max(0, Math.min(1, weight / max)));
  return SIM.R_MIN + t * (SIM.R_MAX - SIM.R_MIN);
}

export interface Layout {
  positions: Map<string, Point>;
  /** The settled extent, before any fitting. */
  box: { minX: number; minY: number; maxX: number; maxY: number };
}

/** What `separate` needs of a body, so it can live outside `settle`'s closure
 *  while still being handed `settle`'s own bodies. */
interface Disc {
  x: number;
  y: number;
  r: number;
  nudge: number;
}

/**
 * Push overlapping discs apart, repeatedly, once the simulation has settled.
 *
 * The repulsion inside the run is a *force*, so it is traded off against
 * attraction, gravity and the cluster pull, and it cools: two nodes the last
 * ticks left touching stay touching. This is the other kind — a constraint,
 * applied to positions directly, after the forces are done arguing. It is what
 * turns "mostly not overlapping" into a picture where a label has somewhere to
 * sit.
 *
 * Iterative because one pass is not a fixed point: separating a pair can push
 * one of them into a third. It stops early when a pass moves nothing, which on
 * a sparse threshold is the first one.
 */
function separate(bodies: readonly Disc[]): void {
  if (bodies.length < 2) return;
  let maxR = 0;
  for (const body of bodies) maxR = Math.max(maxR, body.r);
  const cell = Math.max(1, maxR * 2 + SIM.COLLIDE_PAD);
  const cols = Math.max(1, Math.ceil(SIM.W / cell));
  const rows = Math.max(1, Math.ceil(SIM.H / cell));
  const buckets: Disc[][] = Array.from({ length: cols * rows }, () => []);

  for (let pass = 0; pass < SIM.COLLIDE_PASSES; pass += 1) {
    for (const bucket of buckets) bucket.length = 0;
    for (const body of bodies) {
      const cx = Math.min(cols - 1, Math.max(0, Math.floor(body.x / cell)));
      const cy = Math.min(rows - 1, Math.max(0, Math.floor(body.y / cell)));
      buckets[cy * cols + cx]?.push(body);
    }

    let moved = false;
    for (let cy = 0; cy < rows; cy += 1) {
      for (let cx = 0; cx < cols; cx += 1) {
        const here = buckets[cy * cols + cx];
        if (here === undefined || here.length === 0) continue;
        // The same half-neighbourhood the repulsion walks, for the same
        // reason: every unordered pair exactly once. A cell is one diameter
        // across, so nothing closer than the pad can be further than one cell
        // away.
        for (let oy = cy; oy <= cy + 1; oy += 1) {
          if (oy >= rows) continue;
          for (let ox = cx - 1; ox <= cx + 1; ox += 1) {
            if (ox < 0 || ox >= cols) continue;
            if (oy === cy && ox < cx) continue;
            const other = buckets[oy * cols + ox];
            if (other === undefined) continue;
            const same = other === here;
            for (let ii = 0; ii < here.length; ii += 1) {
              const a = here[ii];
              if (a === undefined) continue;
              for (let jj = same ? ii + 1 : 0; jj < other.length; jj += 1) {
                const b = other[jj];
                if (b === undefined) continue;
                const want = a.r + b.r + SIM.COLLIDE_PAD;
                let ux = a.x - b.x;
                let uy = a.y - b.y;
                let d2 = ux * ux + uy * uy;
                if (d2 >= want * want) continue;
                if (d2 < 1e-6) {
                  ux = a.nudge;
                  uy = b.nudge;
                  d2 = ux * ux + uy * uy;
                }
                const d = Math.sqrt(d2) || 1e-3;
                const push = (want - d) / 2;
                const px = (ux / d) * push;
                const py = (uy / d) * push;
                a.x = Math.max(0, Math.min(SIM.W, a.x + px));
                a.y = Math.max(0, Math.min(SIM.H, a.y + py));
                b.x = Math.max(0, Math.min(SIM.W, b.x - px));
                b.y = Math.max(0, Math.min(SIM.H, b.y - py));
                moved = true;
              }
            }
          }
        }
      }
    }
    if (!moved) break;
  }
}

/**
 * Run the whole simulation and return where everything ended up.
 *
 * Synchronous and complete: the caller decides whether to show the result at
 * once or to animate towards it. Tests call this directly, which is the only
 * way to assert anything about the geometry.
 */
export function settle(
  nodes: readonly ForceNode[],
  edges: readonly ForceEdge[],
  opts: {
    ticks?: number;
    /** Where to start the bodies this map names, instead of on the seeded ring.
     *
     *  This is what makes a *ladder* of layouts possible: settle the sparsest
     *  threshold cold, then seed each denser one from the positions its
     *  predecessor found, so consecutive views of the same library are visibly
     *  related and a reader's mental map survives a change of filter. Two
     *  independent runs are not: `k` below scales with `sqrt(1/n)`, so they
     *  differ globally, and interpolating between them reads as a shuffle.
     *
     *  A node the map does not name still starts on its own seeded ring, so a
     *  partial start is fine and an absent one changes nothing. */
    start?: ReadonlyMap<string, Point>;
    /** Initial temperature as a fraction of the cold one. A warm start needs a
     *  cooler run or the first tick throws away the positions it was given —
     *  the cold temperature is 12% of the smaller dimension, which is 98px. */
    heat?: number;
  } = {},
): Layout {
  const ticks = opts.ticks ?? SIM.TICKS;
  const n = nodes.length;
  if (n === 0) {
    return { positions: new Map(), box: { minX: 0, minY: 0, maxX: 0, maxY: 0 } };
  }

  // Bodies as objects rather than parallel `Float64Array`s. `tsconfig.json` sets
  // `noUncheckedIndexedAccess`, so every `x[i]` in a kernel like this reads as
  // `number | undefined` and would need a non-null assertion — forty-odd of
  // them, through the one part of the file where the arithmetic has to stay
  // readable. Field access carries no such doubt, and at this size the
  // difference does not show: the grid below is what makes it fast, not the
  // storage.
  interface Body {
    x: number;
    y: number;
    dx: number;
    dy: number;
    /** Deterministic tiebreak for two bodies at the same point. */
    nudge: number;
    /** `-1` for a node with no group, which is every book. */
    cluster: number;
    /** The drawn radius, which is what the overlap pass separates by. */
    r: number;
  }

  const bodies: Body[] = [];
  const byId = new Map<string, Body>();
  // Sorted by id before anything is simulated. Seeding each body from its own id
  // makes the *starting* points order-independent, but not the result: forces
  // are accumulated by summing floats, and float addition is not associative, so
  // visiting the same bodies in a different order lands them tens of pixels
  // apart. Canonicalising the order here is what makes "same nodes, same
  // picture" true rather than nearly true.
  const ordered = [...nodes].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));

  // Radii are scaled within a node's own class, exactly as the drawn ones are:
  // one book mentioning 300 concepts would otherwise flatten every concept to
  // the minimum, and the overlap pass would then separate by the wrong amount.
  let maxDoc = 0;
  let maxConcept = 0;
  for (const node of ordered) {
    if (node.kind === "doc") maxDoc = Math.max(maxDoc, node.weight);
    else maxConcept = Math.max(maxConcept, node.weight);
  }

  let clusterCount = 0;
  ordered.forEach((node) => {
    // Started on a jittered ring rather than uniformly over the rectangle: a
    // uniform start leaves the middle of the graph fighting itself for the
    // first fifty ticks, and a ring unfolds outward instead. Both draws come
    // from the node's own id, so where it begins is a property of the node.
    const seed = hash(node.id);
    const random = prng(seed);
    const angle = random() * Math.PI * 2;
    const spread = 0.25 + random() * 0.75;
    const from = opts.start?.get(node.id);
    const cluster = node.cluster ?? -1;
    if (cluster >= 0) clusterCount = Math.max(clusterCount, cluster + 1);
    const body: Body = {
      x: from?.x ?? SIM.W / 2 + Math.cos(angle) * (SIM.W / 3) * spread,
      y: from?.y ?? SIM.H / 2 + Math.sin(angle) * (SIM.H / 3) * spread,
      dx: 0,
      dy: 0,
      nudge: ((seed % 7) - 3) * 0.01 + 0.005,
      cluster,
      r: radius(node.weight, node.kind === "doc" ? maxDoc : maxConcept),
    };
    bodies.push(body);
    byId.set(node.id, body);
  });

  const k = SIM.SPACING * Math.sqrt((SIM.W * SIM.H) / n);
  const cutoff = k * SIM.CUTOFF;
  const cell = cutoff;
  const cols = Math.max(1, Math.ceil(SIM.W / cell));
  const rows = Math.max(1, Math.ceil(SIM.H / cell));

  // Edges resolved to bodies once. A dangling endpoint is dropped rather than
  // throwing: the concept list and the edge list come from one response, but a
  // caller that filtered one of them should get a layout, not an exception.
  const links: { a: Body; b: Body; w: number }[] = [];
  let maxEdge = 1;
  for (const e of edges) maxEdge = Math.max(maxEdge, e.weight);
  for (const e of edges) {
    const a = byId.get(e.source);
    const b = byId.get(e.target);
    if (a === undefined || b === undefined || a === b) continue;
    links.push({ a, b, w: 0.35 + 0.65 * (e.weight / maxEdge) });
  }

  const buckets: Body[][] = Array.from({ length: cols * rows }, () => []);
  let temperature = Math.min(SIM.W, SIM.H) * SIM.STEP * (opts.heat ?? 1);

  // Where each cluster currently is. Recomputed every tick rather than fixed
  // up front: a centre chosen before the bodies move is a place none of them
  // has any reason to be, and every group would spend the run walking away
  // from it. These stay allocated across ticks and are refilled in place.
  const centroidX = new Float64Array(clusterCount);
  const centroidY = new Float64Array(clusterCount);
  const centroidN = new Int32Array(clusterCount);

  for (let tick = 0; tick < ticks; tick += 1) {
    for (const body of bodies) {
      body.dx = 0;
      body.dy = 0;
    }
    if (clusterCount > 0) {
      centroidX.fill(0);
      centroidY.fill(0);
      centroidN.fill(0);
      for (const body of bodies) {
        const c = body.cluster;
        if (c < 0) continue;
        centroidX[c] = (centroidX[c] as number) + body.x;
        centroidY[c] = (centroidY[c] as number) + body.y;
        centroidN[c] = (centroidN[c] as number) + 1;
      }
      for (let c = 0; c < clusterCount; c += 1) {
        const n = centroidN[c] as number;
        if (n === 0) continue;
        centroidX[c] = (centroidX[c] as number) / n;
        centroidY[c] = (centroidY[c] as number) / n;
      }
    }
    for (const bucket of buckets) bucket.length = 0;
    for (const body of bodies) {
      const cx = Math.min(cols - 1, Math.max(0, Math.floor(body.x / cell)));
      const cy = Math.min(rows - 1, Math.max(0, Math.floor(body.y / cell)));
      buckets[cy * cols + cx]?.push(body);
    }

    // Repulsion, over the nine cells that can hold anything within the cutoff.
    for (let cy = 0; cy < rows; cy += 1) {
      for (let cx = 0; cx < cols; cx += 1) {
        const here = buckets[cy * cols + cx];
        if (here === undefined || here.length === 0) continue;
        for (let oy = cy; oy <= cy + 1; oy += 1) {
          if (oy >= rows) continue;
          for (let ox = cx - 1; ox <= cx + 1; ox += 1) {
            if (ox < 0 || ox >= cols) continue;
            // Each unordered pair of cells visited once: the row below and, on
            // the same row, only the cell to the right.
            if (oy === cy && ox < cx) continue;
            const other = buckets[oy * cols + ox];
            if (other === undefined) continue;
            const same = other === here;
            for (let ii = 0; ii < here.length; ii += 1) {
              const a = here[ii];
              if (a === undefined) continue;
              for (let jj = same ? ii + 1 : 0; jj < other.length; jj += 1) {
                const b = other[jj];
                if (b === undefined) continue;
                let ux = a.x - b.x;
                let uy = a.y - b.y;
                let d2 = ux * ux + uy * uy;
                if (d2 > cutoff * cutoff) continue;
                if (d2 < 1e-6) {
                  // Two bodies exactly on top of each other have no direction to
                  // separate along. Nudged by a per-body constant so the result
                  // stays reproducible.
                  ux = a.nudge;
                  uy = b.nudge;
                  d2 = ux * ux + uy * uy;
                }
                const d = Math.sqrt(d2);
                // The asymmetry that makes a colour a region: two nodes of
                // different groups push each other apart harder than two of
                // the same one. A node with no group — every book — is
                // neutral to both, so books settle among whatever mentions
                // them instead of being pushed into a lane of their own.
                const scale =
                  a.cluster < 0 || b.cluster < 0
                    ? 1
                    : a.cluster === b.cluster
                      ? SIM.INTRA_CLUSTER
                      : SIM.INTER_CLUSTER;
                const force = ((k * k) / d) * scale;
                const fx = (ux / d) * force;
                const fy = (uy / d) * force;
                a.dx += fx;
                a.dy += fy;
                b.dx -= fx;
                b.dy -= fy;
              }
            }
          }
        }
      }
    }

    // Attraction along the edges.
    for (const { a, b, w } of links) {
      const ux = a.x - b.x;
      const uy = a.y - b.y;
      const d = Math.sqrt(ux * ux + uy * uy) || 1e-3;
      const force = ((d * d) / k) * w;
      const fx = (ux / d) * force;
      const fy = (uy / d) * force;
      a.dx -= fx;
      a.dy -= fy;
      b.dx += fx;
      b.dy += fy;
    }

    for (const body of bodies) {
      // Gravity, so components with no edge between them stay in one picture.
      body.dx += (SIM.W / 2 - body.x) * SIM.GRAVITY * k;
      body.dy += (SIM.H / 2 - body.y) * SIM.GRAVITY * k;

      // The same idea one level down: a group is held together by its own
      // centre the way the picture is held together by the canvas's.
      const c = body.cluster;
      if (c >= 0 && (centroidN[c] as number) > 0) {
        body.dx += ((centroidX[c] as number) - body.x) * SIM.CLUSTER_PULL * k;
        body.dy += ((centroidY[c] as number) - body.y) * SIM.CLUSTER_PULL * k;
      }

      const d = Math.sqrt(body.dx * body.dx + body.dy * body.dy) || 1e-9;
      const step = Math.min(d, temperature);
      body.x += (body.dx / d) * step;
      body.y += (body.dy / d) * step;
      // Kept in the field: a body that escapes stops being repelled by anything
      // and never comes back, which shows up as one book alone in a corner.
      body.x = Math.max(0, Math.min(SIM.W, body.x));
      body.y = Math.max(0, Math.min(SIM.H, body.y));
    }
    temperature *= SIM.COOL;
  }

  separate(bodies);

  const positions = new Map<string, Point>();
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const [id, body] of byId) {
    positions.set(id, { x: body.x, y: body.y });
    minX = Math.min(minX, body.x);
    minY = Math.min(minY, body.y);
    maxX = Math.max(maxX, body.x);
    maxY = Math.max(maxY, body.y);
  }
  return { positions, box: { minX, minY, maxX, maxY } };
}

/**
 * The transform that puts a settled layout inside a viewBox with a margin.
 *
 * Returned as numbers rather than applied, so the canvas can compose it with
 * the user's own pan and zoom instead of having two transforms fight.
 */
export function fit(
  layout: Layout,
  view: { width: number; height: number; margin?: number },
): { scale: number; x: number; y: number } {
  const margin = view.margin ?? 24;
  const { minX, minY, maxX, maxY } = layout.box;
  // An empty layout has a box of zeros, which is finite and would scale a point
  // to fill the viewport. There is nothing to fit, so nothing is transformed.
  if (layout.positions.size === 0 || !Number.isFinite(minX)) {
    return { scale: 1, x: 0, y: 0 };
  }
  // A single node has no extent; 1 keeps the division honest and centres it.
  const w = Math.max(1, maxX - minX);
  const h = Math.max(1, maxY - minY);
  const scale = Math.min(
    (view.width - margin * 2) / w,
    (view.height - margin * 2) / h,
  );
  return {
    scale,
    x: (view.width - w * scale) / 2 - minX * scale,
    y: (view.height - h * scale) / 2 - minY * scale,
  };
}
