/**
 * The Chat screen's state, as a reducer rather than as component state.
 *
 * Here for the reason `askSession.ts` gives about itself: the properties that
 * matter are properties of a function, and testing them costs nothing and does
 * not depend on anything rendering. Three of them are specific to conversations
 * and none is obvious from the screen.
 *
 * **A settled turn replaces its draft; it never completes one.** `citas` is the
 * last field in the answering schema, so verification cannot run until the
 * envelope closes — a turn can stream fluent, plausible prose and still come
 * back `insufficient_evidence` because the model cited chunks it was never
 * shown. Appending, or keeping what streamed when the settled answer is empty,
 * would leave unverified text on screen under a heading that says it was
 * checked. That is the one failure this product cannot have.
 *
 * **A delta before the resume point is dropped.** Reopening a conversation
 * mid-answer replays from `since`, and a client that trusted arrival order
 * would show the beginning of the answer twice.
 *
 * **Nothing but the selected id is persisted.** The transcript lives in the
 * catalog, so there is no second copy to reconcile and nothing that can drift —
 * which is deliberately unlike `askSession`, whose history is client-side
 * because a question's answer lives only in Temporal until its retention
 * expires. Helena, the reference this feature was built against, keeps two
 * message stores that are never reconciled and its own documentation calls that
 * a live drift bug; this is the one thing from it deliberately not copied.
 */
import type { AskEffort } from "./askEffort";
import { scopedKey } from "./backend";
import type { ChatEvent, Conversation, ConversationTurn } from "./api";

export interface ChatState {
  /** The list, most recently used first. Server-ordered. */
  conversations: Conversation[];
  /** Which conversation is open, or null before one is. */
  selected: string | null;
  /** The open conversation's transcript, oldest first. */
  turns: ConversationTurn[];
  /**
   * The turn currently being streamed, the highest chunk seen for it, and where
   * the server says it has got to.
   *
   * `since` is bookkeeping rather than display: it is what a reconnect passes
   * so the server sends only what follows. `stage` and `evidence` are the
   * opposite — they exist only to be shown, and they are what covers the nine
   * tenths of a turn that streamed prose does not.
   */
  streaming: {
    seq: number;
    since: number;
    stage: string | null;
    evidence: { chunks: number; dense: number | null } | null;
  } | null;
  /** Index into the selected turn's citations. */
  citation: number;
  /** Which turn's citations the right-hand column is showing. */
  citedTurn: number | null;
}

export type ChatAction =
  | { type: "conversations"; conversations: Conversation[] }
  | { type: "open"; conversationId: string; turns: ConversationTurn[] }
  | { type: "submit"; seq: number; question: string; effort: AskEffort }
  | { type: "delta"; seq: number; chunkSeq: number; text: string }
  | {
      type: "stage";
      seq: number;
      chunkSeq: number;
      stage: string;
      chunks?: number | null;
      dense?: number | null;
    }
  | { type: "settled"; turn: ConversationTurn }
  | { type: "failed"; seq: number; kind: string; message: string }
  | { type: "select"; conversationId: string | null }
  | { type: "citation"; turnSeq: number; index: number };

export const emptyChat: ChatState = {
  conversations: [],
  selected: null,
  turns: [],
  streaming: null,
  citation: 0,
  citedTurn: null,
};

/** The row a turn occupies while it is being answered.
 *
 * Written locally rather than waited for, so the question appears the instant
 * it is asked. Every field the server will fill is empty here, which is what
 * makes `settled` a replacement rather than a merge — there is nothing local
 * worth keeping.
 */
