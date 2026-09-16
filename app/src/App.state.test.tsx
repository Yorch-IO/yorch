import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import i18n from "./i18n";
import type { Answer } from "./lib/api";

/**
 * Leaving a tab and coming back must not cost anything. Every screen is
 * mounted at once and merely hidden for exactly this reason, and it was
 * reported as broken for Ask — so it is asserted here against the real
 * `AskScreen` rather than the stub `App.test.tsx` uses.
 */
const { ask, askResult, libraries } = vi.hoisted(() => ({
  ask: vi.fn(),
  askResult: vi.fn(),
  libraries: vi.fn(),
}));

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return { ...actual, api: { ...actual.api, ask, askResult, libraries } };
});

// The other five screens are stubbed; AskScreen is the real one.
vi.mock("./screens/HomeScreen", () => ({ HomeScreen: () => <p>screen:home</p> }));
vi.mock("./screens/StackScreen", () => ({ StackScreen: () => <p>screen:stack</p> }));
vi.mock("./screens/LibraryScreen", () => ({ LibraryScreen: () => <p>screen:library</p> }));
vi.mock("./screens/ExploreScreen", () => ({ ExploreScreen: () => <p>screen:explore</p> }));
vi.mock("./screens/GraphScreen", () => ({ GraphScreen: () => <p>screen:graph</p> }));
vi.mock("./screens/ImportScreen", () => ({ ImportScreen: () => <p>screen:import</p> }));
vi.mock("./screens/ChannelScreen", () => ({ ChannelScreen: () => <p>screen:channel</p> }));

const t = (key: string): string => i18n.t(key);

const answer = (text: string): Answer => ({
  state: "answered",
  text,
  citations: [
    { chunkId: "c1", claim: "una afirmación", locator: "c1#1", page: null, sectionTitle: null },
  ],
  evidence: [
    {
      chunkId: "c1",
      title: "Historia de la Iglesia",
      breadcrumb: "1. Capítulo",
      text: "el pasaje",
      kind: "cuerpo",
      score: 0.7,
      source: "vector",
      locator: "c1#1",
    },
  ],
  reason: "",
  spend: [],
});

// The navigation's "Ask" and the composer's "Ask" are the same word, so each
// is looked up inside its own landmark rather than across the document.
const goTo = (tab: string) =>
  fireEvent.click(
    within(screen.getByRole("navigation")).getByRole("button", { name: t(`nav.${tab}`) }),
  );

const submitButton = () =>
  within(screen.getByRole("main")).getByRole("button", { name: t("ask.submit") });

function willAnswer(a: Answer) {
  ask.mockResolvedValue({ questionId: "qid_1", state: "running" });
  askResult.mockResolvedValue({
    questionId: "qid_1",
    state: "done",
    answer: a,
    error: null,
  });
}

beforeEach(() => {
  window.localStorage.clear();
  libraries.mockResolvedValue({
    libraries: [
      { id: "lib_a", name: "Biblioteca A", language: "es", documents: 2, indexedVersions: 2 },
    ],
  });
  willAnswer(answer("la respuesta"));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("leaving Ask and coming back", () => {
  it("keeps the history, the answer and the typed question", async () => {
    render(<App />);
    await waitFor(() => expect(libraries).toHaveBeenCalled());
    goTo("ask");

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "¿Historia de la iglesia?" },
    });
    fireEvent.click(submitButton());
    await screen.findByText("la respuesta");

    // Type the next question, then wander off mid-thought.
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "a medio escribir" } });
    goTo("library");
    goTo("ask");

    expect(screen.getByText("la respuesta")).toBeTruthy();
    expect(screen.getByRole("button", { name: "¿Historia de la iglesia?" })).toBeTruthy();
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("a medio escribir");
    expect(screen.getByText("el pasaje")).toBeTruthy();
  });

  it("does not re-ask, and so does not spend again", async () => {
    render(<App />);
    await waitFor(() => expect(libraries).toHaveBeenCalled());
    goTo("ask");

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "¿una pregunta?" } });
    fireEvent.click(submitButton());
    await screen.findByText("la respuesta");

    goTo("graph");
    goTo("ask");

    expect(ask).toHaveBeenCalledTimes(1);
  });

  it("keeps collecting a question that is still running", async () => {
    // Answering takes tens of seconds to minutes, so wandering off during it is
    // the normal case rather than an edge one.
    ask.mockResolvedValue({ questionId: "qid_1", state: "running" });
    askResult.mockResolvedValue({
      questionId: "qid_1",
      state: "running",
      answer: null,
      error: null,
    });

    render(<App />);
    await waitFor(() => expect(libraries).toHaveBeenCalled());
    goTo("ask");

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "¿lenta?" } });
    fireEvent.click(submitButton());
    await waitFor(() => expect(askResult).toHaveBeenCalled());

    goTo("import");
    goTo("ask");

    // Still on the shelf, still being collected, and the answer lands when it
    // lands rather than being lost to the tab switch.
    askResult.mockResolvedValue({
      questionId: "qid_1",
      state: "done",
      answer: answer("llegó tarde"),
      error: null,
    });
    // Longer than one POLL_MS: the answer arrives on the *next* collection, and
    // findBy's default second would give up before it.
    expect(await screen.findByText("llegó tarde", {}, { timeout: 4000 })).toBeTruthy();
  });
});
