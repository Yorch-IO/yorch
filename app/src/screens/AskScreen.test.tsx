import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { Answer, Citation, EvidenceItem } from "../lib/api";
import { LibrariesProvider } from "../lib/libraries";
import { AskScreen } from "./AskScreen";

/**
 * This renders the component, which the two older suites in this project
 * cannot do — they scan source text, because there was no component test stack
 * until this screen needed one. `LibraryScreen.test.ts` says in its own header
 * what that costs: it cannot catch a panel that renders with dead buttons. The
 * assertions below are exactly that class of property.
 *
 * Only `api` is replaced. `errorMessage` and `errorGuidanceKey` are pure and
 * worth exercising for real.
 */
// `vi.hoisted`, because `vi.mock`'s factory is hoisted above every import and
// would otherwise close over bindings that do not exist yet.
const { ask, askResult, libraries } = vi.hoisted(() => ({
  ask: vi.fn(),
  askResult: vi.fn(),
  libraries: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, ask, askResult, libraries } };
});

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});

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

function answer(over: Partial<Answer> = {}): Answer {
  return {
    state: "answered",
    text: "la respuesta",
    citations: [citation("c1", "la primera afirmación"), citation("c2", "la segunda")],
    evidence: [evidence("c1", "el pasaje uno"), evidence("c2", "el pasaje dos")],
    reason: "",
    spend: [{ stage: "answering", model: "gemini", inputTokens: 1, outputTokens: 1, usd: 0.01 }],
    ...over,
  };
}

/** Asking hands over an id and the answer is collected by polling, so a test
 *  that wants an answer has to arrange both halves. */
function willAnswer(a: Answer) {
  ask.mockResolvedValue({ questionId: "qid_1", state: "running" });
  askResult.mockResolvedValue({
    questionId: "qid_1",
    state: "done",
    answer: a,
    error: null,
  });
}

/** Render, and wait for the provider's one library fetch to land. Until it
 *  does there is no selected library, and submitting would ask nothing. */
async function mounted() {
  libraries.mockClear();
  render(
    <LibrariesProvider>
      <AskScreen />
    </LibrariesProvider>,
  );
  await waitFor(() => expect(libraries).toHaveBeenCalled());
}

/** Type a question and submit it. The button is disabled on an empty box and
 *  on a missing library, so it is waited for rather than assumed. */
async function submitQuestion(question: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: question } });
  const button = screen.getByRole("button", { name: t("ask.submit") });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(button);
}

async function askQuestion(question: string) {
  await mounted();
  await submitQuestion(question);
  await waitFor(() => expect(ask).toHaveBeenCalled());
}

beforeEach(() => {
  // The history is persisted now, so without this each test inherits the
  // previous one's questions and "nothing asked yet" is never true again.
  window.localStorage.clear();
  libraries.mockResolvedValue({
    libraries: [{ id: "lib_a", documents: 3, indexedVersions: 3 }],
  });
  willAnswer(answer());
});

afterEach(() => {
  // Vitest exposes no global afterEach here, so RTL's automatic cleanup never
  // registers itself. Without this the second test renders into a document
  // that still holds the first one's tree.
  cleanup();
  vi.clearAllMocks();
});

describe("session history", () => {
  it("keeps earlier questions and selects the newest", async () => {
    await mounted();

    await submitQuestion("¿la primera?");
    await screen.findByText("la respuesta");

    willAnswer(answer({ text: "la segunda respuesta" }));
    await submitQuestion("¿la segunda?");
    await screen.findByText("la segunda respuesta");

    // Both questions are still on the left…
    expect(screen.getByRole("button", { name: "¿la primera?" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "¿la segunda?" })).toBeTruthy();
    // …and the newest is what the centre column is showing.
    expect(screen.queryByText("la respuesta")).toBeNull();
  });

  it("restores an earlier answer when its question is clicked", async () => {
    await mounted();

    await submitQuestion("¿la primera?");
    await screen.findByText("la respuesta");

    willAnswer(answer({ text: "la segunda respuesta" }));
    await submitQuestion("¿la segunda?");
    await screen.findByText("la segunda respuesta");

    fireEvent.click(screen.getByRole("button", { name: "¿la primera?" }));

    expect(await screen.findByText("la respuesta")).toBeTruthy();
    expect(screen.queryByText("la segunda respuesta")).toBeNull();
  });

  it("says so before anything has been asked", async () => {
    await mounted();
    expect(screen.getByText(t("ask.historyEmpty"))).toBeTruthy();
    expect(screen.getByText(t("ask.noAnswerYet"))).toBeTruthy();
  });
});

