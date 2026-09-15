import { describe, expect, it } from "vitest";

import type { GraphConcept, GraphDocument, GraphEdge, LibraryGraph } from "./api";
import { clusterConcepts } from "./graphClusters";
import { buildIndex, subgraphAt } from "./graphModel";

/**
 * The clustering, asserted as numbers.
 *
 * The load-bearing test is the last one: it builds the shape that broke the
 * first two versions of this — a dense corpus where almost every concept
 * co-occurs with many others — and asserts that no cluster runs away with the
 * whole library. Both earlier attempts passed every other test here and failed
 * that one on the real data.
 */

function envelope(over: Partial<LibraryGraph> = {}): LibraryGraph {
  return {
    libraryId: "lib_a",
    semantic: true,
    confidenceFloor: 0.6,
    minDocuments: 1,
    documents: [],
    concepts: [],
    edges: [],
    truncated: { documents: false, edges: false },
    ...over,
  };
}

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

  it("asks for more clusters than the graph has and gets the ones that exist, not filler", () => {
    // A target the structure cannot supply is not a licence to split concepts
    // that belong together: this fixture holds three communities — the two
    // cliques and the pair nothing else reaches — and asking for a hundred
    // returns those three rather than seven concepts in seven colours.
    const index = buildIndex(twoCliques());
    const sub = subgraphAt(index, 1);
    const { cluster, count } = clusterConcepts(index, sub, 100);
    const at = (name: string) => cluster[index.ids.indexOf(`con_${name}`)];

    expect(count).toBe(3);
    expect(at("Gracia")).toBe(at("Esperanza"));
    expect(at("Roma")).toBe(at("Corinto"));
    expect(at("Gracia")).not.toBe(at("Roma"));
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
    const drawn = [...sizes.values()].sort((a, b) => b - a);

    // The version that shipped before this one put 893 of 903 comparable
    // concepts — 99% — into a single cluster on the real library, and left
    // nine holding one or two. Both ends are asserted: no cluster runs away
    // with the corpus, and none of the ten colours is spent on a leftover.
    //
    // A quarter rather than `SIZE_SLACK`'s exact bound, because the cap
    // governs *merges* and cannot split a community Louvain already found —
    // the real library's largest is 110 of 903 (12%) against a cap of 104,
    // and this fixture's is 40 of 276 (14%). Both are the shape being
    // asserted; 99% is not.
    expect(count).toBeGreaterThan(1);
    expect(drawn[0] as number).toBeLessThan(total / 4);
    // Exactly the eight the fixture built with no shared book at all. Those
    // are singletons by construction and must stay so; what must *not* happen
    // is a connected concept stranded alone because the merge ran out of room
    // — which is the other half of the failure this replaced, where nine of
    // the ten colours went to leftovers.
    expect(drawn.filter((n) => n === 1)).toHaveLength(8);
  });

  it("stays linear in the edges, not quadratic in a book's concepts", () => {
    // The reason this is a *test* and not a comment: the first version built
    // one weighted pair per co-occurring concept pair, which on the real
    // library is 7,296,901 pairs at the widest threshold — 2.6 s to build and
    // 3.2 s to cluster, inside a render. A book with 2,000 concepts is 2,000
    // edges here and would be 2,000,000 pairs there, so a reimplementation
    // that reintroduces the projection blows this budget by orders of
    // magnitude rather than by a little.
    const bookCount = 5;
    const perBook = 2_000;
    const documents: GraphDocument[] = Array.from({ length: bookCount }, (_, i) => ({
      documentId: `d${i}`,
      versionId: `ver_${i}`,
      title: `Libro ${i}`,
      format: "pdf",
    }));
    const concepts: GraphConcept[] = [];
    const edges: GraphEdge[] = [];
    for (let i = 0; i < perBook; i += 1) {
      for (let b = 0; b < bookCount; b += 1) {
        const id = `con_${b}_${i}`;
        if (b === 0) {
          // Every concept sits in two books, so the projection would pair it
          // with every other concept of both.
          concepts.push({ id, name: `C${i}`, conceptType: null, mentions: 2, documents: 2 });
        }
      }
    }
    for (let i = 0; i < perBook; i += 1) {
      const id = `con_0_${i}`;
      edges.push({ versionId: `ver_${i % bookCount}`, conceptId: id, mentions: 1, confidence: 0.9 });
      edges.push({ versionId: `ver_${(i + 1) % bookCount}`, conceptId: id, mentions: 1, confidence: 0.9 });
    }

    const index = buildIndex(envelope({ documents, concepts, edges, minDocuments: 1 }));
    const sub = subgraphAt(index, 1);
    const started = Date.now();
    const { count } = clusterConcepts(index, sub, 10);
    const elapsed = Date.now() - started;

    expect(count).toBeGreaterThan(0);
    expect(elapsed).toBeLessThan(1_000);
  });
});
