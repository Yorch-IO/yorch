import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

import { api, type LibraryGraph } from "./api";
import { useBackend } from "./backend";
import {
  buildAdjacency,
  buildIndex,
  histogram,
  type Adjacency,
  type GraphIndex,
  type ThresholdCount,
} from "./graphModel";
import type { GraphConcept, GraphDocument } from "./api";

/**
 * One download per (plane, library, confidence floor), held in memory.
 *
 * **What is fetched and what is not.** The degree control used to be a server
 * filter, so every change was a round trip. It is derived on the client now —
 * see `graphModel.ts` for why that is an equality and not an approximation — so
 * the request is always `min_documents = 1`, the widest envelope, and every
 * threshold is a view of it. The confidence floor stays a server filter, because
 * a concept's degree is counted *after* the floor predicate
 * (`graph/queries.py`), so no client can re-derive it; each floor therefore gets
 * its own entry here and the second visit to one costs nothing.
 *
 * Measured on the real library 2026-09-01 (73 books): the envelope is 17,814
 * rows, 3.48 MB of JSON, 183 ms end to end, and `JSON.parse` of it is 10.5 ms.
 * Deriving a threshold's view from it afterwards is 0.88 ms.
 *
 * **Not localStorage.** 3.48 MB does not fit a quota, and it does not need to:
 * the thing is cheap to fetch again. This is a cache because refetching is
 * *wasteful*, not because it is expensive — which is the opposite of the reason
 * the Ask history is persisted, and worth saying so nobody "improves" it.
 *
 * **A module, not a context.** `App.tsx` keys every screen on the plane identity
 * and remounts them when it changes, so a provider mounted inside that key would
 * die exactly when the cache is most valuable, and one mounted above it would be
 * a fourth global provider for one screen. A module survives the remount — which
 * is the feature — and the identity is in the key, which is what stops it being
 * a leak between organisations.
 */

/** How many envelopes are held at once.
 *
 *  Measured net cost per entry on the real library: about 3.7 MB — the response
 *  objects are read once into typed arrays and dropped, which is where 2.5 MB of
 *  uninterned edge id strings goes. Four is "the floor you are on plus the three
 *  you tried" (`FLOORS` offers five) at roughly 15 MB, which is a fair share of a
 *  webview that is also holding the canvas. */
const CAPACITY = 4;

/** The envelope is always the widest one. A threshold is a view, never a
 *  request. */
const ENVELOPE_THRESHOLD = 1;

export interface GraphEntry {
  key: string;
  identity: string;
  libraryId: string;
  floor: number;
  status: "loading" | "ready" | "failed";
  error: unknown;
  index: GraphIndex | null;
  adjacency: Adjacency | null;
  counts: readonly ThresholdCount[];
  /** For the detail panes, which need names rather than numbers. */
  concepts: ReadonlyMap<string, GraphConcept>;
  documents: ReadonlyMap<string, GraphDocument>;
  /** True when the response itself was cut, which is a different question from
   *  whether a given threshold's view was. */
  truncatedDocuments: boolean;
  touched: number;
}

/**
 * The plane is in the key unconditionally, and deliberately *not* through
 * `scopedKey`.
 *
 * That helper leaves the local plane's key bare, because it names localStorage
 * entries that existed before scoping did and would have been orphaned. Nothing
 * here has anything to orphan, and a cache that let two planes collide would
 * show one organisation's library to another.
 */
export function graphKey(identity: string, libraryId: string, floor: number): string {
  // `floor` arrives from a `<select>` as `Number(value)`, so it is normalised
  // rather than trusted: 0.30000000000000004 must not become a second entry.
  return `${identity}|${libraryId}|${floor.toFixed(2)}`;
}

const cache = new Map<string, GraphEntry>();
const listeners = new Set<() => void>();
let clock = 0;

function announce(): void {
  for (const listener of listeners) listener();
}

function put(entry: GraphEntry): void {
  clock += 1;
  cache.set(entry.key, { ...entry, touched: clock });
  evictBeyondCapacity();
  announce();
}

function evictBeyondCapacity(): void {
  while (cache.size > CAPACITY) {
    let oldestKey: string | null = null;
    let oldest = Infinity;
    for (const [key, entry] of cache) {
      if (entry.touched < oldest) {
        oldest = entry.touched;
        oldestKey = key;
      }
    }
    if (oldestKey === null) return;
    cache.delete(oldestKey);
  }
}

/** Everything belonging to a plane that is no longer the one on screen. It can
 *  never be wanted again — `App` has remounted the screens — and holding it is
 *  pure waste. */
export function evictOtherIdentities(identity: string): void {
  let removed = false;
  for (const [key, entry] of cache) {
    if (entry.identity !== identity) {
      cache.delete(key);
      removed = true;
    }
  }
  if (removed) announce();
}

