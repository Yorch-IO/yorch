import { fit, settle, type ForceEdge, type ForceNode, type Point } from "./force";
import { clusterConcepts, CLUSTER_TARGET } from "./graphClusters";
import { DEFAULT_THRESHOLD, THRESHOLDS } from "./graphModel";

/**
 * Where every node sits, at every threshold, computed off the main thread.
 *
 * **Why this is a ladder and not five independent layouts.** `settle`'s
 * `k = SPACING * sqrt(W*H/n)` (`force.ts`) scales with the node count, so two
 * runs over different subsets of one library are globally different pictures.
 * Measured on the real degree distribution (73 books, 12,832 concepts), fitting
 * both to the same canvas: **a threshold change moved every surviving node
 * 162 px on average — 11% of the canvas diagonal.** That is a shuffle, not a
 * filter, and no amount of easing hides it. Aligning the two layouts with the
 * best rotation and scale only got it to 110 px: they differ structurally.
 *
 * That is why this used to be a *ladder*: the default threshold settled cold,
 * the widest one seeded from it, and every other one relaxed from that base in
 * 60 ticks — which held a threshold change to 40-48 px instead of 162.
 *
 * **The ladder was removed when the layout became cluster-aware, and the
 * measurement is why.** Each threshold clusters its own subgraph (see
 * `forceInput`), so a relaxation now starts from positions organised around
 * *different* groups and spends its 60 ticks fighting them. Measured on the
 * real library, scoring how often a node's own group is the nearest group:
 *
 * | threshold | settled cold | relaxed from the base |
 * |---|---|---|
 * | 5 | 97.9% | 83.6% |
 * | 3 (the default) | **92.6%** | **54.6%** |
 * | 2 | 77.5% | 46.9% |
 *
 * And it no longer bought what it was built for: seeded, a move from threshold
 * 3 to 2 shifts a surviving node 157 px, against 231 px cold — both far past
 * the 40-48 px the ladder existed to hold, and both on the order of the 162 px
 * its own note calls a shuffle rather than a filter. So it was costing half the
 * grouping to turn one shuffle into a slightly smaller shuffle. Every layout is
 * now settled cold, and the tween animates the change.
 *
 * Ordered by cost, so everything except "every book" is ready early. Measured:
 *
 * | | measured |
 * |---|---|
 * | the default threshold — the first picture | 364 ms |
 * | thresholds 5 and 10 | 23 ms and 7 ms |
 * | threshold 2 | ~1 s |
 * | the widest, which CLAUDE.md calls a texture rather than a picture | ~8.8 s |
 *
 * A per-layout `fit` is deliberate, and was measured under the ladder too: a
 * single fit shared by all five made it *worse* (214 px against 162), because
 * the surviving nodes occupy a different fraction of the field at every
 * threshold.
 */

/** The coordinate space every layout is fitted into. Pan and zoom compose on
 *  top, so this is not a viewport. */
export const ATLAS_VIEW = { W: 1200, H: 820, MARGIN: 40 } as const;

/** The widest threshold — every node of the envelope, and the only layout that
 *  has a position for all of them. Computed last: it is the most expensive by
 *  an order of magnitude and the least often looked at. */
export const BASE_THRESHOLD = Math.min(...THRESHOLDS);

/** Everything the worker needs, in transferable form. No `Map`, no object per
 *  node: the whole point of `graphModel`'s typed arrays is that this crosses a
 *  thread boundary cheaply. */
export interface AtlasRequest {
  job: number;
  ids: string[];
  docCount: number;
  degree: Int32Array;
  edgeSrc: Int32Array;
  edgeDst: Int32Array;
  edgeMentions: Int32Array;
}

