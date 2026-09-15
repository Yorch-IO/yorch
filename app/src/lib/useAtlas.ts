import { useEffect, useState } from "react";

import {
  ATLAS_STEPS,
  startAtlas,
  type AtlasJob,
  type AtlasMessage,
} from "./graphAtlas";
import type { GraphIndex } from "./graphModel";

/**
 * The chain of layouts for one envelope, collected as it arrives.
 *
 * Kept out of `graphAtlas.ts` on purpose: **that module is imported by the
 * worker**, and a worker bundle that pulls in React is a bundle that fails to
 * load for a reason nobody will look for.
 */

/** A layout and the grouping it was laid out by, kept together on purpose:
 *  the colours a reader sees have to be the same clusters the simulation
 *  separated, and `chooseLayout` may answer a threshold with a wider one's
 *  layout while the chain is still running. Two parallel maps would let the
 *  picture and the legend fall a step apart during exactly that window. */
export interface AtlasLayout {
  xy: Float32Array;
  clusters: Int32Array;
}

export interface AtlasState {
  /** One computed layout per threshold that has been reached. */
  layouts: ReadonlyMap<number, AtlasLayout>;
  done: number;
  total: number;
  failed: string | null;
}

const EMPTY: AtlasState = {
  layouts: new Map(),
  done: 0,
  total: ATLAS_STEPS,
  failed: null,
};

/**
 * Which layout to draw a threshold with when its own is not computed yet.
 *
 * A layout for threshold `t` holds a position for every node of degree ≥ `t`,
 * so it can stand in for any threshold at or above it — everything drawn is
 * placed, and nothing moves, which is the behaviour a filter should have while
 * the chain is still running. Below it there would be nodes with no position,
 * so a *higher* threshold's layout is the last resort rather than the first.
 */
export function chooseLayout(
  layouts: ReadonlyMap<number, AtlasLayout>,
  threshold: number,
): AtlasLayout | null {
  const exact = layouts.get(threshold);
  if (exact !== undefined) return exact;

  let best: number | null = null;
  for (const t of layouts.keys()) {
    if (t > threshold) continue;
    if (best === null || t > best) best = t;
  }
  if (best !== null) return layouts.get(best) ?? null;

  // Nothing wide enough exists yet. Draw with the widest thing there is and
  // leave the unplaced nodes out, rather than showing nothing at all.
  let widest: number | null = null;
  for (const t of layouts.keys()) if (widest === null || t < widest) widest = t;
  return widest === null ? null : layouts.get(widest) ?? null;
}

/**
 * Runs the chain once per envelope, and never while the tab is hidden.
 *
 * The whole chain is about six seconds of work on the real library, so it is
 * started eagerly the moment the graph is on show and left to finish: after it
 * lands every threshold is a lookup. Changing library or floor produces a new
 * index, which cancels the job in flight — a `terminate`, because a `settle` of
 * the whole library runs for seconds and cannot observe a message while it does.
 */
export function useAtlas(index: GraphIndex | null, active: boolean): AtlasState {
  const [state, setState] = useState<AtlasState>(EMPTY);

  useEffect(() => {
    if (index === null || !active) return;
    setState(EMPTY);

    let job: AtlasJob | null = null;
    let live = true;
    const request = {
      job: Date.now(),
      ids: index.ids,
      docCount: index.docCount,
      degree: index.degree,
      edgeSrc: index.edgeSrc,
      edgeDst: index.edgeDst,
      edgeMentions: index.edgeMentions,
    };

    const onMessage = (message: AtlasMessage) => {
      if (!live) return;
      if (message.kind === "failed") {
        setState((was) => ({ ...was, failed: message.message }));
        return;
      }
      if (message.kind !== "layout") return;
      setState((was) => {
        const layouts = new Map(was.layouts);
        layouts.set(message.threshold, { xy: message.xy, clusters: message.clusters });
        return { ...was, layouts, done: message.done, total: message.total };
      });
    };

    job = startAtlas(request, onMessage);
    return () => {
      live = false;
      job?.cancel();
    };
  }, [index, active]);

  return state;
}
