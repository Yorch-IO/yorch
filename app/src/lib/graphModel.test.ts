import { describe, expect, it } from "vitest";

import type { GraphConcept, GraphDocument, GraphEdge, LibraryGraph } from "./api";
import {
  buildAdjacency,
  buildIndex,
  clusterConcepts,
  DEFAULT_THRESHOLD,
  histogram,
  isolatedIn,
  matchesQuery,
  neighbours,
  subgraphAt,
  THRESHOLDS,
  visibleIn,
  type ViewFilters,
} from "./graphModel";

/**
 * The derivation, which is the only place the client-side degree filter can be
 * checked.
 *
 * The load-bearing test is the first one: it reimplements the FastAPI route's
 * own folding and asserts that filtering the `min_documents = 1` envelope gives
 * byte-for-byte what the server would have answered at each threshold. Every
 * other saving in this feature rests on that equality, so it is asserted
 * against a second implementation rather than against the first one's output.
 *
 * The counts used by the budget test are the ones that ship: 73 books and
 * 12,775 concepts over 18,056 mentions, measured on `lib_teologia` 2026-09-01.
 */

function envelope(over: Partial<LibraryGraph> = {}): LibraryGraph {
  return {
    libraryId: "lib_a",
    semantic: true,
    confidenceFloor: 0.6,
    // The envelope is always fetched whole; a threshold is a view of it.
    minDocuments: 1,
    documents: [
      { documentId: "doc_1", versionId: "ver_1", title: "Historia", format: "pdf" },
      { documentId: "doc_2", versionId: "ver_2", title: "Doctrina", format: "pdf" },
      { documentId: "doc_3", versionId: "ver_3", title: "Sin conceptos", format: "pdf" },
    ],
    // `documents` is set independently of how many edges each concept carries,
    // because that is exactly the case the route has to survive: the degree is
    // what the database counted, and a capped response holds only some of the
    // edges behind it. A fixture where degree happened to equal the edge count
    // would let a wrong implementation pass by recounting.
    concepts: [
      { id: "con_wide", name: "Gracia", conceptType: "Doctrina", mentions: 30, documents: 5 },
      { id: "con_three", name: "Bautismo", conceptType: null, mentions: 9, documents: 3 },
      { id: "con_two", name: "Concilio", conceptType: null, mentions: 4, documents: 2 },
      { id: "con_one", name: "Anécdota", conceptType: null, mentions: 1, documents: 1 },
    ],
    edges: [
      { versionId: "ver_1", conceptId: "con_wide", mentions: 20, confidence: 0.95 },
      { versionId: "ver_2", conceptId: "con_wide", mentions: 10, confidence: 0.9 },
      { versionId: "ver_1", conceptId: "con_three", mentions: 6, confidence: 0.8 },
      { versionId: "ver_2", conceptId: "con_three", mentions: 3, confidence: 0.75 },
      { versionId: "ver_2", conceptId: "con_two", mentions: 4, confidence: 0.7 },
      { versionId: "ver_1", conceptId: "con_one", mentions: 1, confidence: 0.65 },
    ],
    truncated: { documents: false, edges: false },
    ...over,
  };
}

/**
 * `brainworker/api/main.py:1226-1256`, reimplemented.
 *
 * The route receives the template's rows — one per (version, concept) pair that
 * survived `WHERE documents >= $min_documents` — folds them into concepts and
 * emits one edge per row. Feeding it the envelope's rows filtered by the same
 * predicate is what the server does at that threshold.
 */
function serverWould(
  data: LibraryGraph,
  threshold: number,
): { concepts: GraphConcept[]; edges: GraphEdge[] } {
  const degree = new Map(data.concepts.map((c) => [c.id, c.documents]));
  const rows = data.edges.filter((e) => (degree.get(e.conceptId) ?? 0) >= threshold);

  const concepts = new Map<string, GraphConcept>();
  const edges: GraphEdge[] = [];
  for (const row of rows) {
    const seen = concepts.get(row.conceptId);
    if (seen === undefined) {
      const c = data.concepts.find((x) => x.id === row.conceptId) as GraphConcept;
      concepts.set(row.conceptId, { ...c, mentions: row.mentions });
    } else {
      seen.mentions += row.mentions;
    }
    edges.push(row);
  }
  return { concepts: [...concepts.values()], edges };
}

