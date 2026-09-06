import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { ChatEvent, ConversationTurn } from "../lib/api";
import { BackendProvider } from "../lib/backend";
import { LibrariesProvider } from "../lib/libraries";
import { ChatScreen } from "./ChatScreen";
import { loadSelected, saveSelected } from "../lib/chatSession";

/**
 * The conversation screen, rendered.
 *
 * What is worth asserting here is what the reducer's tests cannot see: that the
 * streamed draft actually reaches the transcript, that the settled turn replaces
 * it *on screen*, and that the substitution the rewrite made is visible. The
 * last one is a product rule rather than a mechanism — an answer to a question
 * the person did not type is indistinguishable from a bad answer unless the
 * screen says what was searched.
 */
const { chatList, chatRead, chatCreate, chatTurn, chatStream, chatDelete, libraries } =
  vi.hoisted(() => ({
    chatList: vi.fn(),
    chatRead: vi.fn(),
    chatCreate: vi.fn(),
    chatTurn: vi.fn(),
    chatStream: vi.fn(),
    chatDelete: vi.fn(),
    libraries: vi.fn(),
  }));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      chatList,
      chatRead,
      chatCreate,
      chatTurn,
      chatStream,
      chatDelete,
      libraries,
    },
  };
});

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});

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

/** Arrange a turn whose stream emits `events` and then ends. */
function willStream(events: ChatEvent[]) {
  chatCreate.mockResolvedValue({ conversationId: "cnv_1", libraryId: "lib_a" });
  chatTurn.mockResolvedValue({ conversationId: "cnv_1", turnSeq: 1, state: "running" });
  chatStream.mockImplementation(
    async (_c: string, _s: number, _since: number, onEvent: (e: ChatEvent) => void) => {
      for (const e of events) onEvent(e);
    },
  );
}

async function mounted() {
  libraries.mockClear();
  render(
    <BackendProvider>
      <LibrariesProvider>
        <ChatScreen />
      </LibrariesProvider>
    </BackendProvider>,
  );
  await waitFor(() => expect(libraries).toHaveBeenCalled());
}

async function send(text: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  const button = screen.getByRole("button", { name: t("chat.send") });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(button);
}

beforeEach(() => {
  window.localStorage.clear();
  libraries.mockResolvedValue({
    libraries: [{ id: "lib_a", documents: 3, indexedVersions: 3 }],
  });
  chatList.mockResolvedValue({ conversations: [] });
  chatRead.mockResolvedValue({
    id: "cnv_1",
    libraryId: "lib_a",
    title: "T",
    titleGenerated: true,
    turns: 0,
    createdAt: "t0",
    lastMessageAt: "t1",
    turnsDetail: [],
  });
  willStream([]);
});

afterEach(() => {
  // Vitest exposes no global afterEach here, so RTL's automatic cleanup never
  // registers. Without this the next test renders into a document that still
  // holds this one's tree.
  cleanup();
  vi.clearAllMocks();
});

describe("an empty screen", () => {
  it("says what to do rather than showing an empty box", async () => {
    await mounted();
    expect(screen.getByText(t("chat.nothingYet"))).toBeTruthy();
    expect(screen.getByText(t("chat.listEmpty"))).toBeTruthy();
  });

  it("offers no citations until something has been answered", async () => {
    await mounted();
    expect(screen.getByText(t("chat.noCitations"))).toBeTruthy();
  });
});

describe("asking", () => {
  it("opens a conversation on the first message", async () => {
    await mounted();
    await send("¿Quién fue Jesucristo?");
    await waitFor(() => expect(chatCreate).toHaveBeenCalledWith("lib_a"));
    await waitFor(() => expect(chatTurn).toHaveBeenCalled());
  });

  it("shows the question immediately, before any answer arrives", async () => {
    await mounted();
    await send("¿Quién fue Jesucristo?");
    await waitFor(() => expect(screen.getByText("¿Quién fue Jesucristo?")).toBeTruthy());
  });

  it("puts the streamed prose in the transcript", async () => {
    willStream([
      { type: "token", seq: 1, text: "La gente " },
      { type: "token", seq: 2, text: "es feliz" },
    ]);
    await mounted();
    await send("¿?");
    await waitFor(() => expect(screen.getByText("La gente es feliz")).toBeTruthy());
  });

  it("does not lose what was typed when handing the turn over fails", async () => {
    chatCreate.mockRejectedValue({ kind: "control_unreachable", message: "no" });
    await mounted();
    await send("una pregunta larga");
    await waitFor(() =>
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
        "una pregunta larga",
      ),
    );
  });
});

