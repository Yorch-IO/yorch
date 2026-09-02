/**
 * Concepts grouped into a handful of coloured clusters, by which books connect
 * them.
 *
 * **Why not `conceptType`.** It reads as the obvious grouping and is not one:
 * measured on the running corpus, it is free text from the extractor with
 * **1,619 distinct values** after folding case, accents and underscores alone
 * — "concepto teológico", "concepto filosófico" and "concepto religioso" are
 * three names for one idea a human would merge on sight, and nothing here can
 * do that automatically. Structure needs no curation: two concepts that appear
 * in many of the same books are related whatever either is called, and every
 * book already reports exactly that through its `MENTIONS` edges.
 *
 * **Why the bipartite graph, and not the concept-concept projection.** The
 * first version of this built one weighted pair per co-occurring concept pair
 * and clustered that. It is the textbook move and it does not survive this
 * corpus: measured on `lib_teologia`, the projection is 214,879 pairs at the
 * default threshold and **7,296,901 at "every book"** — 2.6 s to build and
 * 3.2 s to cluster, synchronously, inside a render. That is the same
 * whole-second freeze the library graph was rewritten to remove. The bipartite
 * graph carries the same relation in **18,779 edges**, because a book with
 * *k* concepts is *k* edges here and *k²/2* pairs there. Clustering it with
 * books left in as connectors costs **23 ms at that same widest threshold**,
 * and the books are dropped afterwards — they are the join, not a colour.
 *
 * **Louvain for the cores, a capped merge for the sizes.** Modularity alone
 * answers "which concepts belong together" well and "how many groups, of what
 * size" not at all: at resolution 1 this corpus is one community of 903, and
 * at a resolution high enough to split it, 61 of the 117 communities are
 * single concepts. Merging alone has the opposite failure — an unbounded
 * weight-ordered merge put 893 of 903 concepts in one cluster and left nine of
 * one or two, because a dense library is not several separable regions but one
 * mass almost everything co-occurs in. So: Louvain at `RESOLUTION` for cores
 * that mean something, then merge cores by how strongly they are connected
 * until `targetClusters` remain, refusing any merge that would take a cluster
 * past `SIZE_SLACK` of an even split.
 *
 * Measured on the real library at the default threshold, 903 concepts into 10:
 * **110, 103, 100, 97, 97, 93, 90, 78, 74, 61**, no singletons — and the cores
 * survive the merge, so one cluster is Roma/Imperio Romano/Agustín/Edad Media
 * and another is Tomás de Aquino/Platón/razón/Reforma Protestante.
 *
 * Nothing here touches the DOM, React or `Worker`, for the reason `force.ts`
 * records about itself: a property of numbers can be asserted, and jsdom lays
 * nothing out.
 */

/** What clustering reads of the envelope. Narrower than `GraphIndex` on
 *  purpose: `graphAtlas`'s `AtlasRequest` also satisfies it, so the worker can
 *  cluster without reassembling an index it never receives — and the layout
 *  and the legend then come from one computation rather than two that agree by
 *  luck. `GraphIndex` satisfies it structurally; nothing has to adapt. */
export interface ClusterGraph {
  ids: readonly string[];
  docCount: number;
  edgeSrc: Int32Array;
  edgeDst: Int32Array;
}

/** The node and edge indices one threshold draws. `Subgraph` satisfies it. */
export interface ClusterScope {
  nodes: Int32Array;
  edges: Int32Array;
}

/** How many colour groups to aim for. Ten is what the reader asked for and
 *  also what the palette in `styles.css` holds, so a legend never has to spend
 *  one colour on two groups. */
export const CLUSTER_TARGET = 10;

/** Louvain's resolution. 1 finds *one* community in this corpus — a theology
 *  library really is one connected mass — and 3 starts cutting through
 *  coherent groups ("Arrepentimiento · Judas · Trinidad · comunismo"). 2 was
 *  the value whose communities read as topics on the real corpus. */