describe("the client-side degree filter", () => {
  it("returns exactly what the server would have returned, at every threshold", () => {
    // The equality the whole feature rests on. `library_mentions` computes
    // `documents = count(v)` in its second WITH and applies
    // `WHERE documents >= $min_documents` only afterwards, so a concept's degree
    // does not depend on the threshold it was fetched at.
    const data = envelope();
    const index = buildIndex(data);

    for (const threshold of THRESHOLDS) {
      const theirs = serverWould(data, threshold);
      const sub = subgraphAt(index, threshold);

      const ourConcepts = [...sub.nodes]
        .filter((i) => i >= index.docCount)
        .map((i) => index.ids[i])
        .sort();
      expect(ourConcepts).toEqual(theirs.concepts.map((c) => c.id).sort());

      const ourEdges = [...sub.edges]
        .map((e) => `${index.ids[index.edgeSrc[e] as number]}:${index.ids[index.edgeDst[e] as number]}`)
        .sort();
      expect(ourEdges).toEqual(theirs.edges.map((e) => `${e.versionId}:${e.conceptId}`).sort());
    }
  });

  it("keeps every book, including one no surviving concept mentions", () => {
    // `library_documents` is a separate template and takes no `min_documents`,
    // so the server does not drop books at a high threshold. A book alone on the
    // canvas is a true statement about the filter, not a fault to hide.
    const index = buildIndex(envelope());
    const sub = subgraphAt(index, 10);
    expect([...sub.nodes].filter((i) => i < index.docCount)).toHaveLength(3);
    expect([...sub.nodes].filter((i) => i >= index.docCount)).toHaveLength(0);
  });

  it("counts a concept's books from the database's number, not from its edges", () => {
    // `con_wide` says 5 books and carries 2 edges, which is what a capped
    // response looks like. Recomputing degree from the payload would understate
    // every concept on screen and would drop this one at threshold 3.
    const index = buildIndex(envelope());
    const wide = index.ids.indexOf("con_wide");
    expect(index.degree[wide]).toBe(5);
    expect(neighbours(buildAdjacency(index), wide)).toHaveLength(2);
    expect([...subgraphAt(index, 5).nodes]).toContain(wide);
  });

  it("reports truncation per threshold rather than copying the envelope's flag", () => {
    // The envelope was cut, so the widest view is short — but a threshold whose
    // rows all fit is complete, and saying otherwise would put "recortado" on a
    // picture that is whole.
    //
    // That this works at all is a property of the query's ORDER BY: it begins
    // with `documents DESC`, the same key the filter uses, so a threshold's rows
    // are a *prefix* of the ordering and LIMIT cuts the same tail from both.
    // Reordering that clause to `mentions DESC, documents DESC` would break the
    // equivalence silently, which is why this test names the reason.
    const data = envelope();
    const index = buildIndex({ ...data, truncated: { documents: false, edges: true } });
    const rows = histogram(index, [1, 3]);

    expect(rows[0]?.truncated).toBe(true);
    expect(rows[1]?.truncated).toBe(false);
  });
});