describe("a settled turn", () => {
  it("replaces the draft on screen rather than adding to it", async () => {
    willStream([
      { type: "token", seq: 1, text: "borrador " },
      { type: "done", turn: turn({ answer: "definitiva" }) },
    ]);
    await mounted();
    await send("¿?");
    await waitFor(() => expect(screen.getByText("definitiva")).toBeTruthy());
    expect(screen.queryByText(/borrador/)).toBeNull();
  });

  it("clears prose the reader was shown when the turn is refused", async () => {
    // The gap the whole design has to survive: `citas` arrives last, so a turn
    // can stream a confident paragraph and then be refused because the model
    // cited chunks it was never shown. Leaving that paragraph up would put
    // unverified text under a heading that says it was checked.
    willStream([
      { type: "token", seq: 1, text: "La felicidad procede de la virtud." },
      {
        type: "done",
        turn: turn({ state: "insufficient_evidence", answer: "", citations: [] }),
      },
    ]);
    await mounted();
    await send("¿?");
    await waitFor(() => expect(screen.getByText(t("chat.insufficient"))).toBeTruthy());
    expect(screen.queryByText(/La felicidad procede/)).toBeNull();
  });

  it("shows what was actually searched when it differs from what was asked", async () => {
    willStream([
      {
        type: "done",
        turn: turn({
          question: "¿y su muerte?",
          searched: "¿Qué dice el corpus sobre la muerte de Jesucristo?",
        }),
      },
    ]);
    await mounted();
    await send("¿y su muerte?");
    await waitFor(() =>
      expect(
        screen.getByText(
          t("chat.searchedAs", {
            question: "¿Qué dice el corpus sobre la muerte de Jesucristo?",
          }),
        ),
      ).toBeTruthy(),
    );
  });

  it("does not clutter a first turn with a substitution that did not happen", async () => {
    // The rewrite makes no call on a first turn, so `searched` equals the
    // question. Printing it anyway would put a redundant line under every
    // opening message.
    willStream([{ type: "done", turn: turn({ question: "¿A?", searched: "¿A?" }) }]);
    await mounted();
    await send("¿A?");
    await waitFor(() => expect(screen.getByText("Fue presentado como…")).toBeTruthy());
    expect(screen.queryByText(t("chat.searchedAs", { question: "¿A?" }))).toBeNull();
  });
});

describe("a turn that fails", () => {
  it("reports the kind rather than a blank answer", async () => {
    willStream([
      { type: "error", kind: "provider_quota", message: "sin cuota" },
    ]);
    await mounted();
    await send("¿?");
    await waitFor(() => expect(screen.getByText("sin cuota")).toBeTruthy());
  });
});

describe("citations", () => {
  const cited = turn({
    citations: [
      { chunkId: "c1", claim: "la afirmación", locator: "Cap 1", page: null, sectionTitle: null },
    ],
    citedEvidence: [
      {
        chunkId: "c1",
        title: "Libro",
        breadcrumb: "1. Capítulo",
        text: "el pasaje citado",
        kind: "cuerpo",
        score: 0.8,
        source: "vector",
        locator: "Cap 1",
      },
    ],
  });

  it("lists them and shows the passage behind the selected one", async () => {
    willStream([{ type: "done", turn: cited }]);
    await mounted();
    await send("¿?");
    await waitFor(() => expect(screen.getByText("la afirmación")).toBeTruthy());
    expect(screen.getByText("el pasaje citado")).toBeTruthy();
  });
});

describe("the conversation list", () => {
  it("shows only the shelf the picker is on", async () => {
    // A conversation belongs to one library, and the picker is global. Showing
    // another shelf's would offer a transcript the picker says nothing about.
    chatList.mockResolvedValue({
      conversations: [
        { id: "cnv_1", libraryId: "lib_a", title: "De esta", titleGenerated: true, turns: 1, createdAt: "t", lastMessageAt: "t" },
        { id: "cnv_2", libraryId: "lib_otra", title: "De otra", titleGenerated: true, turns: 1, createdAt: "t", lastMessageAt: "t" },
      ],
    });
    await mounted();
    await waitFor(() => expect(screen.getByText("De esta")).toBeTruthy());
    expect(screen.queryByText("De otra")).toBeNull();
  });

  it("asks twice before deleting one", async () => {
    chatList.mockResolvedValue({
      conversations: [
        { id: "cnv_1", libraryId: "lib_a", title: "La fe", titleGenerated: true, turns: 2, createdAt: "t", lastMessageAt: "t" },
      ],
    });
    await mounted();
    await waitFor(() => expect(screen.getByText("La fe")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: t("chat.delete") }));
    expect(chatDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: t("chat.confirmDelete") }));
    await waitFor(() => expect(chatDelete).toHaveBeenCalledWith("cnv_1"));
  });
});

