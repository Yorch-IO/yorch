/**
 * The Ask screen's session history, as a reducer rather than as component
 * state.
 *
 * It lives here for two reasons. The screen used to hold a single `answer`, so
 * asking a second question destroyed the first — and the four properties that
 * matter about the replacement (the newest submission is what you are looking
 * at, earlier ones survive, selecting one restores it, a citation resolves to
 * the passage it rests on) are properties of a function. Testing them here
 * costs nothing and does not depend on anything rendering.
 *
 * History is persisted to localStorage, so closing the window and coming back
 * does not lose what you asked. A question still in flight is persisted too,
 * with the id the API gave it — which is what lets the answer computed while the
 * window was shut be collected on the next launch rather than paid for twice.
 */
import { errorMessage, isAppError, type Answer, type EvidenceItem } from "./api";
import { DEFAULT_ASK_EFFORT, isAskEffort, type AskEffort } from "./askEffort";
import { scopedKey } from "./backend";

export interface AskEntry {
  /** Supplied by the caller, so the reducer needs no clock and no counter of
   *  its own and stays a pure function of its arguments. */
  id: string;
  question: string;
  /** Which library it was asked of. The picker is global and the answer is not:
   *  an entry from before you switched libraries was answered by the other
   *  one, and saying so is cheaper than the confusion. */
  libraryId: string;
  status: "pending" | "done" | "failed";
  /** What the API called this question. Null only between pressing Ask and the
   *  API accepting it; an entry reloaded in that state can never be collected,
   *  so `loadSession` retires it rather than polling forever. */
  questionId: string | null;
  /** Wall clock, for showing how long a slow question has been running. Minutes
   *  of a silent spinner are indistinguishable from a hang. */
  startedAt: number;
  answer: Answer | null;
  error: unknown;
  /** Index into `answer.citations`. Kept per entry, so returning to an earlier
   *  question finds it where you left it rather than reset to the first. */
  citation: number;
  /** The level it was asked at. Recorded per entry rather than read off the
   *  current control, because the control moves and the entry does not: asking
   *  an old question again must repeat what it did, and two entries with
   *  different answers to the same question are otherwise unexplained. */
  effort: AskEffort;
}

export interface AskSession {
  /** Newest first. */
  entries: AskEntry[];
  selected: string | null;
}

export type AskAction =
  | {
      type: "submit";
      id: string;
      question: string;
      libraryId: string;
      startedAt: number;
      effort: AskEffort;
    }
  | { type: "started"; id: string; questionId: string }
  | { type: "answered"; id: string; answer: Answer }
  | { type: "failed"; id: string; error: unknown }
  | { type: "select"; id: string }
  | { type: "citation"; index: number };

export const EMPTY_SESSION: AskSession = { entries: [], selected: null };

/** Replace one entry by id, leaving every other entry identical. */
function patch(
  state: AskSession,
  id: string,
  change: (entry: AskEntry) => AskEntry,
): AskSession {
  return {
    ...state,
    entries: state.entries.map((e) => (e.id === id ? change(e) : e)),
  };
}

export function askReducer(state: AskSession, action: AskAction): AskSession {
  switch (action.type) {
    case "submit":
      return {
        entries: [
          {
            id: action.id,
            question: action.question,
            libraryId: action.libraryId,
            status: "pending",
            questionId: null,
            startedAt: action.startedAt,
            answer: null,
            error: null,
            citation: 0,
            effort: action.effort,
          },
          ...state.entries,
        ],
        // A new question is what you asked to look at. It is selected while it
        // is still in flight, so the centre column shows the question being
        // worked on rather than the previous answer.
        selected: action.id,
      };

    case "started":
      return patch(state, action.id, (e) => ({ ...e, questionId: action.questionId }));

    case "answered":
      return patch(state, action.id, (e) => ({
        ...e,
        status: "done",
        answer: action.answer,
        error: null,
        citation: 0,
      }));

    case "failed":
      return patch(state, action.id, (e) => ({
        ...e,
        status: "failed",
        answer: null,
        error: action.error,
      }));

    case "select":
      return state.entries.some((e) => e.id === action.id)
        ? { ...state, selected: action.id }
        : state;

    case "citation":
      return state.selected === null
        ? state
        : patch(state, state.selected, (e) => ({ ...e, citation: action.index }));
  }
}

