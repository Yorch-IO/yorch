/**
 * The library graph as numbers, so filtering it is arithmetic instead of a
 * request.
 *
 * **Why this module exists.** The degree control was a server-side filter: every
 * change refetched, and the response then drove a 320-tick force simulation
 * inside a `useMemo` during render. Measured on the real corpus (73 books,
 * `lib_teologia`, 2026-09-01) that is 183 ms of query and 209 ms of layout at
 * the default, 3,859 ms at the widest. Measured here, against the same data:
 * **deriving the whole view for a threshold takes 0.88 ms.** The work was never
 * the filtering.
 *
 * **The property that makes a client-side degree filter exact rather than
 * approximate.** `graph/queries.py`'s `library_mentions` computes
 * `documents = count(v)` in its second `WITH` and only then applies
 * `WHERE documents >= $min_documents`. The degree of a concept therefore does
 * not depend on the threshold it was fetched at, so for one
 * `(library, tenant, confidence_floor)` the server's answer at k is exactly the
 * envelope's rows with `documents >= k`. This is an equality, not a heuristic,
 * and `graphModel.test.ts` reimplements the route to assert it.
 *
 * It survives truncation too, which is not obvious: the query's `ORDER BY`
 * begins with `documents DESC`, the *same key the filter uses*, so a threshold's
 * rows are a prefix of the full ordering and `LIMIT` cuts the same tail from
 * both. Reordering that clause to `mentions DESC, documents DESC` would break
 * this silently — hence a test that names the reason.
 *
 * **Index space, and why typed arrays.** Every node is an integer: books in
 * `[0, docCount)`, concepts after. The envelope's own `edges` array is 18,056
 * objects holding two *uninterned* id strings each — about 2.5 MB, against
 * 289 KB for the same information as `Int32Array`s. So the objects are read once
 * here and the caller drops them.
 *
 * Nothing in this file touches the DOM, React, or `Worker`. That is the same
 * decision `force.ts` and `radial.ts` record: jsdom lays nothing out, so a
 * property about numbers can be asserted and a property about pixels cannot.
 */

import type { GraphConcept, GraphDocument, LibraryGraph } from "./api";

/** The thresholds the degree control offers.
 *
 *  1 is "everything" and is reachable on purpose. Measured 2026-09-01 on the
 *  real library: 12,775 concepts at ≥ 1, 2,034 at ≥ 2, 858 at ≥ 3, 322 at ≥ 5.
 *  The default is 3 — two thousand nodes was still not a picture — and the step
 *  above 5 starts reading as a map of general topics rather than of the bridges
 *  between these particular books. */
export const THRESHOLDS = [1, 2, 3, 5, 10] as const;

/** Kept equal to the `min_documents` default in `graph/queries.py`, which both
 *  planes' routes already derive from the template registry. The client cannot
 *  read that registry, so this is the fourth site the repository's own note
 *  about this number counts — and the one a test names. */
export const DEFAULT_THRESHOLD = 3;

export interface GraphIndex {
  libraryId: string;
  confidenceFloor: number;
  /** Node ids in index order: books first, then concepts. */
  ids: string[];
  /** Where the concepts begin. `i < docCount` is a book. */
  docCount: number;
  /** A book's title (or its version id) and a concept's name. */
  label: string[];
  /** `label` lowercased once, so a keystroke does not lowercase 12,775 strings.
   *  The search box did exactly that on every character before this existed. */
  lower: string[];
  /** A concept's `conceptType`; `null` for a book. */
  conceptType: (string | null)[];
  /** **The degree the database counted, not the edges in this payload.** A
   *  concept 59 books mention arrives with a handful of its edges when the
   *  response is capped; recomputing degree from the edges here would understate
   *  every concept on screen. 0 for a book. */
  degree: Int32Array;
  /** Mentions summed across every book, as the route folded them. 0 for a book. */
  mentions: Int32Array;
  edgeSrc: Int32Array;
  edgeDst: Int32Array;
  edgeMentions: Int32Array;
  edgeConfidence: Float32Array;
  /** `degree[edgeDst[e]]` — the threshold at which this edge stops being drawn,
   *  precomputed so the per-threshold scan never has to indirect. */
  edgeDegree: Int32Array;
  /** For the stroke widths. Taken over the whole envelope rather than over one
   *  threshold's edges, so raising the threshold removes lines instead of
   *  rescaling the survivors. */
  maxEdgeMentions: number;
  /** The row count at which the server would have cut this response, or
   *  `Infinity` when it did not cut it. Derived, never assumed: `truncated.edges`
   *  is a fact about the envelope, and each threshold needs its own answer. */
  envelopeCap: number;
}