describe("citations and the passage behind them", () => {
  it("previews the first citation's passage by default", async () => {
    await askQuestion("¿una pregunta?");
    expect(await screen.findByText("el pasaje uno")).toBeTruthy();
    expect(screen.queryByText("el pasaje dos")).toBeNull();
  });

  it("previews another citation's passage when it is selected", async () => {
    await askQuestion("¿una pregunta?");
    await screen.findByText("el pasaje uno");

    fireEvent.click(screen.getByRole("button", { name: "la segunda" }));

    expect(await screen.findByText("el pasaje dos")).toBeTruthy();
    expect(screen.queryByText("el pasaje uno")).toBeNull();
  });

  it("keeps the citation and says so when no retrieved passage matches", async () => {
    // The citation survives; what is missing is the text beside it.
    willAnswer(answer({ evidence: [evidence("c9", "otro pasaje")] }));
    await askQuestion("¿una pregunta?");

    expect(await screen.findByText(t("ask.noEvidence"))).toBeTruthy();
    expect(screen.getByRole("button", { name: "la primera afirmación" })).toBeTruthy();
  });
});

describe("the two refusals", () => {
  it("shows the passages that were retrieved and did not support an answer", async () => {
    willAnswer(
      answer({
        state: "insufficient_evidence",
        text: "",
        citations: [],
        reason: "los fragmentos no contienen la respuesta",
      }),
    );
    await askQuestion("¿una pregunta?");

    expect(await screen.findByText(t("ask.insufficient"))).toBeTruthy();
    expect(screen.getByText("los fragmentos no contienen la respuesta")).toBeTruthy();
    expect(screen.getByText(t("ask.notCitedInsufficient"))).toBeTruthy();
    // The passages themselves, not an empty state: they are what this library
    // does have on the question.
    expect(screen.getByText("el pasaje uno")).toBeTruthy();
    expect(screen.getByText("el pasaje dos")).toBeTruthy();
  });

  it("shows the near-misses for a question off the corpus", async () => {
    willAnswer(
      answer({
        state: "off_corpus",
        text: "",
        citations: [],
        evidence: [evidence("c1", "el pasaje que casi entra")],
        reason: "ningún fragmento supera el umbral",
      }),
    );
    await askQuestion("¿otra materia?");

    expect(await screen.findByText(t("ask.offCorpus"))).toBeTruthy();
    expect(screen.getByText(t("ask.notCitedOffCorpus"))).toBeTruthy();
    expect(screen.getByText("el pasaje que casi entra")).toBeTruthy();
  });

  it("distinguishes the two states rather than collapsing them", async () => {
    willAnswer(answer({ state: "off_corpus", citations: [] }));
    await askQuestion("¿otra materia?");

    await screen.findByText(t("ask.offCorpus"));
    expect(screen.queryByText(t("ask.insufficient"))).toBeNull();
    expect(screen.queryByText(t("ask.notCitedInsufficient"))).toBeNull();
  });
});

describe("failure", () => {
  it("reports the error against the question that failed", async () => {
    ask.mockRejectedValue(new Error("the control API did not answer"));
    await askQuestion("¿una pregunta?");

    expect(await screen.findByText(t("error.title"))).toBeTruthy();
    expect(screen.getByText(/the control API did not answer/)).toBeTruthy();
    // The question stays in the history, so it can be seen and asked again.
    expect(screen.getByRole("button", { name: "¿una pregunta?" })).toBeTruthy();
  });
});

