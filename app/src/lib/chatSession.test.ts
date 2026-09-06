/**
 * The conversation reducer.
 *
 * Most of these are about the gap between what streams and what is true. A
 * turn's prose arrives before its citations are verified, so the screen shows
 * something it may have to take back — and the rules for taking it back are the
 * only thing standing between this feature and shipping an answer nothing
 * checked.
 */
import { describe, expect, it } from "vitest";

import {
  actionFor,
  chatReducer,
  citedPassage,
  citedTurnOf,
  emptyChat,
  type ChatState,
} from "./chatSession";
import type { ChatEvent, Conversation, ConversationTurn } from "./api";

function turn(over: Partial<ConversationTurn> = {}): ConversationTurn {
  return {
    seq: 1,
    question: "¿Quién fue Jesucristo?",
    searched: "¿Quién fue Jesucristo?",
    answer: "Fue presentado como…",
    state: "answered",
    effort: "standard",
    styleEffort: "standard",
    citations: [],
    citedEvidence: [],
    error: null,
    askedAt: "t0",
    answeredAt: "t1",
    ...over,
  };
}

function conversation(over: Partial<Conversation> = {}): Conversation {
  return {
    id: "cnv_1",
    libraryId: "lib_a",
    title: "La fe",
    titleGenerated: true,
    turns: 1,
    createdAt: "t0",
    lastMessageAt: "t1",
    ...over,
  };
}

/** A state with one conversation open and a turn in flight. */
function streaming(): ChatState {
  let s = chatReducer(emptyChat, {
    type: "open",
    conversationId: "cnv_1",
    turns: [],
  });
  return chatReducer(s, { type: "submit", seq: 1, question: "¿?", effort: "standard" });
}

// -- streaming --------------------------------------------------------------

describe("a turn being answered", () => {
  it("shows the question the instant it is asked", () => {
    const s = streaming();
    expect(s.turns).toHaveLength(1);
    expect(s.turns[0]!.state).toBe("running");
    expect(s.streaming).toEqual({ seq: 1, since: 0, stage: null, evidence: null });
  });

  it("accumulates deltas in order", () => {
    let s = streaming();
    s = chatReducer(s, { type: "delta", seq: 1, chunkSeq: 1, text: "La gente " });
    s = chatReducer(s, { type: "delta", seq: 1, chunkSeq: 2, text: "es feliz" });
    expect(s.turns[0]!.answer).toBe("La gente es feliz");
    expect(s.streaming!.since).toBe(2);
  });

  it("drops a delta at or below the resume point", () => {
    // Reopening a conversation mid-answer replays from `since`. A client that
    // trusted arrival order would show the opening of the answer twice.
    let s = streaming();
    s = chatReducer(s, { type: "delta", seq: 1, chunkSeq: 1, text: "La gente " });
    s = chatReducer(s, { type: "delta", seq: 1, chunkSeq: 1, text: "La gente " });
    expect(s.turns[0]!.answer).toBe("La gente ");
  });

  it("ignores a delta for a turn it is not streaming", () => {
    let s = streaming();
    s = chatReducer(s, { type: "delta", seq: 99, chunkSeq: 1, text: "de otra" });
    expect(s.turns[0]!.answer).toBe("");
  });
});

// -- the property this whole design rests on --------------------------------

describe("a settled turn", () => {
  it("replaces the draft rather than completing it", () => {
    let s = streaming();
    s = chatReducer(s, { type: "delta", seq: 1, chunkSeq: 1, text: "borrador " });
    s = chatReducer(s, { type: "settled", turn: turn({ answer: "definitiva" }) });
    expect(s.turns[0]!.answer).toBe("definitiva");
    expect(s.streaming).toBeNull();
  });

  it("clears prose the reader was already shown when the turn is refused", () => {
    // The gap this feature has to survive. The model wrote a confident,
    // fluent paragraph and cited a chunk it was never shown; `citas` arrives
    // last in the envelope, so nothing knew that until it closed. Keeping the
    // paragraph would leave unverified text under a heading that says it was
    // checked.
    let s = streaming();
    s = chatReducer(s, {
      type: "delta",
      seq: 1,
      chunkSeq: 1,
      text: "La felicidad procede de la virtud, según el texto.",
    });
    expect(s.turns[0]!.answer).not.toBe("");

    s = chatReducer(s, {
      type: "settled",
      turn: turn({ state: "insufficient_evidence", answer: "", citations: [] }),
    });
    expect(s.turns[0]!.answer).toBe("");
    expect(s.turns[0]!.state).toBe("insufficient_evidence");
  });

  it("carries the substitution the reader needs to see", () => {
    let s = streaming();
    s = chatReducer(s, {
      type: "settled",
      turn: turn({
        question: "¿y su muerte?",
        searched: "¿Qué relación tiene la justificación por la fe con las obras?",
      }),
    });
    // The person typed one thing and the corpus was searched for another. An
    // answer that quietly addresses something adjacent is indistinguishable
    // from a bad answer unless this is visible.
    expect(s.turns[0]!.question).toBe("¿y su muerte?");
    expect(s.turns[0]!.searched).not.toBe(s.turns[0]!.question);
  });

  it("leaves another turn's stream alone", () => {
    let s = streaming();
    s = chatReducer(s, { type: "settled", turn: turn({ seq: 99 }) });
    expect(s.streaming).toEqual({ seq: 1, since: 0, stage: null, evidence: null });
  });
});