export function selectedEntry(state: AskSession): AskEntry | null {
  if (state.selected === null) return null;
  return state.entries.find((e) => e.id === state.selected) ?? null;
}

/**
 * The passage a citation rests on, joined by `chunkId`.
 *
 * `null` is a real outcome and not an error: the backend drops a citation whose
 * chunk id it never retrieved, but an answer can still name a chunk that is not
 * in the evidence the UI was handed. The panel says so rather than showing an
 * empty box.
 */
export function citedEvidence(entry: AskEntry | null): EvidenceItem | null {
  const citation = entry?.answer?.citations[entry.citation];
  if (!citation) return null;
  return (
    entry?.answer?.evidence.find((e) => e.chunkId === citation.chunkId) ?? null
  );
}

// -- persistence -------------------------------------------------------------
//
// localStorage, guarded exactly as `libraries.tsx` guards its own access: it
// throws outright in some webview configurations, and a failure there must
// degrade to "nothing remembered" rather than to a blank screen.

const STORED = "companyBrain.askHistory";

/** Enough to find last week's question, few enough that a session's worth of
 *  answers — each carrying its retrieved passages in full — stays well inside a
 *  5 MB quota. Oldest are dropped first. */
const KEEP = 20;

/** Errors are stored as the shape the UI already knows how to read. A thrown
 *  `Error` serialises to `{}`, which would reload as a failure with nothing
 *  said about it. */
function storableError(error: unknown): { kind: string; message: string } | null {
  if (error === null || error === undefined) return null;
  if (isAppError(error)) return { kind: error.kind, message: error.message };
  return { kind: "io", message: errorMessage(error) };
}

/** Scoped on the plane, because a question is asked of one corpus and answered
 *  by one organisation's data. Signing out of the paid service and back in as
 *  somebody else must not leave their questions — and their retrieved passages —
 *  on screen. `scopedKey` leaves the local plane's key alone, so no existing
 *  history is orphaned by this. */
export function saveSession(state: AskSession, identity: string): void {
  const entries = state.entries.slice(0, KEEP);
  const payload = {
    entries: entries.map((e) => ({ ...e, error: storableError(e.error) })),
    selected: entries.some((e) => e.id === state.selected) ? state.selected : null,
  };
  try {
    window.localStorage.setItem(scopedKey(STORED, identity), JSON.stringify(payload));
  } catch {
    // Over quota, or storage denied. Losing the history is not worth an error
    // in front of an answer the user is reading.
  }
}

/** Restore a previous window's history, or an empty session. */
export function loadSession(identity: string): AskSession {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(scopedKey(STORED, identity));
  } catch {
    return EMPTY_SESSION;
  }
  if (!raw) return EMPTY_SESSION;

  try {
    const parsed = JSON.parse(raw) as Partial<AskSession>;
    if (!Array.isArray(parsed.entries)) return EMPTY_SESSION;

    const entries = parsed.entries.slice(0, KEEP).map((e) => ({
      ...e,
      // A question that was in flight when the window closed is still in flight
      // in the API, and resumes polling. One that never got an id cannot be
      // collected by anything, so it is retired here instead of spinning.
      status:
        e.status === "pending" && !e.questionId ? ("failed" as const) : e.status,
      error:
        e.status === "pending" && !e.questionId
          ? { kind: "io", message: "interrupted" }
          : e.error,
      // Every entry already in a user's history predates the level, and was
      // asked at what is now the default. Validated rather than trusted for the
      // same reason `loadEffort` validates: a bad value here would be handed
      // straight back to the server by "ask again".
      effort: isAskEffort(e.effort) ? e.effort : DEFAULT_ASK_EFFORT,
    }));

    return {
      entries,
      selected: entries.some((e) => e.id === parsed.selected) ? parsed.selected ?? null : null,
    };
  } catch {
    // Written by an older shape, or truncated. Not worth reporting.
    return EMPTY_SESSION;
  }
}

/** The highest `q<n>` id in a restored history, so new ids do not collide with
 *  reloaded ones. */
export function lastId(state: AskSession): number {
  return state.entries.reduce((max, e) => {
    const n = Number.parseInt(e.id.replace(/^q/, ""), 10);
    return Number.isFinite(n) && n > max ? n : max;
  }, 0);
}