/** An undirected CSR over the whole index space.
 *
 *  One structure answers both directions — `neighbours(adj, i)` gives a book's
 *  concepts when `i` is a book and a concept's books when it is not — because
 *  books and concepts share one numbering. It replaces a scan of all 18,056
 *  edges on every selection and every hover. */
export interface Adjacency {
  offset: Int32Array;
  neighbour: Int32Array;
}

export interface Subgraph {
  /** Node indices drawn at this threshold: every book, then the surviving
   *  concepts. */
  nodes: Int32Array;
  /** Edge indices drawn at this threshold. */
  edges: Int32Array;
}

export interface ThresholdCount {
  threshold: number;
  /** Always every book. `library_documents` takes no `min_documents`, so the
   *  server does not drop books at a high threshold and neither may we — a book
   *  with no surviving concept is a true statement about the filter. */
  books: number;
  concepts: number;
  edges: number;
  truncated: boolean;
}

/** Read the envelope once. Everything else in this file reads the result. */
export function buildIndex(data: LibraryGraph): GraphIndex {
  const docCount = data.documents.length;
  const conceptCount = data.concepts.length;
  const total = docCount + conceptCount;

  const ids: string[] = new Array<string>(total);
  const label: string[] = new Array<string>(total);
  const lower: string[] = new Array<string>(total);
  const conceptType: (string | null)[] = new Array<string | null>(total);
  const degree = new Int32Array(total);
  const mentions = new Int32Array(total);
  const slot = new Map<string, number>();

  for (let i = 0; i < docCount; i += 1) {
    const d = data.documents[i] as GraphDocument;
    const name = d.title ?? d.versionId;
    ids[i] = d.versionId;
    label[i] = name;
    lower[i] = name.toLocaleLowerCase();
    conceptType[i] = null;
    slot.set(d.versionId, i);
  }
  for (let j = 0; j < conceptCount; j += 1) {
    const c = data.concepts[j] as GraphConcept;
    const i = docCount + j;
    ids[i] = c.id;
    label[i] = c.name;
    lower[i] = c.name.toLocaleLowerCase();
    conceptType[i] = c.conceptType;
    degree[i] = c.documents;
    mentions[i] = c.mentions;
    slot.set(c.id, i);
  }

  // An edge whose endpoint is not in the response is dropped rather than
  // throwing, for the reason `force.ts` gives about its own dangling edges: the
  // two lists arrive in one payload, but a caller that filtered one of them
  // should get a graph, not an exception.
  const n = data.edges.length;
  const src = new Int32Array(n);
  const dst = new Int32Array(n);
  const edgeMentionsAll = new Int32Array(n);
  const edgeConfidenceAll = new Float32Array(n);
  let kept = 0;
  let maxEdgeMentions = 1;
  for (let e = 0; e < n; e += 1) {
    const edge = data.edges[e];
    if (edge === undefined) continue;
    const a = slot.get(edge.versionId);
    const b = slot.get(edge.conceptId);
    if (a === undefined || b === undefined) continue;
    src[kept] = a;
    dst[kept] = b;
    edgeMentionsAll[kept] = edge.mentions;
    edgeConfidenceAll[kept] = edge.confidence;
    if (edge.mentions > maxEdgeMentions) maxEdgeMentions = edge.mentions;
    kept += 1;
  }

  const edgeSrc = src.slice(0, kept);
  const edgeDst = dst.slice(0, kept);
  const edgeDegree = new Int32Array(kept);
  for (let e = 0; e < kept; e += 1) edgeDegree[e] = degree[edgeDst[e] as number] as number;

  return {
    libraryId: data.libraryId,
    confidenceFloor: data.confidenceFloor,
    ids,
    docCount,
    label,
    lower,
    conceptType,
    degree,
    mentions,
    edgeSrc,
    edgeDst,
    edgeMentions: edgeMentionsAll.slice(0, kept),
    edgeConfidence: edgeConfidenceAll.slice(0, kept),
    edgeDegree,
    maxEdgeMentions,
    // The envelope was cut at exactly the number of rows it carries; it was not
    // cut, so nothing below it can be. `Infinity` is the honest "no ceiling
    // applies" rather than a large number that would one day be reached.
    envelopeCap: data.truncated.edges ? data.edges.length : Infinity,
  };
}