function pending(seq: number, question: string, effort: AskEffort): ConversationTurn {
  return {
    seq,
    question,
    searched: null,
    answer: "",
    state: "running",
    effort,
    styleEffort: null,
    citations: [],
    citedEvidence: [],
    error: null,
    askedAt: new Date().toISOString(),
    answeredAt: null,
  };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "conversations":
      return { ...state, conversations: action.conversations };

    case "open":
      return {
        ...state,
        selected: action.conversationId,
        turns: action.turns,
        // A conversation just loaded is not streaming, even if one was: the
        // stream belonged to the conversation we left.
        streaming: null,
        citation: 0,
        citedTurn: lastCited(action.turns),
      };

    case "submit":
      return {
        ...state,
        turns: [...state.turns, pending(action.seq, action.question, action.effort)],
        streaming: { seq: action.seq, since: 0, stage: null, evidence: null },
        // The citations column follows the turn being answered rather than
        // staying on the previous one, which would read as an answer to the
        // question just asked.
        citation: 0,
        citedTurn: action.seq,
      };

    case "delta": {
      // Anything at or below the resume point has already been shown. Without
      // this a reconnect repeats the opening of the answer.
      if (
        state.streaming === null ||
        state.streaming.seq !== action.seq ||
        action.chunkSeq <= state.streaming.since
      ) {
        return state;
      }
      return {
        ...state,
        streaming: { ...state.streaming, since: action.chunkSeq },
        turns: state.turns.map((t) =>
          t.seq === action.seq ? { ...t, answer: t.answer + action.text } : t,
        ),
      };
    }

    case "stage": {
      // Same resume guard as a delta, and for the same reason: reopening a
      // conversation replays from `since`, and a stage applied out of order
      // would move the display backwards through stages already passed.
      if (
        state.streaming === null ||
        state.streaming.seq !== action.seq ||
        action.chunkSeq <= state.streaming.since
      ) {
        return state;
      }
      return {
        ...state,
        streaming: {
          ...state.streaming,
          since: action.chunkSeq,
          stage: action.stage,
          // `evidence` is the one stage that carries figures; every other one
          // leaves whatever was measured standing rather than clearing it, so
          // the count stays on screen through `generating`.
          evidence:
            action.stage === "evidence"
              ? { chunks: action.chunks ?? 0, dense: action.dense ?? null }
              : state.streaming.evidence,
        },
      };
    }

    case "settled":
      return {
        ...state,
        // **Replaced, not merged.** What streamed was a draft of one field; the
        // settled turn is the document, and a refused turn's empty `answer` has
        // to win over the prose that was on screen a moment ago.
        turns: state.turns.map((t) => (t.seq === action.turn.seq ? action.turn : t)),
        streaming:
          state.streaming?.seq === action.turn.seq ? null : state.streaming,
        citation: 0,
        citedTurn: action.turn.seq,
      };

    case "failed":
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.seq === action.seq
            ? {
                ...t,
                state: "failed",
                // Cleared for the same reason `settled` replaces: a failed turn
                // must not keep half an answer that nothing verified.
                answer: "",
                error: { kind: action.kind, message: action.message },
              }
            : t,
        ),
        streaming: state.streaming?.seq === action.seq ? null : state.streaming,
      };

    case "select":
      return action.conversationId === state.selected
        ? state
        : { ...state, selected: action.conversationId, turns: [], streaming: null,
            citation: 0, citedTurn: null };

    case "citation":
      return { ...state, citedTurn: action.turnSeq, citation: action.index };
  }
}

/**
 * Which turn the citations column opens on.
 *
 * The last turn *with citations*, not simply the last answered one. A
 * conversation whose most recent question was refused — `off_corpus`, or
 * evidence that did not support an answer — would otherwise open showing "no
 * citations" while three answered turns above it each had several. Found by
 * screenshotting a transcript that ended in a refusal.
 *
 * Falls back to the last settled turn so the column still follows the
 * conversation when nothing has been cited yet, and to null when nothing has
 * been answered at all.
 */
function lastCited(turns: ConversationTurn[]): number | null {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    if (turns[i]!.citations.length > 0) return turns[i]!.seq;
  }
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    if (turns[i]!.state !== "running") return turns[i]!.seq;
  }
  return null;
}

/** Fold one server event into an action, or null when there is nothing to do. */
export function actionFor(event: ChatEvent, seq: number): ChatAction | null {
  if (event.type === "token" && typeof event.text === "string") {
    return { type: "delta", seq, chunkSeq: event.seq ?? 0, text: event.text };
  }
  if (event.type === "stage" && typeof event.stage === "string") {
    return {
      type: "stage",
      seq,
      chunkSeq: event.seq ?? 0,
      stage: event.stage,
      chunks: event.chunks,
      dense: event.dense,
    };
  }
  if (event.type === "done" && event.turn) {
    return { type: "settled", turn: event.turn };
  }
  if (event.type === "error") {
    return {
      type: "failed",
      seq,
      kind: event.kind ?? "chat_failed",
      message: event.message ?? "",
    };
  }
  // An event type this build does not read, from a newer server. Ignored rather
  // than treated as a failure: the answer is still arriving and the settled
  // copy is in the catalog either way. That tolerance is why a stage travels as
  // one event type carrying a *name* — a stage added later needs no client
  // change, where one event type per stage would need one every time.
  return null;
}

/** The turn whose citations the right-hand column shows. */
export function citedTurnOf(state: ChatState): ConversationTurn | null {
  if (state.citedTurn === null) return null;
  return state.turns.find((t) => t.seq === state.citedTurn) ?? null;
}

/** The passage a citation rests on, joined by chunk id. */
export function citedPassage(turn: ConversationTurn | null, index: number) {
  const citation = turn?.citations[index];
  if (!citation) return null;
  return (
    turn!.citedEvidence.find((e) => e.chunkId === citation.chunkId) ?? null
  );
}

// -- persistence ------------------------------------------------------------
//
// One string. The transcript is the server's, so there is nothing else here
// worth keeping and nothing that can be stale.

const STORED = "companyBrain.chatSelected";

export function saveSelected(conversationId: string | null, identity: string): void {
  try {
    const key = scopedKey(STORED, identity);
    if (conversationId === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, conversationId);
  } catch {
    // Some webview configurations make localStorage throw outright. Failing
    // here has to degrade to "nothing remembered", never to a blank screen.
  }
}

export function loadSelected(identity: string): string | null {
  try {
    return window.localStorage.getItem(scopedKey(STORED, identity));
  } catch {
    return null;
  }
}