const RESOLUTION = 2;

/** How far a cluster may grow past an even split (`concepts / targetClusters`)
 *  before a merge into it is refused. At 1.15 the real library lands between
 *  61 and 110 concepts per cluster against an average of 90; without any cap
 *  at all it lands at 893 and nine leftovers.
 *
 *  **It bounds merging, not Louvain.** A community that already exceeds the
 *  cap is never split — 110 above is exactly that, against a cap of 104 — so
 *  this is the size at which a cluster stops *absorbing*, not a ceiling on
 *  what a cluster can be. Splitting one would mean cutting a group the
 *  structure says belongs together, to hit a number the reader did not ask
 *  for. */
const SIZE_SLACK = 1.15;

export interface Clustering {
  /** Cluster id per node index, dense from 0. `-1` for a book, and for a
   *  concept the subgraph does not reach — same meaning as `-1` everywhere
   *  else in this codebase: "this control has nothing to say about that
   *  node." */
  cluster: Int32Array;
  /** How many clusters exist — not always `targetClusters`. Fewer when the
   *  graph offers fewer communities than that (two concepts with no
   *  co-occurrence path cannot be merged, however small the target), and
   *  fewer again when the size cap refuses the last merges. */
  count: number;
}

interface Edge {
  a: number;
  b: number;
  w: number;
}

/**
 * Louvain community detection: local moving until no node improves modularity,
 * then collapse each community into one node and repeat.
 *
 * Deterministic, which is not automatic and is required here for the same
 * reason `force.ts` seeds its initial placement: a colour that changed on
 * every render would be a different picture of the same library each time.
 * Nodes are visited in the order given, ties are kept by the incumbent
 * (`> bestGain + EPSILON`), and every collection below preserves insertion
 * order.
 */