/** Counting sort into a CSR. Two passes over the edges, no per-node arrays. */
export function buildAdjacency(index: GraphIndex): Adjacency {
  const total = index.ids.length;
  const offset = new Int32Array(total + 1);
  const edges = index.edgeSrc.length;

  // `offset[x] += 1` reads as `number | undefined` under
  // `noUncheckedIndexedAccess`, and a compound assignment leaves nowhere to put
  // the assertion — so the reads are explicit. Same reason `force.ts:137-143`
  // gives for preferring fields to parallel arrays inside its own kernel.
  for (let e = 0; e < edges; e += 1) {
    const a = (index.edgeSrc[e] as number) + 1;
    const b = (index.edgeDst[e] as number) + 1;
    offset[a] = (offset[a] as number) + 1;
    offset[b] = (offset[b] as number) + 1;
  }
  for (let i = 0; i < total; i += 1) {
    offset[i + 1] = (offset[i + 1] as number) + (offset[i] as number);
  }

  const neighbour = new Int32Array(edges * 2);
  const cursor = offset.slice(0, total);
  for (let e = 0; e < edges; e += 1) {
    const a = index.edgeSrc[e] as number;
    const b = index.edgeDst[e] as number;
    neighbour[cursor[a] as number] = b;
    cursor[a] = (cursor[a] as number) + 1;
    neighbour[cursor[b] as number] = a;
    cursor[b] = (cursor[b] as number) + 1;
  }
  return { offset, neighbour };
}

/** A view into `neighbour`, not a copy: hover must not allocate. */
export function neighbours(adj: Adjacency, node: number): Int32Array {
  const from = adj.offset[node];
  const to = adj.offset[node + 1];
  if (from === undefined || to === undefined) return new Int32Array(0);
  return adj.neighbour.subarray(from, to);
}

/** What the server would have returned for this threshold. See the note at the
 *  top of the file for why "would have" is an equality here. */
export function subgraphAt(index: GraphIndex, threshold: number): Subgraph {
  const total = index.ids.length;
  const nodes = new Int32Array(total);
  let n = 0;
  for (let i = 0; i < index.docCount; i += 1) nodes[n++] = i;
  for (let i = index.docCount; i < total; i += 1) {
    if ((index.degree[i] as number) >= threshold) nodes[n++] = i;
  }

  const edgeCount = index.edgeDegree.length;
  const edges = new Int32Array(edgeCount);
  let m = 0;
  for (let e = 0; e < edgeCount; e += 1) {
    if ((index.edgeDegree[e] as number) >= threshold) edges[m++] = e;
  }
  return { nodes: nodes.slice(0, n), edges: edges.slice(0, m) };
}

/** Every threshold's counts in two passes, so a slider can label all five stops
 *  without deriving anything per frame. */
export function histogram(
  index: GraphIndex,
  thresholds: readonly number[] = THRESHOLDS,
): ThresholdCount[] {
  const rows = thresholds.map((threshold) => ({
    threshold,
    books: index.docCount,
    concepts: 0,
    edges: 0,
    truncated: false,
  }));

  const total = index.ids.length;
  for (let i = index.docCount; i < total; i += 1) {
    const d = index.degree[i] as number;
    for (const row of rows) if (d >= row.threshold) row.concepts += 1;
  }
  const edgeCount = index.edgeDegree.length;
  for (let e = 0; e < edgeCount; e += 1) {
    const d = index.edgeDegree[e] as number;
    for (const row of rows) if (d >= row.threshold) row.edges += 1;
  }
  for (const row of rows) row.truncated = row.edges >= index.envelopeCap;
  return rows;
}

