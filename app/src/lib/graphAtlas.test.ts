import { describe, expect, it, vi } from "vitest";

import type { GraphConcept, GraphDocument, GraphEdge, LibraryGraph } from "./api";
import {
  ATLAS_STEPS,
  ATLAS_VIEW,
  atlasSteps,
  BASE_THRESHOLD,
  startAtlas,
  type AtlasMessage,
  type AtlasRequest,
} from "./graphAtlas";
import { buildIndex, DEFAULT_THRESHOLD, THRESHOLDS } from "./graphModel";

/**
 * The chain of layouts, which is the only place its properties can be checked.
 *
 * jsdom defines no `Worker` — asserted below rather than assumed — so every
 * test here drives `atlasSteps` through the same inline path a webview without
 * one would take. That is the point: the worker file is a message pump with no
 * logic in it precisely because this is where the logic is tested.
 */

function library(over: Partial<LibraryGraph> = {}): LibraryGraph {
  const documents: GraphDocument[] = [];
  for (let i = 0; i < 6; i += 1) {
    documents.push({ documentId: `doc_${i}`, versionId: `ver_${i}`, title: `Libro ${i}`, format: "pdf" });
  }
  const concepts: GraphConcept[] = [];
  const edges: GraphEdge[] = [];
  // One concept at each stop and a few below the lowest, so every threshold has
  // something to place and something to leave out.
  const degrees = [1, 1, 1, 2, 2, 3, 3, 5, 5, 6];
  degrees.forEach((degree, i) => {
    concepts.push({ id: `con_${i}`, name: `Concepto ${i}`, conceptType: null, mentions: degree, documents: degree });
    for (let d = 0; d < degree; d += 1) {
      edges.push({ versionId: `ver_${(i + d) % 6}`, conceptId: `con_${i}`, mentions: 1 + (i % 3), confidence: 0.9 });
    }
  });
  return {
    libraryId: "lib_a",
    semantic: true,
    confidenceFloor: 0.6,
    minDocuments: 1,
    documents,
    concepts,
    edges,
    truncated: { documents: false, edges: false },
    ...over,
  };
}

/** The real corpus's shape at a size a test suite can afford: 620 nodes and
 *  1,024 edges settle in about 145 ms. The degree distribution is the measured
 *  one — most concepts reach one book and a short tail reaches many — because
 *  the property under test is about how the *surviving* set moves, and a flat
 *  distribution has no tail to survive. */
function distributed(): LibraryGraph {
  const documents: GraphDocument[] = [];
  for (let i = 0; i < 20; i += 1) {
    documents.push({ documentId: `doc_${i}`, versionId: `ver_${i}`, title: `Libro ${i}`, format: "pdf" });
  }
  const concepts: GraphConcept[] = [];
  const edges: GraphEdge[] = [];
  let c = 0;
  for (const [degree, count] of [[1, 420], [2, 90], [3, 40], [4, 20], [5, 14], [7, 8], [10, 5], [16, 3]]) {
    for (let j = 0; j < (count as number); j += 1) {
      const id = `con_${c}`;
      concepts.push({ id, name: `Concepto ${c}`, conceptType: null, mentions: degree as number, documents: degree as number });
      for (let d = 0; d < (degree as number); d += 1) {
        edges.push({ versionId: `ver_${(c * 7 + d * 13) % 20}`, conceptId: id, mentions: 1 + (c % 5), confidence: 0.9 });
      }
      c += 1;
    }
  }
  return { ...library(), documents, concepts, edges };
}

function requestFor(index: ReturnType<typeof buildIndex>, job = 1): AtlasRequest {
  return {
    job,
    ids: index.ids,
    docCount: index.docCount,
    degree: index.degree,
    edgeSrc: index.edgeSrc,
    edgeDst: index.edgeDst,
    edgeMentions: index.edgeMentions,
  };
}