function louvain(nodes: number[], edges: readonly Edge[], resolution: number): Map<number, number> {
  const slot = new Map<number, number>();
  nodes.forEach((node, i) => slot.set(node, i));

  let n = nodes.length;
  let adjacency: Map<number, number>[] = Array.from({ length: n }, () => new Map<number, number>());
  for (const { a, b, w } of edges) {
    const x = slot.get(a);
    const y = slot.get(b);
    if (x === undefined || y === undefined || x === y) continue;
    const ax = adjacency[x] as Map<number, number>;
    const ay = adjacency[y] as Map<number, number>;
    ax.set(y, (ax.get(y) ?? 0) + w);
    ay.set(x, (ay.get(x) ?? 0) + w);
  }
  let selfLoop = new Float64Array(n);
  /** Which original nodes each collapsed node stands for. */
  let holds: number[][] = nodes.map((node) => [node]);

  const EPSILON = 1e-12;
  for (let round = 0; round < 10; round += 1) {
    const degree = new Float64Array(n);
    let m2 = 0;
    for (let i = 0; i < n; i += 1) {
      let d = (selfLoop[i] as number) * 2;
      for (const w of (adjacency[i] as Map<number, number>).values()) d += w;
      degree[i] = d;
      m2 += d;
    }
    if (m2 === 0) break;

    const community = new Int32Array(n);
    for (let i = 0; i < n; i += 1) community[i] = i;
    const communityDegree = Float64Array.from(degree);

    let moved = false;
    for (let sweep = 0; sweep < 20; sweep += 1) {
      let changed = false;
      for (let i = 0; i < n; i += 1) {
        const own = community[i] as number;
        // Removed from its own community before scoring, or it competes with
        // the mass it is itself contributing.
        communityDegree[own] = (communityDegree[own] as number) - (degree[i] as number);
        const linksTo = new Map<number, number>();
        for (const [j, w] of adjacency[i] as Map<number, number>) {
          const c = community[j] as number;
          linksTo.set(c, (linksTo.get(c) ?? 0) + w);
        }
        let best = own;
        let bestGain =
          (linksTo.get(own) ?? 0) -
          (resolution * (communityDegree[own] as number) * (degree[i] as number)) / m2;
        for (const [c, w] of linksTo) {
          const gain = w - (resolution * (communityDegree[c] as number) * (degree[i] as number)) / m2;
          if (gain > bestGain + EPSILON) {
            bestGain = gain;
            best = c;
          }
        }
        community[i] = best;
        communityDegree[best] = (communityDegree[best] as number) + (degree[i] as number);
        if (best !== own) {
          changed = true;
          moved = true;
        }
      }
      if (!changed) break;
    }
    if (!moved) break;

    const dense = new Map<number, number>();
    for (let i = 0; i < n; i += 1) {
      const c = community[i] as number;
      if (!dense.has(c)) dense.set(c, dense.size);
    }
    const k = dense.size;
    if (k === n) break;

    const nextAdjacency: Map<number, number>[] = Array.from({ length: k }, () => new Map<number, number>());
    const nextSelfLoop = new Float64Array(k);
    const nextHolds: number[][] = Array.from({ length: k }, () => []);
    for (let i = 0; i < n; i += 1) {
      const ci = dense.get(community[i] as number) as number;
      nextSelfLoop[ci] = (nextSelfLoop[ci] as number) + (selfLoop[i] as number);
      for (const held of holds[i] as number[]) (nextHolds[ci] as number[]).push(held);
      for (const [j, w] of adjacency[i] as Map<number, number>) {
        const cj = dense.get(community[j] as number) as number;
        if (ci === cj) {
          // Each undirected edge is visited from both ends; half from each
          // keeps the collapsed self-loop equal to the original weight.
          if (i <= j) nextSelfLoop[ci] = (nextSelfLoop[ci] as number) + w / 2;
        } else {
          const row = nextAdjacency[ci] as Map<number, number>;
          row.set(cj, (row.get(cj) ?? 0) + w);
        }
      }
    }
    n = k;
    adjacency = nextAdjacency;
    selfLoop = nextSelfLoop;
    holds = nextHolds;
  }

  const out = new Map<number, number>();
  holds.forEach((held, c) => {
    for (const node of held) out.set(node, c);
  });
  return out;
}

/**
 * Cluster the concepts the given subgraph draws.
 *
 * Scoped to `sub` on purpose, so raising the degree threshold reclusters what
 * is actually on screen rather than clustering the whole envelope and then
 * discarding most of the answer.
 */