describe("closing the window and coming back", () => {
  it("still has the question and its answer", async () => {
    await askQuestion("¿Historia de la iglesia?");
    await screen.findByText("la respuesta");

    // A relaunch: the tree goes, localStorage stays.
    cleanup();
    await mounted();

    expect(screen.getByRole("button", { name: "¿Historia de la iglesia?" })).toBeTruthy();
    expect(await screen.findByText("la respuesta")).toBeTruthy();
  });

  it("collects an answer computed while the window was shut", async () => {
    // The reason asking was split in two. The API kept working, and what was
    // paid for is picked up instead of being asked — and billed — again.
    ask.mockResolvedValue({ questionId: "qid_1", state: "running" });
    askResult.mockResolvedValue({
      questionId: "qid_1",
      state: "running",
      answer: null,
      error: null,
    });
    await askQuestion("¿lenta?");
    await waitFor(() => expect(askResult).toHaveBeenCalled());

    cleanup();
    askResult.mockResolvedValue({
      questionId: "qid_1",
      state: "done",
      answer: answer({ text: "estaba lista" }),
      error: null,
    });
    await mounted();

    expect(await screen.findByText("estaba lista")).toBeTruthy();
    expect(ask).toHaveBeenCalledTimes(1);
  });

  it("retires a question that never got an id, instead of polling forever", async () => {
    // Killed between pressing Ask and the API accepting it: nothing can collect
    // it, because nothing ever named it.
    window.localStorage.setItem(
      "companyBrain.askHistory",
      JSON.stringify({
        entries: [
          {
            id: "q1",
            question: "¿interrumpida?",
            libraryId: "lib_a",
            status: "pending",
            questionId: null,
            startedAt: 0,
            answer: null,
            error: null,
            citation: 0,
          },
        ],
        selected: "q1",
      }),
    );
    await mounted();

    expect(screen.getByRole("button", { name: "¿interrumpida?" })).toBeTruthy();
    expect(askResult).not.toHaveBeenCalled();
    // Retired, not stuck. A pending entry would keep `busy` true forever and
    // the composer disabled with it, which is the shape of the report this
    // whole change came from.
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "otra" } });
    const button = screen.getByRole("button", { name: t("ask.submit") });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  });
});

describe("asking again", () => {
  it("repeats a question the composer already cleared", async () => {
    await askQuestion("¿Historia de la iglesia?");
    await screen.findByText("la respuesta");
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");

    fireEvent.click(screen.getAllByRole("button", { name: t("ask.again") })[0]!);

    await waitFor(() => expect(ask).toHaveBeenCalledTimes(2));
    expect(ask.mock.calls[1]?.[0]).toMatchObject({ text: "¿Historia de la iglesia?" });
    // And it is a second entry, not an overwrite of the first.
    expect(screen.getAllByRole("button", { name: "¿Historia de la iglesia?" })).toHaveLength(2);
  });

  it("is not offered for a question still in flight", async () => {
    ask.mockResolvedValue({ questionId: "qid_1", state: "running" });
    askResult.mockResolvedValue({
      questionId: "qid_1",
      state: "running",
      answer: null,
      error: null,
    });
    await askQuestion("¿lenta?");
    await waitFor(() => expect(askResult).toHaveBeenCalled());

    expect(screen.queryByRole("button", { name: t("ask.again") })).toBeNull();
  });
});

describe("the clock", () => {
  it("runs during the handover, not only once the question has an id", async () => {
    // It was keyed to having an id, so it sat at 0s for the whole handover —
    // and the handover is exactly what is slow when the API is not answering.
    ask.mockReturnValue(new Promise(() => {}));
    await mounted();
    await submitQuestion("¿lenta de entregar?");

    // Both the history entry and the centre column carry it, which is the
    // point: whichever one you are looking at, it is visibly counting.
    expect(
      await screen.findAllByText(/Searching… [1-9]/, {}, { timeout: 4000 }),
    ).toHaveLength(2);
  });
});