export type AtlasMessage =
  | {
      kind: "layout";
      job: number;
      threshold: number;
      /** Stride 2, length `2 * ids.length`, already fitted to `ATLAS_VIEW`.
       *  `NaN` at any node this threshold does not place. */
      xy: Float32Array;
      /** The cluster this layout separated each node into, by node index;
       *  `-1` for a book and for anything this threshold does not draw. It
       *  travels *with* the positions so the colours on screen are the same
       *  grouping the simulation pulled apart, including while the chain is
       *  still running and a threshold is drawn with a wider one's layout. */
      clusters: Int32Array;
      done: number;
      total: number;
      ms: number;
    }
  | { kind: "done"; job: number }
  | { kind: "failed"; job: number; message: string };

/** The order the layouts are computed in: the default first because it is the
 *  picture a reader sees, then by rising cost, so only "every book" is still
 *  missing after about a second and a half. */
const ORDER = [DEFAULT_THRESHOLD, ...THRESHOLDS.filter((k) => k !== DEFAULT_THRESHOLD)].sort(
  (a, b) => (a === DEFAULT_THRESHOLD ? -1 : b === DEFAULT_THRESHOLD ? 1 : b - a),
);

/** One emit per threshold. */
export const ATLAS_STEPS = ORDER.length;

/**
 * The nodes and edges one threshold draws, each concept carrying the cluster
 * it belongs to.
 *
 * **Clustered here rather than in the screen, and per threshold rather than
 * once.** Here, because the layout is what has to separate the groups — a
 * colour scattered across the canvas is not a grouping anybody can see — and
 * the screen then reads the same assignment back off the message, so the
 * legend and the picture cannot disagree. Per threshold, because the
 * alternative was measured: reusing the widest threshold's clustering at the
 * default one gives groups of 224, 163, 113, 100, 94, 87, 50, 38, 34 against
 * 110, 103, 100, 97, 97, 93, 90, 78, 74, 61 for its own. Stable colours across
 * a slider move are worth less than a legend whose groups are comparable.
 */
function forceInput(
  req: AtlasRequest,
  threshold: number,
): { nodes: ForceNode[]; edges: ForceEdge[]; clusters: Int32Array } {
  const nodeIndices: number[] = [];
  for (let i = 0; i < req.ids.length; i += 1) {
    if (i >= req.docCount && (req.degree[i] as number) < threshold) continue;
    nodeIndices.push(i);
  }
  const edgeIndices: number[] = [];
  for (let e = 0; e < req.edgeSrc.length; e += 1) {
    if ((req.degree[req.edgeDst[e] as number] as number) < threshold) continue;
    edgeIndices.push(e);
  }

  const { cluster } = clusterConcepts(
    req,
    { nodes: Int32Array.from(nodeIndices), edges: Int32Array.from(edgeIndices) },
    CLUSTER_TARGET,
  );

  const nodes: ForceNode[] = [];
  for (const i of nodeIndices) {
    const doc = i < req.docCount;
    nodes.push({
      id: req.ids[i] as string,
      kind: doc ? "doc" : "concept",
      weight: doc ? 1 : (req.degree[i] as number),
      // Books carry none: their concepts belong to several groups, and the
      // edges already put a book among the ones that mention it.
      cluster: doc ? -1 : (cluster[i] as number),
    });
  }
  const edges: ForceEdge[] = [];
  for (const e of edgeIndices) {
    edges.push({
      source: req.ids[req.edgeSrc[e] as number] as string,
      target: req.ids[req.edgeDst[e] as number] as string,
      weight: req.edgeMentions[e] as number,
    });
  }
  return { nodes, edges, clusters: cluster };
}

/** Fitted coordinates for every node, `NaN` where this threshold placed none. */
function scatter(req: AtlasRequest, positions: ReadonlyMap<string, Point>): Float32Array {
  const t = fit(
    { positions: positions as Map<string, Point>, box: boxOf(positions) },
    { width: ATLAS_VIEW.W, height: ATLAS_VIEW.H, margin: ATLAS_VIEW.MARGIN },
  );
  const xy = new Float32Array(req.ids.length * 2).fill(NaN);
  for (let i = 0; i < req.ids.length; i += 1) {
    const p = positions.get(req.ids[i] as string);
    if (p === undefined) continue;
    xy[i * 2] = p.x * t.scale + t.x;
    xy[i * 2 + 1] = p.y * t.scale + t.y;
  }
  return xy;
}