describe("the indexes built once per envelope", () => {
  it("answers both directions of a neighbour question from one structure", () => {
    const index = buildIndex(envelope());
    const adj = buildAdjacency(index);

    const book = index.ids.indexOf("ver_1");
    const concept = index.ids.indexOf("con_three");

    expect([...neighbours(adj, book)].map((i) => index.ids[i]).sort())
      .toEqual(["con_one", "con_three", "con_wide"]);
    expect([...neighbours(adj, concept)].map((i) => index.ids[i]).sort())
      .toEqual(["ver_1", "ver_2"]);
  });

  it("returns a view rather than a copy, so hovering allocates nothing", () => {
    const index = buildIndex(envelope());
    const adj = buildAdjacency(index);
    const view = neighbours(adj, index.ids.indexOf("ver_1"));
    expect(view.buffer).toBe(adj.neighbour.buffer);
  });

  it("has no neighbours for a node no edge reaches", () => {
    const index = buildIndex(envelope());
    const adj = buildAdjacency(index);
    expect(neighbours(adj, index.ids.indexOf("ver_3"))).toHaveLength(0);
  });

  it("labels every slider stop in one pass", () => {
    const rows = histogram(buildIndex(envelope()));
    expect(rows.map((r) => r.threshold)).toEqual([...THRESHOLDS]);
    expect(rows.map((r) => r.concepts)).toEqual([4, 3, 2, 1, 0]);
    // Books are never filtered, so the count is the same at every stop.
    expect(rows.every((r) => r.books === 3)).toBe(true);
    // Monotone: a higher bar cannot admit more.
    for (let i = 1; i < rows.length; i += 1) {
      expect(rows[i]?.concepts).toBeLessThanOrEqual(rows[i - 1]?.concepts as number);
      expect(rows[i]?.edges).toBeLessThanOrEqual(rows[i - 1]?.edges as number);
    }
  });

  it("lowercases the search index once, not once per keystroke", () => {
    const index = buildIndex(envelope());
    expect(index.lower).toHaveLength(index.ids.length);
    expect([...matchesQuery(index, "GRAC")].map((i) => index.label[i])).toEqual(["Gracia"]);
    // A book is searchable by its title, which is what it is labelled with.
    expect([...matchesQuery(index, "histor")].map((i) => index.ids[i])).toEqual(["ver_1"]);
  });

  it("treats an empty search as no search rather than as everything", () => {
    const index = buildIndex(envelope());
    expect(matchesQuery(index, "   ")).toHaveLength(0);
  });

  it("finds what a filter left joined to nothing", () => {
    const index = buildIndex(envelope());
    const sub = subgraphAt(index, 3);
    // ver_3 has no concepts at all; at this threshold nothing else is stranded.
    expect([...isolatedIn(index, sub)].map((i) => index.ids[i])).toEqual(["ver_3"]);
  });

  it("drops an edge whose endpoint the response did not carry", () => {
    // The two lists arrive in one payload, but a caller that filtered one of
    // them should get a graph, not an exception — the rule `force.ts` already
    // applies to its own dangling edges.
    const data = envelope();
    data.edges.push({ versionId: "ver_gone", conceptId: "con_wide", mentions: 1, confidence: 0.9 });
    const index = buildIndex(data);
    expect(index.edgeSrc).toHaveLength(6);
  });
});

describe("the filters the reader can move", () => {
  const all: ViewFilters = {
    books: true,
    concepts: true,
    onlyBook: null,
    matched: null,
    onlyFound: false,
    hideIsolated: false,
  };

  function view(over: Partial<ViewFilters> = {}, threshold = 1) {
    const index = buildIndex(envelope());
    const sub = subgraphAt(index, threshold);
    return { index, out: visibleIn(index, sub, { ...all, ...over }) };
  }

  it("keeps an edge only when both of its ends are drawn", () => {
    const { out } = view({ books: false });
    expect(out.edges).toHaveLength(0);
    expect(out.books).toBe(0);
  });

  it("narrows to one book and the concepts it mentions", () => {
    // The overview asked "what is this one about", which is the document view's
    // question — but answered without leaving the map.
    const { index, out } = view({ onlyBook: "ver_1" });
    const drawn = index.ids.filter((_, i) => out.nodes[i] === 1);
    expect(drawn).toEqual(["ver_1", "con_wide", "con_three", "con_one"]);
    expect(out.books).toBe(1);
  });

  it("draws nothing for a book the threshold already removed", () => {
    // Not a special case in the code — the book is simply not in the mask the
    // narrowing starts from — but it is the behaviour a reader would query.
    const { out } = view({ onlyBook: "ver_missing" });
    expect(out.books + out.concepts).toBe(0);
  });

  it("marks by default and hides only when asked", () => {
    const matched = new Set(["con_wide"]);
    expect(view({ matched }).out.concepts).toBe(4);
    expect(view({ matched, onlyFound: true }).out.concepts).toBe(1);
  });

  it("hides what joins nothing, and only after the other filters have run", () => {
    // `ver_3` has no concepts at all, and at threshold 3 `con_one` has no book
    // left either. Running this before the degree filter would answer a
    // question about a picture nobody is looking at.
    const { index, out } = view({ hideIsolated: true }, 3);
    const drawn = index.ids.filter((_, i) => out.nodes[i] === 1);
    expect(drawn).not.toContain("ver_3");
    expect(drawn).toContain("ver_1");
    expect(drawn).toContain("con_three");
  });

  it("counts what is drawn, which is what the controls say", () => {
    const { out } = view({}, 3);
    expect(out.books).toBe(3);
    expect(out.concepts).toBe(2);
  });
});

