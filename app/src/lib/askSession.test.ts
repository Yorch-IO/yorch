import { describe, expect, it } from "vitest";

import type { Answer, Citation, EvidenceItem } from "./api";
import {
  askReducer,
  citedEvidence,
  selectedEntry,
  EMPTY_SESSION,
  type AskSession,
} from "./askSession";

function citation(chunkId: string, claim: string): Citation {
  return { chunkId, claim, locator: `${chunkId}#1`, page: null, sectionTitle: null };
}

function evidence(chunkId: string, text: string): EvidenceItem {
  return {
    chunkId,
    title: "Libro",
    breadcrumb: "1. Capítulo",
    text,
    kind: "cuerpo",
    score: 0.7,
    source: "vector",
    locator: `${chunkId}#1`,
  };
}

function answered(chunkIds: string[]): Answer {
  return {
    state: "answered",
    text: "una respuesta",
    citations: chunkIds.map((id, i) => citation(id, `afirmación ${i}`)),
    evidence: chunkIds.map((id) => evidence(id, `el texto de ${id}`)),
    reason: "",
    spend: [],
  };
}

/** Two questions asked in order, the second still in flight. */
function twoAsked(): AskSession {
  let s = askReducer(EMPTY_SESSION, {
    type: "submit",
    id: "q1",
    question: "¿la primera?",
    libraryId: "lib_a",
    startedAt: 0,
  });
  s = askReducer(s, { type: "answered", id: "q1", answer: answered(["c1", "c2"]) });
  return askReducer(s, {
    type: "submit",
    id: "q2",
    question: "¿la segunda?",
    libraryId: "lib_b",
    startedAt: 0,
  });
}

describe("submitting", () => {
  it("selects the newest question and keeps the earlier ones", () => {
    const s = twoAsked();

    expect(s.selected).toBe("q2");
    expect(s.entries.map((e) => e.id)).toEqual(["q2", "q1"]);
    // The earlier answer is not disturbed by the later question. This is the
    // whole reason the screen holds a history rather than one `answer`.
    expect(s.entries[1]?.answer?.text).toBe("una respuesta");
  });

  it("records which library each question was asked of", () => {
    // The picker is global and the answers are not: an entry from before you
    // switched libraries was answered by the other one.
    const s = twoAsked();
    expect(s.entries.map((e) => e.libraryId)).toEqual(["lib_b", "lib_a"]);
  });

  it("leaves the new entry pending until it is answered", () => {
    const s = twoAsked();
    expect(selectedEntry(s)?.status).toBe("pending");
    expect(selectedEntry(s)?.answer).toBeNull();
  });
});

describe("selecting an earlier question", () => {
  it("restores its answer", () => {
    const s = askReducer(twoAsked(), { type: "select", id: "q1" });

    expect(s.selected).toBe("q1");
    expect(selectedEntry(s)?.question).toBe("¿la primera?");
    expect(selectedEntry(s)?.answer?.citations).toHaveLength(2);
  });

  it("restores the citation it was left on, rather than the first", () => {
    let s = askReducer(twoAsked(), { type: "select", id: "q1" });
    s = askReducer(s, { type: "citation", index: 1 });
    s = askReducer(s, { type: "select", id: "q2" });
    s = askReducer(s, { type: "select", id: "q1" });

    expect(selectedEntry(s)?.citation).toBe(1);
  });

  it("ignores an id that is not in the history", () => {
    const s = twoAsked();
    expect(askReducer(s, { type: "select", id: "q9" })).toBe(s);
  });
});

describe("failure", () => {
  it("marks only its own entry and leaves the others answered", () => {
    const s = askReducer(twoAsked(), {
      type: "failed",
      id: "q2",
      error: new Error("no reachable"),
    });

    expect(s.entries[0]?.status).toBe("failed");
    expect(s.entries[1]?.status).toBe("done");
    expect(s.entries[1]?.answer?.text).toBe("una respuesta");
  });
});

describe("citedEvidence", () => {
  it("joins the selected citation to its passage by chunkId", () => {
    let s = askReducer(twoAsked(), { type: "select", id: "q1" });
    expect(citedEvidence(selectedEntry(s))?.text).toBe("el texto de c1");

    s = askReducer(s, { type: "citation", index: 1 });
    expect(citedEvidence(selectedEntry(s))?.text).toBe("el texto de c2");
  });

  it("returns null when no retrieved passage carries that chunk id", () => {
    // A real case, not a defensive one: the citation survives and the panel has
    // to say that the passage behind it is not in what it was handed.
    const orphan = answered(["c1"]);
    orphan.evidence = [];
    let s = askReducer(EMPTY_SESSION, {
      type: "submit",
      id: "q1",
      question: "¿?",
      libraryId: "lib_a",
      startedAt: 0,
    });
    s = askReducer(s, { type: "answered", id: "q1", answer: orphan });

    expect(citedEvidence(selectedEntry(s))).toBeNull();
  });

  it("returns null for an entry with no answer at all", () => {
    expect(citedEvidence(selectedEntry(twoAsked()))).toBeNull();
    expect(citedEvidence(null)).toBeNull();
  });
});