function boxOf(positions: ReadonlyMap<string, Point>) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of positions.values()) {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  return { minX, minY, maxX, maxY };
}

/**
 * The chain, as a generator.
 *
 * A generator rather than a loop with a callback because the two hosts drive it
 * differently: the worker runs it to completion, and the host that has no
 * worker steps it with a `setTimeout` between layouts so the window can still
 * repaint. One implementation, one set of assertions.
 */
export function* atlasSteps(req: AtlasRequest): Generator<AtlasMessage> {
  let done = 0;
  const emit = (
    threshold: number,
    positions: ReadonlyMap<string, Point>,
    clusters: Int32Array,
    ms: number,
  ): AtlasMessage => {
    done += 1;
    return {
      kind: "layout",
      job: req.job,
      threshold,
      xy: scatter(req, positions),
      clusters,
      done,
      total: ATLAS_STEPS,
      ms,
    };
  };

  // Each threshold settled on its own, in the order named above. Nothing is
  // seeded from anything — see the header for the measurement that removed the
  // ladder — so no layout is a stand-in for another and none is ever
  // superseded, which is why the message carries no `provisional` flag.
  for (const threshold of ORDER) {
    const input = forceInput(req, threshold);
    const t0 = Date.now();
    const layout = settle(input.nodes, input.edges);
    yield emit(threshold, layout.positions, input.clusters, Date.now() - t0);
  }

  yield { kind: "done", job: req.job };
}

export interface AtlasJob {
  cancel: () => void;
}

/**
 * Off the main thread where that is possible, on it where it is not.
 *
 * jsdom defines no `Worker`, so the whole test suite takes the inline path and
 * `atlasSteps` is exercised for real by every assertion in
 * `graphAtlas.test.ts`. A webview too old for module workers throws in the
 * constructor and lands in the same place. The worst case is the behaviour that
 * shipped before any of this — a synchronous `settle` — staged between timeouts
 * and reporting progress, never a blank canvas.
 */
export function startAtlas(
  req: AtlasRequest,
  onMessage: (message: AtlasMessage) => void,
): AtlasJob {
  if (typeof Worker !== "undefined") {
    try {
      // Written as one expression on purpose: this literal form is what a
      // bundler recognises. Assigning the URL to a variable first emits no
      // worker chunk, which works in dev and 404s in a built app.
      const worker = new Worker(new URL("./graphAtlas.worker.ts", import.meta.url), {
        type: "module",
      });
      worker.onmessage = (event: MessageEvent<AtlasMessage>) => {
        onMessage(event.data);
        if (event.data.kind !== "layout") worker.terminate();
      };
      worker.onerror = (event) => {
        onMessage({ kind: "failed", job: req.job, message: String(event.message ?? event.type) });
        worker.terminate();
      };
      worker.postMessage(req);
      return {
        // `terminate` rather than a cooperative flag: a `settle` of the whole
        // library runs for seconds and cannot observe a message while it does,
        // so a cancel it only notices between layouts would waste all of it.
        cancel: () => worker.terminate(),
      };
    } catch {
      // Fall through to the inline path.
    }
  }
  return runInline(req, onMessage);
}

function runInline(req: AtlasRequest, onMessage: (message: AtlasMessage) => void): AtlasJob {
  const steps = atlasSteps(req);
  let stopped = false;
  const pump = () => {
    if (stopped) return;
    let next: IteratorResult<AtlasMessage>;
    try {
      next = steps.next();
    } catch (error) {
      onMessage({ kind: "failed", job: req.job, message: String(error) });
      return;
    }
    if (next.done === true) return;
    onMessage(next.value);
    if (next.value.kind !== "layout") return;
    // A turn of the loop between layouts, so a host with no worker still
    // repaints rather than freezing for the whole chain.
    setTimeout(pump, 0);
  };
  setTimeout(pump, 0);
  return { cancel: () => { stopped = true; } };
}