describe("a failed turn", () => {
  it("keeps its kind and drops its draft", () => {
    let s = streaming();
    s = chatReducer(s, { type: "delta", seq: 1, chunkSeq: 1, text: "a medio" });
    s = chatReducer(s, {
      type: "failed",
      seq: 1,
      kind: "provider_quota",
      message: "sin cuota",
    });
    expect(s.turns[0]!.state).toBe("failed");
    expect(s.turns[0]!.answer).toBe("");
    expect(s.turns[0]!.error).toEqual({ kind: "provider_quota", message: "sin cuota" });
    expect(s.streaming).toBeNull();
  });
});

// -- switching conversations ------------------------------------------------

describe("moving between conversations", () => {
  it("drops a stream that belonged to the one being left", () => {
    // The events are still arriving for the other conversation's turn, and
    // applying them here would write another conversation's answer into this
    // one's transcript.
    let s = streaming();
    s = chatReducer(s, { type: "select", conversationId: "cnv_2" });
    expect(s.streaming).toBeNull();
    expect(s.turns).toEqual([]);
    s = chatReducer(s, { type: "delta", seq: 1, chunkSeq: 1, text: "fuga" });
    expect(s.turns).toEqual([]);
  });

  it("does nothing when the same one is chosen again", () => {
    const s = streaming();
    expect(chatReducer(s, { type: "select", conversationId: "cnv_1" })).toBe(s);
  });

  it("opens on the last turn that produced an answer", () => {
    const s = chatReducer(emptyChat, {
      type: "open",
      conversationId: "cnv_1",
      turns: [turn({ seq: 1 }), turn({ seq: 2 }), turn({ seq: 3, state: "running", answer: "" })],
    });
    expect(s.citedTurn).toBe(2);
  });

  it("opens on the last turn that has citations, not the last answered one", () => {
    // Found by screenshotting a transcript that ended in a refusal: the column
    // said "no citations" while two answered turns above it each had some.
    const withCitation = (seq: number) =>
      turn({
        seq,
        citations: [
          { chunkId: `chk_${seq}`, locator: "L", claim: "c", page: null, sectionTitle: null },
        ],
      });
    const s = chatReducer(emptyChat, {
      type: "open",
      conversationId: "cnv_1",
      turns: [
        withCitation(1),
        withCitation(2),
        turn({ seq: 3, state: "insufficient_evidence", answer: "", citations: [] }),
      ],
    });
    expect(s.citedTurn).toBe(2);
  });

  it("still follows the conversation when nothing has been cited at all", () => {
    const s = chatReducer(emptyChat, {
      type: "open",
      conversationId: "cnv_1",
      turns: [turn({ seq: 1, citations: [] }), turn({ seq: 2, citations: [] })],
    });
    expect(s.citedTurn).toBe(2);
  });

  it("shows no citations for a conversation with nothing answered yet", () => {
    const s = chatReducer(emptyChat, {
      type: "open",
      conversationId: "cnv_1",
      turns: [turn({ seq: 1, state: "running", answer: "" })],
    });
    expect(s.citedTurn).toBeNull();
  });
});

// -- citations --------------------------------------------------------------

describe("the citations column", () => {
  const cited = turn({
    citations: [
      { chunkId: "chk_a", locator: "Cap 1 · [1:2]", claim: "uno", page: null, sectionTitle: null },
      { chunkId: "chk_b", locator: "Cap 2 · [3:4]", claim: "dos", page: null, sectionTitle: null },
    ],
    citedEvidence: [
      { chunkId: "chk_b", title: "T", breadcrumb: "Cap 2", text: "el pasaje", kind: "cuerpo", score: 0.8, source: "vector", locator: "Cap 2 · [3:4]" },
    ],
  });

  it("resolves a citation to the passage it rests on", () => {
    let s = chatReducer(emptyChat, { type: "open", conversationId: "cnv_1", turns: [cited] });
    s = chatReducer(s, { type: "citation", turnSeq: 1, index: 1 });
    expect(citedPassage(citedTurnOf(s), s.citation)!.text).toBe("el pasaje");
  });

  it("returns nothing rather than the wrong passage when the chunk is absent", () => {
    // Only the evidence a *verified* citation names is stored, so the two lists
    // normally match — but a citation whose chunk is missing must render as
    // "no passage", never as another citation's text.
    const s = chatReducer(emptyChat, { type: "open", conversationId: "cnv_1", turns: [cited] });
    expect(citedPassage(citedTurnOf(s), 0)).toBeNull();
  });

  it("follows the turn being answered rather than staying on the previous one", () => {
    let s = chatReducer(emptyChat, { type: "open", conversationId: "cnv_1", turns: [cited] });
    expect(s.citedTurn).toBe(1);
    s = chatReducer(s, { type: "submit", seq: 2, question: "¿y?", effort: "brief" });
    expect(s.citedTurn).toBe(2);
    expect(s.citation).toBe(0);
  });
});