/** After an import, a rebuild or a removal changes the projection. Dropping the
 *  entry is enough: the next read refetches. */
export function invalidateLibrary(identity: string, libraryId: string): void {
  let removed = false;
  for (const [key, entry] of cache) {
    if (entry.identity === identity && entry.libraryId === libraryId) {
      cache.delete(key);
      removed = true;
    }
  }
  if (removed) announce();
}

/** Tests only. The cache outlives a render tree on purpose, so a test file that
 *  touches it calls this beside its own `cleanup()`. */
export function clearGraphCache(): void {
  cache.clear();
  clock = 0;
  announce();
}

const inFlight = new Set<string>();

async function load(identity: string, libraryId: string, floor: number): Promise<void> {
  const key = graphKey(identity, libraryId, floor);
  if (inFlight.has(key)) return;
  inFlight.add(key);

  const base: GraphEntry = {
    key,
    identity,
    libraryId,
    floor,
    status: "loading",
    error: null,
    index: null,
    adjacency: null,
    counts: [],
    concepts: new Map(),
    documents: new Map(),
    truncatedDocuments: false,
    touched: 0,
  };
  // A refetch of a key that already answered keeps its graph on screen: a
  // failure at a new floor must not blank a picture that was correct, which is
  // the rule the screen already followed for its own refetch.
  const previous = cache.get(key);
  put(previous === undefined ? base : { ...previous, status: "loading", error: null });

  try {
    const data: LibraryGraph = await api.libraryGraph(libraryId, floor, ENVELOPE_THRESHOLD);
    const index = buildIndex(data);
    put({
      ...base,
      status: "ready",
      index,
      adjacency: buildAdjacency(index),
      counts: histogram(index),
      concepts: new Map(data.concepts.map((c) => [c.id, c])),
      documents: new Map(data.documents.map((d) => [d.versionId, d])),
      truncatedDocuments: data.truncated.documents,
    });
  } catch (error) {
    const held = cache.get(key);
    put(held === undefined ? { ...base, status: "failed", error } : { ...held, status: "failed", error });
  } finally {
    inFlight.delete(key);
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export interface LibraryGraphState extends GraphEntry {
  /** Drop this envelope and read it again — what "reset view" deliberately is
   *  not, and the only fully reliable answer to a projection that changed. */
  refresh: () => void;
}

const EMPTY: Omit<GraphEntry, "key" | "identity" | "libraryId" | "floor"> = {
  status: "loading",
  error: null,
  index: null,
  adjacency: null,
  counts: [],
  concepts: new Map(),
  documents: new Map(),
  truncatedDocuments: false,
  touched: 0,
};

/**
 * The envelope for one library at one floor, fetched at most once.
 *
 * `active` is what keeps the graph off the critical path of a cold launch. All
 * seven screens are mounted at startup and both graph views with them, so
 * without it this fetch and the layout behind it run on a tab nobody has opened.
 * It latches: arriving is the trigger, and leaving must never throw work away.
 */
export function useLibraryGraph(opts: {
  libraryId: string;
  floor: number;
  active: boolean;
}): LibraryGraphState {
  const { libraryId, floor, active } = opts;
  const { identity } = useBackend();
  const key = graphKey(identity, libraryId, floor);
  // The last envelope that answered, for this library, on this plane.
  //
  // A failed read at a new floor must not blank a picture that was correct a
  // moment ago — the rule the screen already followed when it held one response
  // in state. Per-floor keys took that away, because the floor being asked for
  // is a different entry with nothing in it, so the fallback is explicit now.
  // Scoped to the same library and plane: showing one shelf's graph while
  // another's request fails would be worse than showing nothing.
  const lastGood = useRef<GraphEntry | null>(null);

  const entry = useSyncExternalStore(
    subscribe,
    () => cache.get(key) ?? null,
    () => null,
  );

  useEffect(() => {
    if (!active || libraryId === "") return;
    if (cache.has(key)) return;
    void load(identity, libraryId, floor);
  }, [active, identity, libraryId, floor, key]);

  useEffect(() => {
    evictOtherIdentities(identity);
  }, [identity]);

  const refresh = useCallback(() => {
    cache.delete(key);
    void load(identity, libraryId, floor);
  }, [identity, key, libraryId, floor]);

  if (entry !== null && entry.index !== null) {
    lastGood.current = entry;
    return { ...entry, refresh };
  }
  const held = lastGood.current;
  const stale =
    held !== null && held.identity === identity && held.libraryId === libraryId
      ? held
      : null;
  const shell = entry ?? { key, identity, libraryId, floor, ...EMPTY };
  if (stale === null) return { ...shell, refresh };
  // The state and the error are the current read's; everything drawable is the
  // one that worked.
  return {
    ...stale,
    key,
    floor,
    status: shell.status,
    error: shell.error,
    refresh,
  };
}