describe("what it costs", () => {
  it("indexes the real library in a time the response can absorb", () => {
    // Not an assertion about speed. What this rules out is the shape
    // `LibraryGraph.tsx` paid on every response — `documents.map(d =>
    // edges.filter(...))`, 73 × 18,056 — for a number whose only consumer,
    // `ForceNode.weight`, `settle()` never reads. Generous on purpose, as
    // `force.test.ts` is about the quadratic layout.
    const books = 73;
    const conceptCount = 12_775;
    const documents: GraphDocument[] = [];
    for (let i = 0; i < books; i += 1) {
      documents.push({ documentId: `doc_${i}`, versionId: `ver_${i}`, title: `Libro ${i}`, format: "pdf" });
    }
    const concepts: GraphConcept[] = [];
    const edges: GraphEdge[] = [];
    for (let i = 0; i < conceptCount; i += 1) {
      // The measured distribution: most concepts reach one book, a long tail
      // reaches many. 18,056 edges over 12,775 concepts is a mean of 1.41.
      const degree = i < 10_741 ? 1 : i < 11_917 ? 2 : i < 12_256 ? 3 : 5;
      concepts.push({ id: `con_${i}`, name: `Concepto ${i}`, conceptType: null, mentions: degree * 3, documents: degree });
      for (let d = 0; d < degree; d += 1) {
        edges.push({ versionId: `ver_${(i * 7 + d * 13) % books}`, conceptId: `con_${i}`, mentions: 1 + (i % 5), confidence: 0.9 });
      }
    }

    const data = envelope({ documents, concepts, edges });
    const started = Date.now();
    const index = buildIndex(data);
    buildAdjacency(index);
    histogram(index);
    subgraphAt(index, DEFAULT_THRESHOLD);
    const elapsed = Date.now() - started;

    expect(index.ids).toHaveLength(books + conceptCount);
    expect(elapsed).toBeLessThan(1_000);
  });
});