export function clusterConcepts(
  index: ClusterGraph,
  sub: ClusterScope,
  targetClusters: number,
): Clustering {
  const total = index.ids.length;
  const cluster = new Int32Array(total).fill(-1);
  if (targetClusters < 1) return { cluster, count: 0 };

  const present = new Uint8Array(total);
  for (const i of sub.nodes) present[i] = 1;

  // The subgraph as it already is: one edge per (book, concept). Unweighted,
  // because the alternative was measured — weighting by mention count pulls
  // the clusters towards whichever book is verbose rather than towards what
  // its concepts have in common.
  const edges: Edge[] = [];
  const nodes: number[] = [];
  for (let i = 0; i < total; i += 1) if (present[i] === 1) nodes.push(i);
  for (const e of sub.edges) {
    const a = index.edgeSrc[e] as number;
    const b = index.edgeDst[e] as number;
    if (present[a] !== 1 || present[b] !== 1) continue;
    edges.push({ a, b, w: 1 });
  }

  const community = louvain(nodes, edges, RESOLUTION);

  // Books were connectors, not colours.
  const conceptCommunity = new Map<number, number>();
  for (const [node, c] of community) if (node >= index.docCount) conceptCommunity.set(node, c);
  const conceptCount = conceptCommunity.size;
  if (conceptCount === 0) return { cluster, count: 0 };

  const size = new Map<number, number>();
  for (const c of conceptCommunity.values()) size.set(c, (size.get(c) ?? 0) + 1);

  // How strongly two communities are connected, built per book so the cost is
  // linear in edges — the quadratic version of exactly this figure is the
  // 7.3M-pair projection the header explains.
  const perBook = new Map<number, Map<number, number>>();
  for (const e of sub.edges) {
    const book = index.edgeSrc[e] as number;
    const c = conceptCommunity.get(index.edgeDst[e] as number);
    if (c === undefined) continue;
    let counts = perBook.get(book);
    if (counts === undefined) {
      counts = new Map<number, number>();
      perBook.set(book, counts);
    }
    counts.set(c, (counts.get(c) ?? 0) + 1);
  }
  const between = new Map<number, number>();
  const communityCount = size.size;
  const key = (a: number, b: number) => (a < b ? a * communityCount + b : b * communityCount + a);
  for (const counts of perBook.values()) {
    const rows = [...counts.entries()];
    for (let i = 0; i < rows.length; i += 1) {
      for (let j = i + 1; j < rows.length; j += 1) {
        const [ca, na] = rows[i] as [number, number];
        const [cb, nb] = rows[j] as [number, number];
        const k = key(ca, cb);
        between.set(k, (between.get(k) ?? 0) + na * nb);
      }
    }
  }
  // Divided by the two sizes, so a merge is decided by how densely two
  // communities are joined rather than by how big they are — undivided, the
  // largest community wins every merge and grows until the cap stops it,
  // which is the blob again in slower motion.
  const merges = [...between.entries()]
    .map(([k, w]) => {
      const a = Math.floor(k / communityCount);
      const b = k % communityCount;
      return { a, b, w: w / ((size.get(a) as number) * (size.get(b) as number)) };
    })
    .sort((x, y) => (y.w !== x.w ? y.w - x.w : x.a !== y.a ? x.a - y.a : x.b - y.b));

  const cap = Math.max(1, Math.ceil((conceptCount / targetClusters) * SIZE_SLACK));
  const parent = new Map<number, number>();
  const held = new Map<number, number>();
  for (const [c, n] of size) {
    parent.set(c, c);
    held.set(c, n);
  }
  const find = (x: number): number => {
    let root = x;
    while ((parent.get(root) as number) !== root) root = parent.get(root) as number;
    let cursor = x;
    while ((parent.get(cursor) as number) !== cursor) {
      const next = parent.get(cursor) as number;
      parent.set(cursor, root);
      cursor = next;
    }
    return root;
  };

  let components = size.size;
  for (const { a, b } of merges) {
    if (components <= targetClusters) break;
    const ra = find(a);
    const rb = find(b);
    if (ra === rb) continue;
    if ((held.get(ra) as number) + (held.get(rb) as number) > cap) continue;
    parent.set(ra, rb);
    held.set(rb, (held.get(rb) as number) + (held.get(ra) as number));
    components -= 1;
  }

  // Relabelled by descending size, ties broken by the lowest node index in the
  // cluster, so the same subgraph colours the same way on every render.
  const lowest = new Map<number, number>();
  const finalSize = new Map<number, number>();
  for (let i = index.docCount; i < total; i += 1) {
    const c = conceptCommunity.get(i);
    if (c === undefined) continue;
    const root = find(c);
    finalSize.set(root, (finalSize.get(root) ?? 0) + 1);
    if (!lowest.has(root)) lowest.set(root, i);
  }
  const roots = [...finalSize.keys()].sort((a, b) => {
    const bySize = (finalSize.get(b) as number) - (finalSize.get(a) as number);
    return bySize !== 0 ? bySize : (lowest.get(a) as number) - (lowest.get(b) as number);
  });
  const label = new Map<number, number>();
  roots.forEach((root, i) => label.set(root, i));

  for (let i = index.docCount; i < total; i += 1) {
    const c = conceptCommunity.get(i);
    if (c === undefined) continue;
    cluster[i] = label.get(find(c)) as number;
  }

  return { cluster, count: roots.length };
}
