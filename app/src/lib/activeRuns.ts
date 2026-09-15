/**
 * Which runs are happening right now, for the shell to say so.
 *
 * The problem this exists for: an import is started on one screen and then the
 * person goes somewhere else, and nothing anywhere says it is still going. A
 * real ingest ran 598 chunks of semantic extraction — an hour, and a projected
 * $3.87 — while every screen that mentioned it said `awaiting_approval`, because
 * that is what the catalog held.
 *
 * **Two sources, because neither is enough alone.**
 *
 * `/project-summary` is discovery: it is the only call that answers "what runs
 * exist" without being told an id, it is already served by both planes, and it
 * reads the catalog only — so it keeps working when Temporal does not, which is
 * the property the landing screen was built around. It carries what a run has
 * been billed.
 *
 * `/runs/{id}` is the detail, asked only about runs that look live. It is the
 * one that reaches Temporal, so it knows whether a run is *really* running and
 * how far the current activity has got. Asking it per run is the cost of the
 * truth; asking it for finished runs would be paying that cost for nothing.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { api, type RunProgress, type RunSummary } from "./api";

/** How often to re-ask. Slow enough not to matter, fast enough that a stage
 *  change is noticed before a person wonders. Semantic extraction moves about
 *  one chunk every five seconds, so this is roughly one chunk per poll. */
const POLL_MS = 5000;

export interface ActiveRun {
  workflowId: string;
  title: string | null;
  kind: string;
  /** From the catalog, or from Temporal when that answered. */
  stage: string | null;
  /** Live from Temporal. `null` means nobody could say, which must read as
   *  "keep waiting" and never as "failed" — an old plane, or a run whose
   *  history has aged out. */
  state: string | null;
  progress: RunProgress | null;
  usdSoFar: number | null;
}

/** A run the catalog thinks has not finished.
 *
 *  `finishedAt` rather than `state`, deliberately. The state column is written
 *  by the workflow and a run that died without recording an outcome keeps
 *  whatever it had; the timestamp is only ever set by `finish_run`, so its
 *  absence is the more honest question — and anything it lets through is then
 *  checked against Temporal, which settles it. */
function unfinished(run: RunSummary): boolean {
  return run.finishedAt === null;
}

/** Terminal states, so a run Temporal has settled disappears at once rather
 *  than at the next catalog write. */
const OVER = new Set(["completed", "failed", "canceled", "cancelled", "terminated", "timed_out"]);

export function useActiveRuns(): ActiveRun[] {
  const [runs, setRuns] = useState<ActiveRun[]>([]);
  // A ref, not state: the effect below must not re-subscribe when a poll lands.
  const alive = useRef(true);

  const poll = useCallback(async () => {
    let candidates: RunSummary[];
    try {
      const summary = await api.projectSummary();
      // `null` is "the catalog could not be read", which is not "nothing is
      // running" — so the indicator holds what it had rather than claiming the
      // work stopped.
      if (summary.recentRuns === null) return;
      candidates = summary.recentRuns.filter(unfinished);
    } catch {
      // The shell must never break over this. A control plane that is down is
      // reported by the screens that are about it; here it means "no news".
      return;
    }

    const detailed = await Promise.all(
      candidates.map(async (run): Promise<ActiveRun | null> => {
        try {
          const live = await api.runStatus(run.workflowId);
          if (live.state !== null && OVER.has(live.state)) return null;
          return {
            workflowId: run.workflowId,
            title: run.title,
            kind: run.kind,
            stage: live.stage ?? run.stage,
            state: live.state,
            progress: live.progress,
            usdSoFar: run.usdSoFar,
          };
        } catch {
          // Temporal could not be asked. The catalog still says this run has
          // not finished, and dropping it would tell the user it had.
          return {
            workflowId: run.workflowId,
            title: run.title,
            kind: run.kind,
            stage: run.stage,
            state: null,
            progress: null,
            usdSoFar: run.usdSoFar,
          };
        }
      }),
    );

    if (alive.current) setRuns(detailed.filter((r): r is ActiveRun => r !== null));
  }, []);

  useEffect(() => {
    alive.current = true;
    void poll();
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
    };
  }, [poll]);

  return runs;
}