/** Node indices whose label contains `needle`, matched against the lowercased
 *  copy built once. An empty needle matches nothing rather than everything: the
 *  caller reads `null`-vs-empty as "not searching" and must not have to. */
export function matchesQuery(index: GraphIndex, needle: string): Int32Array {
  const wanted = needle.trim().toLocaleLowerCase();
  if (wanted === "") return new Int32Array(0);
  const total = index.ids.length;
  const found = new Int32Array(total);
  let n = 0;
  for (let i = 0; i < total; i += 1) {
    const text = index.lower[i];
    if (text !== undefined && text.includes(wanted)) found[n++] = i;
  }
  return found.slice(0, n);
}

/**
 * Everything the client-side controls decide, in one pass.
 *
 * They compose in an order that is not arbitrary: the degree threshold has
 * already produced `sub`, the kind and book filters remove nodes, the edges are
 * then whatever has both ends left, and "hide what joins nothing" runs **last**
 * because it is a question about the edges that survived the others. Doing it
 * earlier would answer a question about a picture nobody is looking at.
 *
 * Returned as a mask rather than a list because that is what both consumers
 * want: the canvas indexes it per edge endpoint, and the node rendering reads
 * it per node.
 */
export interface ViewFilters {
  books: boolean;
  concepts: boolean;
  /** A book's version id, or null. Narrows the canvas to that book and the
   *  concepts it mentions — the question "what is *this* one about" asked of
   *  the overview instead of the document view. */
  onlyBook: string | null;
  /** What the search found, or null when nothing is being searched. */
  matched: ReadonlySet<string> | null;
  /** Whether the search hides what it did not find, or only marks it. Marking
   *  is the default and the recorded reason still stands: cutting the canvas
   *  down to the hits hides the neighbourhood, which is the only thing that
   *  makes a hit worth looking at. This is the escape hatch for a reader who
   *  wants the bare set anyway. */
  onlyFound: boolean;
  hideIsolated: boolean;
}

export interface VisibleView {
  /** 1 where the node is drawn. Indexed by node, like everything else here. */
  nodes: Uint8Array;
  /** Edge indices with both ends drawn. */
  edges: Int32Array;
  books: number;
  concepts: number;
}

export function visibleIn(
  index: GraphIndex,
  sub: Subgraph,
  filters: ViewFilters,
): VisibleView {
  const nodes = new Uint8Array(index.ids.length);
  for (const i of sub.nodes) nodes[i] = 1;

  for (let i = 0; i < index.ids.length; i += 1) {
    if (nodes[i] === 0) continue;
    const isBook = i < index.docCount;
    if (isBook ? !filters.books : !filters.concepts) nodes[i] = 0;
    if (filters.onlyFound && filters.matched !== null && !filters.matched.has(index.ids[i] as string)) {
      nodes[i] = 0;
    }
  }

  if (filters.onlyBook !== null) {
    const book = index.ids.indexOf(filters.onlyBook);
    const kept = new Uint8Array(index.ids.length);
    if (book >= 0 && nodes[book] === 1) {
      kept[book] = 1;
      for (let e = 0; e < index.edgeSrc.length; e += 1) {
        if ((index.edgeSrc[e] as number) !== book) continue;
        const other = index.edgeDst[e] as number;
        if (nodes[other] === 1) kept[other] = 1;
      }
    }
    nodes.set(kept);
  }

  let edges = edgesAmong(index, sub, nodes);

  if (filters.hideIsolated) {
    const touched = new Uint8Array(index.ids.length);
    for (const e of edges) {
      touched[index.edgeSrc[e] as number] = 1;
      touched[index.edgeDst[e] as number] = 1;
    }
    for (let i = 0; i < nodes.length; i += 1) if (touched[i] === 0) nodes[i] = 0;
    // Dropping an isolated node removes no edge — it had none — so the list
    // above is already final and is not recomputed.
    edges = edgesAmong(index, sub, nodes);
  }

  let books = 0;
  let concepts = 0;
  for (let i = 0; i < nodes.length; i += 1) {
    if (nodes[i] === 0) continue;
    if (i < index.docCount) books += 1;
    else concepts += 1;
  }
  return { nodes, edges, books, concepts };
}