function request(job = 1): AtlasRequest {
  const index = buildIndex(library());
  return {
    job,
    ids: index.ids,
    docCount: index.docCount,
    degree: index.degree,
    edgeSrc: index.edgeSrc,
    edgeDst: index.edgeDst,
    edgeMentions: index.edgeMentions,
  };
}

function collect(req: AtlasRequest): AtlasMessage[] {
  return [...atlasSteps(req)];
}

function layouts(messages: AtlasMessage[]) {
  return messages.filter((m): m is Extract<AtlasMessage, { kind: "layout" }> => m.kind === "layout");
}

describe("the chain", () => {
  it("gives the default threshold first, so there is a picture before the rest", () => {
    // The widest layout is seconds of work on the real library; the default is
    // 364 ms of it. Emitting it first is what makes that the time to a picture
    // rather than the time to the whole atlas.
    const rows = layouts(collect(request()));
    expect(rows[0]?.threshold).toBe(DEFAULT_THRESHOLD);
  });

  it("emits each threshold once, because none stands in for another", () => {
    // Under the ladder the default was emitted twice: once cold and once again
    // once the base existed, since the cold one did not belong to the chain.
    // Every layout is settled cold now, so there is nothing to supersede.
    const rows = layouts(collect(request()));
    const seen = rows.map((r) => r.threshold).sort((a, b) => a - b);
    expect(seen).toEqual([...THRESHOLDS].sort((a, b) => a - b));
  });

  it("leaves the widest threshold for last, because it costs an order of magnitude more", () => {
    // ~8.8 s against 364 ms for the default on the real library. Ordering by
    // cost is what makes every threshold but "every book" ready in about a
    // second and a half.
    const rows = layouts(collect(request()));
    expect(rows[rows.length - 1]?.threshold).toBe(BASE_THRESHOLD);
  });

  it("ends with a layout for every threshold the control offers", () => {
    const rows = layouts(collect(request()));
    const settled = new Map(rows.map((r) => [r.threshold, r]));
    expect([...settled.keys()].sort((a, b) => a - b)).toEqual([...THRESHOLDS].sort((a, b) => a - b));
    expect(rows).toHaveLength(ATLAS_STEPS);
    expect(rows[rows.length - 1]?.done).toBe(ATLAS_STEPS);
  });

  it("says it is finished", () => {
    const messages = collect(request());
    expect(messages[messages.length - 1]).toMatchObject({ kind: "done", job: 1 });
  });
});