describe("coming back to the app", () => {
  const listed = {
    id: "cnv_1",
    libraryId: "lib_a",
    title: "La fe",
    titleGenerated: true,
    turns: 2,
    createdAt: "t",
    lastMessageAt: "t",
  };

  it("reopens the conversation that was being read", async () => {
    // The defect this pins was invisible to every other test here, because none
    // of them mounts with a populated localStorage. In the real window the
    // save effect ran first with `selected === null` and deleted the id before
    // the async restore could read it, so the feature never worked once.
    chatList.mockResolvedValue({ conversations: [listed] });
    saveSelected("cnv_1", "local");
    await mounted();
    await waitFor(() => expect(chatRead).toHaveBeenCalledWith("cnv_1"));
  });

  it("does not erase the remembered id merely by opening the screen", async () => {
    chatList.mockResolvedValue({ conversations: [listed] });
    saveSelected("cnv_1", "local");
    await mounted();
    await waitFor(() => expect(chatRead).toHaveBeenCalled());
    expect(loadSelected("local")).toBe("cnv_1");
  });

  it("forgets it when a new conversation is started", async () => {
    // The one gesture that means "no conversation" rather than "not yet".
    chatList.mockResolvedValue({ conversations: [listed] });
    saveSelected("cnv_1", "local");
    await mounted();
    await waitFor(() => expect(chatRead).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: t("chat.new") }));
    await waitFor(() => expect(loadSelected("local")).toBeNull());
  });

  it("ignores a remembered conversation from another shelf", async () => {
    // The picker is global. Reopening a transcript the picker says nothing
    // about would be a conversation with no visible provenance.
    chatList.mockResolvedValue({
      conversations: [{ ...listed, libraryId: "lib_otra" }],
    });
    saveSelected("cnv_1", "local");
    await mounted();
    await waitFor(() => expect(chatList).toHaveBeenCalled());
    expect(chatRead).not.toHaveBeenCalled();
  });
});

describe("what the screen says while a turn is running", () => {
  it("names the stage the server reported", async () => {
    // The half of this feature that covers the wait. Streamed prose is about 8%
    // of a turn; before stages the other 92% was one unchanging line over four
    // things that can each take seconds.
    willStream([
      { type: "stage", seq: 1, stage: "retrieving" },
    ]);
    await mounted();
    await send("¿?");
    await waitFor(() =>
      expect(screen.getByText(t("chat.stage.retrieving"))).toBeTruthy(),
    );
  });

  it("shows both evidence counts", async () => {
    willStream([
      { type: "stage", seq: 1, stage: "evidence", chunks: 48, dense: 27 },
    ]);
    await mounted();
    await send("¿?");
    await waitFor(() =>
      expect(
        screen.getByText(t("chat.stage.evidence", { chunks: 48, dense: 27 })),
      ).toBeTruthy(),
    );
  });

  it("omits a dense count that was never measured rather than printing zero", async () => {
    // A zero is a real fact about the corpus; a missing figure is not, and
    // rendering it as 0 would claim something false about the retrieval.
    willStream([{ type: "stage", seq: 1, stage: "evidence", chunks: 4 }]);
    await mounted();
    await send("¿?");
    await waitFor(() =>
      expect(
        screen.getByText(t("chat.stage.evidenceUnmeasured", { chunks: 4 })),
      ).toBeTruthy(),
    );
  });

  it("falls back to a readable line for a stage it has no label for", async () => {
    // A worker newer than this build. The sidebar's `nav.chat` showed what the
    // alternative looks like: a raw key on screen.
    willStream([{ type: "stage", seq: 1, stage: "inventada" }]);
    await mounted();
    await send("¿?");
    await waitFor(() => expect(screen.getByText(t("chat.thinking"))).toBeTruthy());
    expect(screen.queryByText(/chat\.stage\./)).toBeNull();
  });

  it("stops showing a stage once prose starts arriving", async () => {
    // A stage label under text that is already growing answers a question the
    // reader can now see for themselves.
    willStream([
      { type: "stage", seq: 1, stage: "generating" },
      { type: "token", seq: 2, text: "La gente es feliz" },
    ]);
    await mounted();
    await send("¿?");
    await waitFor(() => expect(screen.getByText("La gente es feliz")).toBeTruthy());
    expect(screen.queryByText(t("chat.stage.generating"))).toBeNull();
  });
});