function edgesAmong(index: GraphIndex, sub: Subgraph, nodes: Uint8Array): Int32Array {
  const out = new Int32Array(sub.edges.length);
  let n = 0;
  for (const e of sub.edges) {
    if (nodes[index.edgeSrc[e] as number] === 1 && nodes[index.edgeDst[e] as number] === 1) {
      out[n++] = e;
    }
  }
  return out.slice(0, n);
}

/** Nodes in `sub` that no edge in `sub` reaches. Free once the subgraph exists,
 *  and it is what the "hide what joins nothing" filter needs — at a high
 *  threshold most of the 73 books are in this set, which is a true statement
 *  about the filter rather than a fault. */
export function isolatedIn(index: GraphIndex, sub: Subgraph): Int32Array {
  const seen = new Uint8Array(index.ids.length);
  for (const e of sub.edges) {
    seen[index.edgeSrc[e] as number] = 1;
    seen[index.edgeDst[e] as number] = 1;
  }
  const out = new Int32Array(sub.nodes.length);
  let n = 0;
  for (const i of sub.nodes) if (seen[i] === 0) out[n++] = i;
  return out.slice(0, n);
}

/** How far a cluster may grow past a perfectly even split (`conceptCount /
 *  targetClusters`) before `clusterConcepts` refuses to merge into it. Below
 *  1.0 no cluster could ever reach the average, which is a stricter promise
 *  than "roughly ten groups" needs; 1.5 caps any one cluster at 15% of the
 *  whole when the target is 10, which is what stops the failure this cap
 *  exists for — see the note where it is used. */
const SIZE_SLACK = 1.5;

export interface Clustering {
  /** Cluster id per node index, dense from 0. `-1` for a book, and for a
   *  concept the subgraph does not reach — same meaning as `-1` everywhere
   *  else in this file: "this control has nothing to say about that node." */
  cluster: Int32Array;
  /** How many clusters exist — not always `targetClusters`, in either
   *  direction. Fewer than it when the subgraph holds fewer concepts than
   *  that to begin with, since nothing merges what does not need to. More
   *  than it when the concept-concept graph's own connected components
   *  outnumber the target: merging cannot join two concepts with no
   *  co-occurrence path between them, however small the target is set. */
  count: number;
}

/**
 * Concepts grouped by which books connect them, not by any field on the
 * concept itself.
 *
 * **Why not `conceptType`.** It reads as the obvious grouping and is not one:
 * measured on the running corpus, it is free text from the extractor with
 * **1,619 distinct values** after folding case, accents and underscores alone
 * — "concepto teológico", "concepto filosófico" and "concepto religioso" are
 * three names for one idea a human would merge on sight, and nothing here can
 * do that automatically. Structure is the alternative that needs no curation:
 * two concepts that appear together in many of the same books are related
 * whatever either of them is called, and every book already reports exactly
 * that through its `MENTIONS` edges.
 *
 * **The algorithm is Kruskal's, stopped early.** Weight every concept pair by
 * how many books in `sub` mention both, sort descending, and union-find merge
 * the heaviest pair first — same as building a maximum spanning forest — but
 * stop the moment `targetClusters` components remain instead of continuing to
 * one. That is the standard reading of "cut the k-1 lightest edges of a
 * maximum spanning forest": a pair of concepts that share only one obscure
 * book is exactly the pair such an edge represents, and it is where this
 * algorithm stops rather than forcing a merge a reader would not recognise.
 *
 * **Sizes need a cap, not just a target count.** The first version merged
 * purely by weight with no size limit, and on the real corpus that produced
 * one cluster of 893 concepts and nine of one or two — a dense, well-connected
 * library is not several comparably-sized regions with a few weak edges
 * between them; it is closer to one thing almost everything co-occurs with at
 * some weight, so "cut the lightest edges" cuts only the handful of truly
 * isolated concepts and fuses the rest. `SIZE_SLACK` bounds how far any one
 * cluster may grow past an even split, which is what makes ten clusters
 * actually read as ten regions rather than one region and nine footnotes.
 *

 * Scoped to `sub` on purpose, so raising the degree threshold reclusters what
 * is actually on screen rather than clustering the whole envelope and then
 * discarding most of the answer.
 */
