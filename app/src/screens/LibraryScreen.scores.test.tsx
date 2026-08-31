/**
 * What a version row says about how well its index can be searched.
 *
 * The property that matters is not that the numbers render — it is that they
 * render *only when a run measured them*, and that they never render as zeros.
 * Every version on this installation predates the stage, so "recall 0.00" would
 * be shown for all seventy of them and would send somebody to fix an index that
 * is fine. `null` means "nobody asked", and that is a different statement.
 *
 * Testing Library's automatic `cleanup` is not registered in this project, so
 * this file calls it by hand — see `AskScreen.test.tsx`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { LibraryScreen } from "./LibraryScreen";

const { libraryDocuments, documentDetail } = vi.hoisted(() => ({
  libraryDocuments: vi.fn(),
  documentDetail: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, libraryDocuments, documentDetail },
  };
});

vi.mock("../lib/libraries", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/libraries")>();
  return {
    ...actual,
    useLibraries: () => ({
      selected: "lib_1",
      libraries: [],
      loading: false,
      error: null,
      select: vi.fn(),
      reload: vi.fn(),
    }),
  };
});

const SCORES = {
  recallAt1: 0.675,
  recallAt5: 0.9,
  mrrAt10: 0.7642,
  recallAt5DenseOnly: 1.0,
  noiseFloor: 0.6232,
  chunks: 274,
  evalQuestions: 40,
  margin: 0.0771,
  leakage: "hybrid 0.900 vs dense 1.000",
  misses: 4,
};

function version(over: Record<string, unknown> = {}) {
  return {
    id: "ver_1",
    contentSha256: "a".repeat(64),
    byteSize: 100,
    pageCount: null,
    state: "indexed",
    active: true,
    createdAt: null,
    rebuildRunId: "ingest-1",
    alsoHeldBy: [],
    scores: null,
    ...over,
  };
}

afterEach(cleanup);

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage("es");
  libraryDocuments.mockResolvedValue({
    id: "lib_1",
    name: "Teología",
    language: "es",
    documents: [
      {
        id: "doc_1", libraryId: "lib_1", title: "Catecismo", author: null,
        format: "pdf", sourceKey: "x/catecismo.pdf", present: true,
        versions: 1, activeVersionId: "ver_1", indexedAt: null, tags: [],
      },
    ],
  });
});

async function openTheDocument() {
  const view = render(<LibraryScreen />);
  await screen.findByText("Catecismo");
  fireEvent.click(screen.getByText("Ver detalles"));
  return view;
}

describe("measured retrieval quality on a version", () => {
  it("shows the figures when a run measured them", async () => {
    documentDetail.mockResolvedValue({
      id: "doc_1", libraryId: "lib_1", title: "Catecismo", author: null,
      format: "pdf", sourceKey: "x/catecismo.pdf", sourcePath: "/w/x.pdf",
      present: true, tags: [], activeVersionId: "ver_1",
      canReindex: true, canRebuild: true,
      versions: [version({ scores: SCORES })],
    });

    await openTheDocument();

    await waitFor(() => expect(screen.getByText("recall@5")).toBeTruthy());
    expect(screen.getByText("90%")).toBeTruthy();
    expect(screen.getByText("0.764")).toBeTruthy();
  });

  it("shows the noise floor and the dense-only leg beside the recall", async () => {
    // Not extras. The questions are written from the chunks they must find, so
    // the hybrid figure alone flatters the index; and recall says how often the
    // right chunk came back while the floor says what a wrong one scores.
    documentDetail.mockResolvedValue({
      id: "doc_1", libraryId: "lib_1", title: "Catecismo", author: null,
      format: "pdf", sourceKey: "x/catecismo.pdf", sourcePath: "/w/x.pdf",
      present: true, tags: [], activeVersionId: "ver_1",
      canReindex: true, canRebuild: true,
      versions: [version({ scores: SCORES })],
    });

    await openTheDocument();

    await waitFor(() => expect(screen.getByText("piso de ruido")).toBeTruthy());
    expect(screen.getByText("0.623")).toBeTruthy();
    expect(screen.getByText("solo denso")).toBeTruthy();
    expect(screen.getByText("100%")).toBeTruthy();
  });

  it("says nothing at all about a version nobody measured", async () => {
    documentDetail.mockResolvedValue({
      id: "doc_1", libraryId: "lib_1", title: "Catecismo", author: null,
      format: "pdf", sourceKey: "x/catecismo.pdf", sourcePath: "/w/x.pdf",
      present: true, tags: [], activeVersionId: "ver_1",
      canReindex: true, canRebuild: true,
      versions: [version()],
    });

    const { container } = await openTheDocument();
    await waitFor(() => expect(screen.getByText("ver_1")).toBeTruthy());

    expect(container.querySelector(".scores")).toBeNull();
    // The specific thing that must not happen: a zero rendered as a measurement.
    expect(screen.queryByText("0%")).toBeNull();
    expect(screen.queryByText("recall@5")).toBeNull();
  });
});