describe("clusterConcepts", () => {
  // Two cliques of three books each, sharing no book — "clique_a" concepts
  // co-occur only with each other, same for "clique_b" — plus one concept in
  // a book of its own, which no other concept shares. Real names rather than
  // `con_0..8` so a failing assertion is legible about which group misplaced.
  function twoCliques(): LibraryGraph {
    const documents: GraphDocument[] = [
      { documentId: "d1", versionId: "ver_1", title: "Uno", format: "pdf" },
      { documentId: "d2", versionId: "ver_2", title: "Dos", format: "pdf" },
      { documentId: "d3", versionId: "ver_3", title: "Tres", format: "pdf" },
      { documentId: "d4", versionId: "ver_4", title: "Cuatro", format: "pdf" },
      { documentId: "d5", versionId: "ver_5", title: "Cinco", format: "pdf" },
      { documentId: "d6", versionId: "ver_6", title: "Seis", format: "pdf" },
      { documentId: "d7", versionId: "ver_7", title: "Siete", format: "pdf" },
    ];
    const aConcepts = ["Gracia", "Fe", "Esperanza"];
    const bConcepts = ["Roma", "Atenas", "Corinto"];
    const concepts: GraphConcept[] = [
      ...aConcepts.map((name) => ({ id: `con_${name}`, name, conceptType: null, mentions: 3, documents: 3 })),
      ...bConcepts.map((name) => ({ id: `con_${name}`, name, conceptType: null, mentions: 3, documents: 3 })),
      { id: "con_Solitario", name: "Solitario", conceptType: null, mentions: 1, documents: 1 },
    ];
    const edges: GraphEdge[] = [];
    // Every "a" concept appears in all three of ver_1..ver_3, every "b"
    // concept in all three of ver_4..ver_6 — disjoint book sets, so the only
    // way the two groups could end up together is a bug, not a shared book.
    for (const version of ["ver_1", "ver_2", "ver_3"]) {
      for (const name of aConcepts) edges.push({ versionId: version, conceptId: `con_${name}`, mentions: 2, confidence: 0.9 });
    }
    for (const version of ["ver_4", "ver_5", "ver_6"]) {
      for (const name of bConcepts) edges.push({ versionId: version, conceptId: `con_${name}`, mentions: 2, confidence: 0.9 });
    }
    edges.push({ versionId: "ver_7", conceptId: "con_Solitario", mentions: 1, confidence: 0.9 });
    return envelope({ documents, concepts, edges, minDocuments: 1 });
  }

  it("puts a book's concepts nowhere, and every concept somewhere", () => {
    const index = buildIndex(twoCliques());
    const sub = subgraphAt(index, 1);
    const { cluster } = clusterConcepts(index, sub, 2);
    for (let i = 0; i < index.docCount; i += 1) expect(cluster[i]).toBe(-1);
    for (let i = index.docCount; i < index.ids.length; i += 1) expect(cluster[i]).toBeGreaterThanOrEqual(0);
  });

  it("groups concepts that co-occur, apart from concepts that do not", () => {
    const index = buildIndex(twoCliques());
    const sub = subgraphAt(index, 1);
    const { cluster, count } = clusterConcepts(index, sub, 2);
    const at = (name: string) => cluster[index.ids.indexOf(`con_${name}`)];

    // 3, not 2: "Solitario" shares no book with anything, so the true number
    // of connected components is 3 (clique A, clique B, Solitario) and a
    // target below that is unreachable — the next test asserts that directly.
    expect(count).toBe(3);
    expect(at("Gracia")).toBe(at("Fe"));
    expect(at("Fe")).toBe(at("Esperanza"));
    expect(at("Roma")).toBe(at("Atenas"));
    expect(at("Atenas")).toBe(at("Corinto"));
    expect(at("Gracia")).not.toBe(at("Roma"));
  });

  it("never merges a concept with no shared book, however small the target", () => {
    // "Solitario" is the only concept in ver_5, so it has no co-occurrence
    // edge at all — asking for one cluster cannot manufacture one.
    const index = buildIndex(twoCliques());
    const sub = subgraphAt(index, 1);
    const { cluster, count } = clusterConcepts(index, sub, 1);
    const at = (name: string) => cluster[index.ids.indexOf(`con_${name}`)];
    expect(count).toBeGreaterThan(1);
    expect(at("Solitario")).not.toBe(at("Gracia"));
    expect(at("Solitario")).not.toBe(at("Roma"));
  });

  it("asks for more clusters than there are concepts and gets exactly one each", () => {
    const index = buildIndex(twoCliques());
    const sub = subgraphAt(index, 1);
    const { cluster, count } = clusterConcepts(index, sub, 100);
    const conceptCount = index.ids.length - index.docCount;
    expect(count).toBe(conceptCount);
    const seen = new Set<number>();
    for (let i = index.docCount; i < index.ids.length; i += 1) {
      expect(seen.has(cluster[i] as number)).toBe(false);
      seen.add(cluster[i] as number);
    }
  });

  it("is deterministic: the same subgraph clusters the same way every time", () => {
    const index = buildIndex(twoCliques());
    const sub = subgraphAt(index, 1);
    const first = clusterConcepts(index, sub, 2);
    const second = clusterConcepts(index, sub, 2);
    expect([...second.cluster]).toEqual([...first.cluster]);
  });

  it("leaves a concept the threshold excluded uncoloured, not merged into a cluster", () => {
    const index = buildIndex(twoCliques());
    // At threshold 2, "Solitario" (documents: 1) drops out of the subgraph
    // entirely — a fact `subgraphAt` already owns, and `clusterConcepts` must
    // agree with it rather than clustering a node nothing else draws.
    const sub = subgraphAt(index, 2);
    const { cluster } = clusterConcepts(index, sub, 2);
    expect(cluster[index.ids.indexOf("con_Solitario")]).toBe(-1);
  });

  // Reproduces the failure a real library actually hit: 268 concepts drawn,
  // and an unbounded version of this algorithm put 893 of a related 903 into
  // one cluster and left nine of one or two — "cut the lightest edges" cuts
  // only the handful of truly isolated concepts when almost every pair
  // co-occurs at *some* weight, which a dense corpus is. This fixture builds
  // exactly that shape: every concept shares a book with several others (so
  // the graph is one connected mass, not several separable regions) plus a
  // few concepts that share nothing with anything.
  function denseCorpus(conceptCount: number, isolatedCount: number): LibraryGraph {
    const bookCount = 20;
    const documents: GraphDocument[] = Array.from({ length: bookCount }, (_, i) => ({
      documentId: `d${i}`,
      versionId: `ver_${i}`,
      title: `Libro ${i}`,
      format: "pdf",
    }));
    const concepts: GraphConcept[] = [];
    const edges: GraphEdge[] = [];
    for (let i = 0; i < conceptCount; i += 1) {
      const id = `con_${i}`;
      concepts.push({ id, name: `Concepto ${i}`, conceptType: null, mentions: 3, documents: 3 });
      // Three books per concept, spread by a coprime stride so pairs overlap
      // heavily without every concept sharing the exact same three books.
      for (const offset of [0, 7, 13]) {
        const book = (i + offset) % bookCount;
        edges.push({ versionId: `ver_${book}`, conceptId: id, mentions: 1, confidence: 0.9 });
      }
    }
    for (let i = 0; i < isolatedCount; i += 1) {
      const id = `con_solo_${i}`;
      concepts.push({ id, name: `Solo ${i}`, conceptType: null, mentions: 1, documents: 1 });
      documents.push({ documentId: `di${i}`, versionId: `ver_solo_${i}`, title: `Solo ${i}`, format: "pdf" });
      edges.push({ versionId: `ver_solo_${i}`, conceptId: id, mentions: 1, confidence: 0.9 });
    }
    return envelope({ documents, concepts, edges, minDocuments: 1 });
  }

  it("does not collapse a densely connected corpus into one giant cluster", () => {
    const index = buildIndex(denseCorpus(268, 8));
    const sub = subgraphAt(index, 1);
    const total = 268 + 8;
    const { cluster, count } = clusterConcepts(index, sub, 10);

    const sizes = new Map<number, number>();
    for (let i = index.docCount; i < index.ids.length; i += 1) {
      const c = cluster[i] as number;
      sizes.set(c, (sizes.get(c) ?? 0) + 1);
    }
    const largest = Math.max(...sizes.values());

    expect(count).toBeGreaterThan(1);
    // The old, uncapped algorithm put 893 of 903 comparable concepts (99%)
    // into one cluster on the real library; the cap keeps any one cluster at
    // or under 15% of the total (`SIZE_SLACK` applied to a 10-way split).
    expect(largest).toBeLessThanOrEqual(Math.ceil((total / 10) * 1.5));
  });
});