describe("what the layouts contain", () => {
  it("places every node the threshold admits, and none it does not", () => {
    const index = buildIndex(library());
    const rows = layouts(collect(request()));
    // Provisional rows included: the first picture is the *default* threshold's
    // node set, and a chain that started from the wrong subgraph would still
    // label its message correctly. The node set is what says which it was.
    for (const row of rows) {
      for (let i = 0; i < index.ids.length; i += 1) {
        const placed = Number.isFinite(row.xy[i * 2]);
        const wanted = i < index.docCount || (index.degree[i] as number) >= row.threshold;
        expect(placed).toBe(wanted);
      }
    }
  });

  it("arrives already fitted to the canvas, so nothing downstream scales it", () => {
    for (const row of layouts(collect(request()))) {
      for (let i = 0; i < row.xy.length; i += 2) {
        const x = row.xy[i] as number;
        const y = row.xy[i + 1] as number;
        if (!Number.isFinite(x)) continue;
        expect(x).toBeGreaterThanOrEqual(ATLAS_VIEW.MARGIN - 1);
        expect(x).toBeLessThanOrEqual(ATLAS_VIEW.W - ATLAS_VIEW.MARGIN + 1);
        expect(y).toBeGreaterThanOrEqual(ATLAS_VIEW.MARGIN - 1);
        expect(y).toBeLessThanOrEqual(ATLAS_VIEW.H - ATLAS_VIEW.MARGIN + 1);
      }
    }
  });

  it("draws each colour as a region rather than sprinkling it over the canvas", () => {
    // **The property the cluster forces exist for**, and the one that replaced
    // the chain's old continuity bound when the ladder was removed — see the
    // module header for the measurement behind that trade.
    //
    // Scored the way a reader reads it: for each concept, is its own group's
    // centre the nearest centre? A colour scattered at random over ten groups
    // scores about 10%; a colour that is a region scores near 100%. Measured on
    // the real library at the default threshold: **30.8% without the cluster
    // forces and 92.6% with them.**
    const data = distributed();
    const index = buildIndex(data);
    const rows = layouts(collect(requestFor(index)));

    for (const row of rows) {
      const centres = new Map<number, { x: number; y: number; n: number }>();
      for (let i = 0; i < index.ids.length; i += 1) {
        const c = row.clusters[i] as number;
        const x = row.xy[i * 2] as number;
        if (c < 0 || !Number.isFinite(x)) continue;
        const centre = centres.get(c) ?? { x: 0, y: 0, n: 0 };
        centre.x += x;
        centre.y += row.xy[i * 2 + 1] as number;
        centre.n += 1;
        centres.set(c, centre);
      }
      if (centres.size < 2) continue;
      for (const centre of centres.values()) {
        centre.x /= centre.n;
        centre.y /= centre.n;
      }

      let home = 0;
      let total = 0;
      for (let i = 0; i < index.ids.length; i += 1) {
        const c = row.clusters[i] as number;
        const x = row.xy[i * 2] as number;
        if (c < 0 || !Number.isFinite(x)) continue;
        const y = row.xy[i * 2 + 1] as number;
        total += 1;
        let best = -1;
        let bestD = Infinity;
        for (const [id, centre] of centres) {
          const d = Math.hypot(x - centre.x, y - centre.y);
          if (d < bestD) {
            bestD = d;
            best = id;
          }
        }
        if (best === c) home += 1;
      }
      // Well above the ~1/k a scattered colour would score, and below the real
      // corpus's 92.6% because this fixture's degrees are synthetic.
      expect(home / total, `threshold ${row.threshold}`).toBeGreaterThan(0.6);
    }
  });

  it("is deterministic, which is what makes a cached position safe to reuse", () => {
    // `App` remounts this screen on a plane change and the cache outlives that,
    // so a layout that differed run to run would move the picture for a reason
    // no reader could see. `force.ts` seeds each body from its own id and sorts
    // before simulating; this asserts the property survives the chain.
    const first = layouts(collect(request()));
    const second = layouts(collect(request()));
    expect(first).toHaveLength(second.length);
    first.forEach((row, i) => {
      expect(Array.from(row.xy)).toEqual(Array.from(second[i]?.xy as Float32Array));
    });
  });
});

describe("where there is no Worker", () => {
  it("has none here, which is why the inline path is the tested one", () => {
    expect(typeof Worker).toBe("undefined");
  });

  it("lays the whole chain out anyway, staged rather than in one block", async () => {
    vi.useFakeTimers();
    try {
      const seen: AtlasMessage[] = [];
      startAtlas(request(), (m) => seen.push(m));
      // Nothing has run yet: the first step is behind a timeout, so a caller
      // that mounts and immediately unmounts pays nothing.
      expect(seen).toHaveLength(0);
      await vi.advanceTimersByTimeAsync(0);
      expect(seen).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(100);
      expect(seen[seen.length - 1]?.kind).toBe("done");
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops when cancelled, without finishing the chain", async () => {
    vi.useFakeTimers();
    try {
      const seen: AtlasMessage[] = [];
      const job = startAtlas(request(), (m) => seen.push(m));
      await vi.advanceTimersByTimeAsync(0);
      const after = seen.length;
      job.cancel();
      await vi.advanceTimersByTimeAsync(100);
      expect(seen).toHaveLength(after);
    } finally {
      vi.useRealTimers();
    }
  });
});
