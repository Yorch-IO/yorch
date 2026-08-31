/**
 * Promoting a version by hand.
 *
 * Two things reach this button. An ingest that found a **structural collision**
 * indexes everything and withholds only the promotion — the fingerprint that
 * picks a family profile is structural and structure is not subject matter, and
 * the retrieval metrics provably cannot see the difference because the eval
 * questions come from the very chunks the wrong rules produced. And a bad
 * re-index is rolled back the same way: the previous version stays in the graph
 * deactivated rather than deleted, precisely so the flag can be flipped instead
 * of the pipeline being paid for again.
 *
 * `cleanup` is called by hand — see `AskScreen.test.tsx`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { LibraryScreen } from "./LibraryScreen";

const { libraryDocuments, documentDetail, versionActivate } = vi.hoisted(() => ({
  libraryDocuments: vi.fn(),
  documentDetail: vi.fn(),
  versionActivate: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, libraryDocuments, documentDetail, versionActivate },
  };
});

vi.mock("../lib/libraries", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/libraries")>();
  return {
    ...actual,
    useLibraries: () => ({
      selected: "lib_1", libraries: [], loading: false, error: null,
      select: vi.fn(), reload: vi.fn(),
    }),
  };
});

function version(over: Record<string, unknown> = {}) {
  return {
    id: "ver_1", contentSha256: "a".repeat(64), byteSize: 100, pageCount: null,
    state: "indexed", active: false, createdAt: null, rebuildRunId: "ingest-1",
    alsoHeldBy: [], scores: null, ...over,
  };
}

function detail(versions: Record<string, unknown>[]) {
  return {
    id: "doc_1", libraryId: "lib_1", title: "Catecismo", author: null,
    format: "pdf", sourceKey: "x/catecismo.pdf", sourcePath: "/w/x.pdf",
    present: true, tags: [], activeVersionId: "ver_active",
    canReindex: true, canRebuild: true, versions,
  };
}

afterEach(cleanup);

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage("es");
  versionActivate.mockResolvedValue({ versionId: "ver_1", documents: ["doc_1"] });
  libraryDocuments.mockResolvedValue({
    id: "lib_1", name: "Teología", language: "es",
    documents: [{
      id: "doc_1", libraryId: "lib_1", title: "Catecismo", author: null,
      format: "pdf", sourceKey: "x/catecismo.pdf", present: true,
      versions: 1, activeVersionId: "ver_active", indexedAt: null, tags: [],
    }],
  });
});

async function open() {
  const view = render(<LibraryScreen />);
  await screen.findByText("Catecismo");
  fireEvent.click(screen.getByText("Ver detalles"));
  return view;
}

describe("promoting a version", () => {
  it("offers it on an indexed version that is not the active one", async () => {
    documentDetail.mockResolvedValue(detail([version()]));

    await open();
    await waitFor(() => screen.getByText("Hacer que sea la versión activa"));
    fireEvent.click(screen.getByText("Hacer que sea la versión activa"));

    await waitFor(() => expect(versionActivate).toHaveBeenCalledWith("lib_1", "ver_1"));
  });

  it("does not offer it on the version that is already active", async () => {
    documentDetail.mockResolvedValue(detail([version({ active: true })]));

    await open();
    await waitFor(() => screen.getByText("ver_1"));

    expect(screen.queryByText("Hacer que sea la versión activa")).toBeNull();
  });

  it("does not offer it on a version that was never indexed", async () => {
    // Promoting a version with no vectors would make a document answerable and
    // then answer nothing — a worse state than the one it replaced.
    documentDetail.mockResolvedValue(detail([version({ state: "failed" })]));

    await open();
    await waitFor(() => screen.getByText("ver_1"));

    expect(screen.queryByText("Hacer que sea la versión activa")).toBeNull();
  });
});