export function clusterConcepts(
  index: GraphIndex,
  sub: Subgraph,
  targetClusters: number,
): Clustering {
  const total = index.ids.length;
  const cluster = new Int32Array(total).fill(-1);
  if (targetClusters < 1) return { cluster, count: 0 };

  const present = new Uint8Array(total);
  for (const i of sub.nodes) present[i] = 1;

  // Every edge in the envelope is book-to-concept, never concept-to-concept —
  // `buildIndex` only ever fills `edgeSrc`/`edgeDst` from `{versionId,
  // conceptId}` pairs — so a concept pair's shared-book weight can only be
  // built by walking each book's own concepts once and pairing them up.
  const byBook = new Map<number, number[]>();
  for (const e of sub.edges) {
    const doc = index.edgeSrc[e] as number;
    const concept = index.edgeDst[e] as number;
    if (present[concept] !== 1) continue;
    let list = byBook.get(doc);
    if (list === undefined) {
      list = [];
      byBook.set(doc, list);
    }
    list.push(concept);
  }

  // Keyed by `a * total + b` with `a < b`, which is a bijection into a range
  // this module already sizes everything else by — cheaper than a string key
  // and needs no second map to recover the pair from.
  const weight = new Map<number, number>();
  for (const list of byBook.values()) {
    list.sort((a, b) => a - b);
    for (let i = 0; i < list.length; i += 1) {
      for (let j = i + 1; j < list.length; j += 1) {
        const key = (list[i] as number) * total + (list[j] as number);
        weight.set(key, (weight.get(key) ?? 0) + 1);
      }
    }
  }

  const pairs = [...weight.entries()]
    .map(([key, w]) => ({ a: Math.floor(key / total), b: key % total, w }))
    .sort((x, y) => y.w - x.w);

  const parent = new Int32Array(total);
  const size = new Int32Array(total);
  let conceptCount = 0;
  for (let i = index.docCount; i < total; i += 1) {
    parent[i] = i;
    if (present[i] === 1) {
      size[i] = 1;
      conceptCount += 1;
    }
  }
  const find = (x: number): number => {
    while (parent[x] !== x) {
      parent[x] = parent[parent[x] as number] as number;
      x = parent[x] as number;
    }
    return x;
  };

  // Unbounded, this is where a dense real corpus goes wrong: measured on the
  // running library, an unconstrained cut left one cluster of 893 concepts
  // and nine of one or two — "cut the lightest edges" assumes several
  // comparably-weighted regions to cut *between*, and a theology corpus is
  // instead one thing almost everything co-occurs with at some weight, so
  // nearly every edge stays above the few genuinely isolated ones. The cap
  // below is what turns "10 clusters" back into 10 clusters that are each a
  // plausible fraction of the whole, at the cost of the merge sometimes
  // stopping before `targetClusters` is reached — see `Clustering.count`.
  const cap = Math.max(1, Math.ceil((conceptCount / targetClusters) * SIZE_SLACK));

  let components = conceptCount;
  for (const { a, b } of pairs) {
    if (components <= targetClusters) break;
    const ra = find(a);
    const rb = find(b);
    if (ra === rb) continue;
    if ((size[ra] as number) + (size[rb] as number) > cap) continue;
    parent[ra] = rb;
    size[rb] = (size[rb] as number) + (size[ra] as number);
    components -= 1;
  }

  // Relabelled by descending size and then by the root's own index, so the
  // same subgraph clusters the same way on every render — the determinism
  // `force.ts` seeds initial placement for, applied to a colour instead of a
  // position.
  const sizeOf = new Map<number, number>();
  for (let i = index.docCount; i < total; i += 1) {
    if (present[i] !== 1) continue;
    const r = find(i);
    sizeOf.set(r, (sizeOf.get(r) ?? 0) + 1);
  }
  const roots = [...sizeOf.keys()].sort((a, b) => {
    const bySize = (sizeOf.get(b) as number) - (sizeOf.get(a) as number);
    return bySize !== 0 ? bySize : a - b;
  });
  const label = new Map<number, number>();
  roots.forEach((r, i) => label.set(r, i));

  for (let i = index.docCount; i < total; i += 1) {
    if (present[i] !== 1) continue;
    cluster[i] = label.get(find(i)) as number;
  }

  return { cluster, count: roots.length };
}
