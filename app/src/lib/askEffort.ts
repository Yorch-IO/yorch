/**
 * How hard a question is asked, as a level rather than as a set of numbers.
 *
 * The names are the whole contract. What each one costs — how many passages
 * reach the model, how wide the search is, how much the model may reason before
 * answering — is decided by the worker in `answering/effort.py`, and
 * deliberately not mirrored here. A client that sent numbers could ask for two
 * hundred passages on a paid call, and both control planes would then have to
 * police a figure neither of them owns.
 *
 * It lives in its own module rather than in `askSession.ts` because `api.ts`
 * needs the type for the request and the answer, and `askSession.ts` already
 * imports `api.ts` — putting it there would make the two import each other.
 */
import { scopedKey } from "./backend";

/**
 * The levels, in the order a control draws them. Kept equal to `EFFORT_LEVELS`
 * in `worker/brainworker/answering/effort.py`, which is the list both control
 * planes validate against; a name this app sends that the server does not know
 * is refused rather than quietly downgraded.
 */
export const ASK_EFFORTS = ["brief", "standard", "thorough"] as const;

export type AskEffort = (typeof ASK_EFFORTS)[number];

/**
 * Kept equal to `DEFAULT_EFFORT` on the server, and that equality is the reason
 * adopting the control was safe: this is the behaviour every question had
 * before the levels existed, so a user who never touches the control gets
 * exactly what they always got.
 */
export const DEFAULT_ASK_EFFORT: AskEffort = "standard";

export function isAskEffort(value: unknown): value is AskEffort {
  return ASK_EFFORTS.includes(value as AskEffort);
}

// Its own key rather than a field inside the history, because the two answer
// different questions: the history is what you asked, this is how you prefer to
// ask. Clearing one must not clear the other.
const STORED = "companyBrain.askEffort";

/**
 * Scoped on the plane for the same reason the history is: a level is a
 * preference about one organisation's corpus, and `scopedKey` leaves the local
 * plane's key bare so nothing already stored is orphaned.
 */
export function loadEffort(identity: string): AskEffort {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(scopedKey(STORED, identity));
  } catch {
    // Storage denied outright, as some webview configurations do. Falling back
    // to the default is the whole recovery: nothing is lost but a preference.
    return DEFAULT_ASK_EFFORT;
  }
  // Validated rather than trusted. A level written by an older build, or by
  // hand, must not travel to the server to be refused there — the request would
  // fail with a validation error and the user would have no idea why.
  return isAskEffort(raw) ? raw : DEFAULT_ASK_EFFORT;
}

export function saveEffort(effort: AskEffort, identity: string): void {
  try {
    window.localStorage.setItem(scopedKey(STORED, identity), effort);
  } catch {
    // Over quota, or storage denied. Not remembering the choice is not worth an
    // error in front of the answer being read.
  }
}