// -- events to actions ------------------------------------------------------

describe("folding a server event", () => {
  const fold = (e: ChatEvent) => actionFor(e, 1);

  it("turns a token into a delta", () => {
    expect(fold({ type: "token", seq: 3, text: "hola" })).toEqual({
      type: "delta", seq: 1, chunkSeq: 3, text: "hola",
    });
  });

  it("turns done into a settlement", () => {
    const t = turn();
    expect(fold({ type: "done", turn: t })).toEqual({ type: "settled", turn: t });
  });

  it("turns an error into a failure, with a kind to key on", () => {
    expect(fold({ type: "error", kind: "catalog_unreachable", message: "no" })).toEqual({
      type: "failed", seq: 1, kind: "catalog_unreachable", message: "no",
    });
  });

  it("ignores an event type this build does not read", () => {
    // A newer server sending `stage`, say. The answer is still arriving and the
    // settled copy is in the catalog either way, so this must not read as a
    // failure.
    expect(fold({ type: "stage", seq: 1 })).toBeNull();
  });

  it("ignores a done with no turn rather than settling an empty one", () => {
    expect(fold({ type: "done" })).toBeNull();
  });
});

describe("the list", () => {
  it("is taken from the server in the order it arrives", () => {
    const rows = [conversation({ id: "cnv_2" }), conversation({ id: "cnv_1" })];
    const s = chatReducer(emptyChat, { type: "conversations", conversations: rows });
    expect(s.conversations.map((c) => c.id)).toEqual(["cnv_2", "cnv_1"]);
  });
});

// -- stages -----------------------------------------------------------------
//
// Streamed prose was measured at about 8% of a turn's wait. These cover the
// rest, so they are the events that decide whether a slow turn looks like
// progress or like a hang.

describe("a turn reporting where it has got to", () => {
  it("records the stage the server last announced", () => {
    let s = streaming();
    s = chatReducer(s, { type: "stage", seq: 1, chunkSeq: 1, stage: "planning" });
    expect(s.streaming!.stage).toBe("planning");
    s = chatReducer(s, { type: "stage", seq: 1, chunkSeq: 2, stage: "retrieving" });
    expect(s.streaming!.stage).toBe("retrieving");
  });

  it("keeps the evidence count standing through the later stages", () => {
    // Once the passages are found, how many there were is the most useful thing
    // on screen while the model writes — so a later stage must not clear it.
    let s = streaming();
    s = chatReducer(s, {
      type: "stage", seq: 1, chunkSeq: 1, stage: "evidence", chunks: 48, dense: 27,
    });
    s = chatReducer(s, { type: "stage", seq: 1, chunkSeq: 2, stage: "generating" });
    expect(s.streaming!.stage).toBe("generating");
    expect(s.streaming!.evidence).toEqual({ chunks: 48, dense: 27 });
  });

  it("tells an unmeasured dense count from a measured zero", () => {
    // A zero is a real fact about the corpus and is worth printing; a missing
    // figure is not, and printing it as 0 would claim something false.
    let s = streaming();
    s = chatReducer(s, { type: "stage", seq: 1, chunkSeq: 1, stage: "evidence", chunks: 4 });
    expect(s.streaming!.evidence).toEqual({ chunks: 4, dense: null });
    s = chatReducer(s, {
      type: "stage", seq: 1, chunkSeq: 2, stage: "evidence", chunks: 4, dense: 0,
    });
    expect(s.streaming!.evidence).toEqual({ chunks: 4, dense: 0 });
  });

  it("drops a stage at or below the resume point", () => {
    // Reopening a conversation replays from `since`, and a stage applied out of
    // order would walk the display backwards through stages already passed.
    let s = streaming();
    s = chatReducer(s, { type: "stage", seq: 1, chunkSeq: 2, stage: "generating" });
    s = chatReducer(s, { type: "stage", seq: 1, chunkSeq: 1, stage: "planning" });
    expect(s.streaming!.stage).toBe("generating");
  });

  it("ignores a stage for a turn it is not streaming", () => {
    let s = streaming();
    s = chatReducer(s, { type: "stage", seq: 99, chunkSeq: 1, stage: "planning" });
    expect(s.streaming!.stage).toBeNull();
  });

  it("forgets the stage once the turn settles", () => {
    let s = streaming();
    s = chatReducer(s, { type: "stage", seq: 1, chunkSeq: 1, stage: "generating" });
    s = chatReducer(s, { type: "settled", turn: turn() });
    expect(s.streaming).toBeNull();
  });

  it("folds a stage event into a stage action, counts and all", () => {
    expect(
      actionFor({ type: "stage", seq: 4, stage: "evidence", chunks: 48, dense: 27 }, 1),
    ).toEqual({
      type: "stage", seq: 1, chunkSeq: 4, stage: "evidence", chunks: 48, dense: 27,
    });
  });

  it("ignores a stage event with no stage name", () => {
    expect(actionFor({ type: "stage", seq: 1 }, 1)).toBeNull();
  });
});
