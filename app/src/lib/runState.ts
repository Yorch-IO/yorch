/**
 * How a run's state paints, in one place.
 *
 * Lifted out of `HomeScreen` when the queue and the audit ledger started
 * rendering the same states: three screens choosing their own colour for
 * `blocked` is three chances for the row a reader must notice to be the one that
 * looks least like anything.
 *
 * Deliberately **not** the Services screen's container vocabulary. A stopped
 * container is neither good nor bad; reusing its grey for a failed run is the
 * mistake this mapping exists to avoid.
 */

/** The states a run can be in and no longer be going.
 *
 *  Both spellings of cancelled, because Temporal answers `canceled` and the
 *  catalog stores `cancelled`, and the queue reads whichever it is handed. */
export const FINISHED: ReadonlySet<string> = new Set([
  "succeeded",
  "completed",
  "failed",
  "cancelled",
  "canceled",
  "terminated",
  "timed_out",
  "blocked",
]);

/**
 * `blocked` is `--warn`, not `--bad`, and that is the whole reason this moved.
 *
 * A blocked run did every stage and paid for them; it withheld only the
 * activation, and the way out costs nothing because the index already exists.
 * Painting it red sends somebody to look for a broken run, when what is needed
 * is one click on *Activar*. `awaiting_approval` is the same colour for the
 * same reason: it is the row that will do nothing until a person acts.
 */
export function runDot(state: string | null): string {
  if (state === "succeeded" || state === "completed") return "dot-ok";
  if (state === "running") return "dot-running";
  if (state === "awaiting_approval" || state === "blocked") return "dot-warn";
  if (
    state === "failed" ||
    state === "cancelled" ||
    state === "canceled" ||
    state === "terminated" ||
    state === "timed_out"
  )
    return "dot-bad";
  return "dot-absent";
}

/** How long a stage took, at a precision a person can read.
 *
 *  Sub-second stages are the free ones and their exact millisecond count is
 *  noise; a stage measured in minutes is the one somebody is looking at. Null
 *  renders as nothing at all rather than as `0s`: a stage with no end is either
 *  still running or is the terminal instant, and both are said elsewhere in the
 *  row. */
export function duration(seconds: number | null): string | null {
  if (seconds === null) return null;
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes} min ${rest} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}
