import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../../i18n";
import type { LibraryGraph as Data } from "../../lib/api";
import { LibrariesProvider } from "../../lib/libraries";
import { LibraryGraph } from "./LibraryGraph";

/**
 * The overview's *behaviour*. Its geometry is `lib/force.test.ts`, for the
 * reason `radial.test.ts` states about the document view: jsdom implements no
 * SVG layout, so nothing here can tell whether two circles overlap. What it can
 * tell is whether every node arrived, whether the filters filter, and whether
 * selecting a book lights the right neighbours.
 *
 * Only `api` is replaced; `errorMessage` is pure and worth exercising for real.
 */
const { libraryGraph, exploreClaims, libraries } = vi.hoisted(() => ({
  libraryGraph: vi.fn(),
  exploreClaims: vi.fn(),
  libraries: vi.fn(),
}));

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, libraryGraph, exploreClaims, libraries },
  };
});

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});

/** Two books, three concepts. `con_shared` is in both books, `con_a` only in
 *  the first and `con_b` only in the second — the shape the degree filter acts
 *  on, so a factory with only shared concepts would test half the screen. */
function data(over: Partial<Data> = {}): Data {
  return {
    libraryId: "lib_a",
    semantic: true,
    confidenceFloor: 0.6,
    minDocuments: 2,
    documents: [
      { documentId: "doc_1", versionId: "ver_1", title: "Historia", format: "pdf" },
      { documentId: "doc_2", versionId: "ver_2", title: "Doctrina", format: "pdf" },
    ],
    concepts: [
      { id: "con_shared", name: "Gracia", conceptType: "Doctrina", mentions: 10, documents: 2 },
      { id: "con_a", name: "Concilio", conceptType: null, mentions: 4, documents: 1 },
      { id: "con_b", name: "Bautismo", conceptType: null, mentions: 3, documents: 1 },
    ],
    edges: [
      { versionId: "ver_1", conceptId: "con_shared", mentions: 7, confidence: 0.9 },
      { versionId: "ver_2", conceptId: "con_shared", mentions: 3, confidence: 0.8 },
      { versionId: "ver_1", conceptId: "con_a", mentions: 4, confidence: 0.7 },
      { versionId: "ver_2", conceptId: "con_b", mentions: 3, confidence: 0.7 },
    ],
    truncated: { documents: false, edges: false },
    ...over,
  };
}

/** Scoped to the canvas, not to the document: the legend draws its marks with
 *  the same `g.node` classes on purpose, so both move when the stylesheet does.
 *  An unscoped query counts four swatches as nodes. */
function canvasNodes(): SVGGElement[] {
  const canvas = document.querySelector("svg.graph-canvas:not(.graph-swatch)");
  return Array.from(canvas?.querySelectorAll<SVGGElement>("g.node") ?? []);
}

function node(name: string): SVGGElement {
  const found = canvasNodes().find((g) => g.getAttribute("aria-label") === name);
  if (!found) throw new Error(`no node labelled ${name}`);
  return found;
}

function nodeNames(): string[] {
  return canvasNodes().map((g) => g.getAttribute("aria-label") ?? "");
}

async function mounted() {
  const view = render(
    <LibrariesProvider>
      <LibraryGraph onOpenDocument={open} />
    </LibrariesProvider>,
  );
  await waitFor(() => expect(libraryGraph).toHaveBeenCalled());
  await screen.findByText(t("graph.allNodes"));
  return view;
}

const open = vi.fn();

beforeEach(() => {
  // `restoreMocks` restores spies; these are plain `vi.fn()`s and keep their
  // call log across tests in the same file otherwise.
  vi.clearAllMocks();
  libraries.mockResolvedValue({
    libraries: [{ id: "lib_a", documents: 2, indexedVersions: 2 }],
  });
  libraryGraph.mockResolvedValue(data());
  exploreClaims.mockResolvedValue({
    conceptId: "con_shared",
    semantic: true,
    confidenceFloor: 0.6,
    claims: [
      { id: "clm_1", text: "La gracia precede a la fe.", confidence: 0.9,
        sourceChunkId: "chk_1", status: "afirma" },
    ],
  });
});

afterEach(() => cleanup());

describe("what reaches the canvas", () => {
  it("draws every node in the response", async () => {
    await mounted();
    expect(nodeNames().sort()).toEqual(
      ["Bautismo", "Concilio", "Doctrina", "Gracia", "Historia"].sort(),
    );
  });

  it("asks for the shared subgraph by default", async () => {
    // 10,835 concepts against 1,719 on the real library, measured. Drawing the
    // lot is a click away and is not the default.
    await mounted();
    expect(libraryGraph).toHaveBeenCalledWith("lib_a", 0.6, 2);
  });

  it("says how much is on screen", async () => {
    await mounted();
    await screen.findByText(
      new RegExp(t("graph.libraryCounts", { books: 2, concepts: 3, edges: 4 })),
    );
  });

  it("keeps the model-proposed badge and its threshold", async () => {
    await mounted();
    // Read off the badge itself: the confidence `<select>` offers the same
    // percentages as option text, so the page has several of each.
    const badge = document.querySelector(".semantic");
    expect(badge?.textContent).toContain(t("explore.proposed"));
    expect(badge?.textContent).toContain("≥60%");
  });
});

describe("the controls", () => {
  it("refetches when the degree threshold changes, because the filter is the server's", async () => {
    await mounted();
    fireEvent.change(screen.getByLabelText(t("graph.shared")), {
      target: { value: "1" },
    });
    await waitFor(() => expect(libraryGraph).toHaveBeenCalledWith("lib_a", 0.6, 1));
  });

  it("refetches when the confidence floor changes", async () => {
    await mounted();
    fireEvent.change(screen.getByLabelText(t("graph.confidence")), {
      target: { value: "0.9" },
    });
    await waitFor(() => expect(libraryGraph).toHaveBeenCalledWith("lib_a", 0.9, 2));
  });

  it("hides concepts without dropping the books", async () => {
    await mounted();
    fireEvent.click(screen.getByLabelText(t("graph.filterConcepts")));
    const names = nodeNames();
    expect(names).toContain("Historia");
    expect(names).not.toContain("Gracia");
  });

  it("hides books without dropping the concepts", async () => {
    await mounted();
    fireEvent.click(screen.getByLabelText(t("graph.filterBooks")));
    const names = nodeNames();
    expect(names).toContain("Gracia");
    expect(names).not.toContain("Historia");
  });

  it("marks what the search found, and leaves the rest reachable", async () => {
    // Filtering the canvas down to the hits would hide the neighbourhood, which
    // is the only thing that makes a hit worth looking at.
    await mounted();
    fireEvent.change(screen.getByLabelText(t("graph.search")), {
      target: { value: "grac" },
    });
    expect(node("Gracia").getAttribute("class")).toContain("is-shared");
    expect(node("Bautismo").getAttribute("class")).not.toContain("is-shared");
    expect(nodeNames()).toHaveLength(5);
  });

  it("narrows the list to the search, which is the keyboard path", async () => {
    await mounted();
    fireEvent.change(screen.getByLabelText(t("graph.search")), {
      target: { value: "grac" },
    });
    const list = document.querySelector(".graph-index");
    expect(list?.querySelectorAll("li")).toHaveLength(1);
  });

  it("resets the view without refetching", async () => {
    await mounted();
    const calls = libraryGraph.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: t("graph.reset") }));
    expect(libraryGraph.mock.calls.length).toBe(calls);
  });

  it("keeps every concept at 18px while the graph zooms", async () => {
    await mounted();
    const concept = node("Gracia");
    expect(concept.querySelector("circle.shape")?.getAttribute("r")).toBe("9");

    fireEvent.click(screen.getByRole("button", { name: t("graph.zoomIn") }));
    expect(concept.getAttribute("transform")).toContain(`scale(${1 / 1.3})`);
  });

  it("shows a concept's full name next to its circle on hover", async () => {
    await mounted();
    const concept = node("Gracia");
    expect(concept.querySelector(".concept-label")).toBeNull();

    fireEvent.mouseEnter(concept);
    expect(concept.querySelector(".concept-label")?.textContent).toBe("Gracia");
    expect(concept.getAttribute("class")).toContain("is-hovered");

    fireEvent.mouseLeave(concept);
    expect(concept.getAttribute("class")).not.toContain("is-hovered");
  });
});

describe("selection", () => {
  it("lights a book's concepts and fades the rest", async () => {
    await mounted();
    fireEvent.click(node("Historia"));
    // Historia holds Gracia and Concilio; Bautismo belongs to the other book.
    expect(node("Gracia").getAttribute("class")).not.toContain("is-faded");
    expect(node("Concilio").getAttribute("class")).not.toContain("is-faded");
    expect(node("Bautismo").getAttribute("class")).toContain("is-faded");
  });

  it("lights every book that mentions a concept", async () => {
    await mounted();
    fireEvent.click(node("Gracia"));
    expect(node("Historia").getAttribute("class")).not.toContain("is-faded");
    expect(node("Doctrina").getAttribute("class")).not.toContain("is-faded");
    expect(node("Concilio").getAttribute("class")).toContain("is-faded");
  });

  it("reaches a node from the keyboard", async () => {
    await mounted();
    fireEvent.keyDown(node("Gracia"), { key: "Enter" });
    await screen.findByRole("heading", { name: "Gracia" });
  });

  it("shows a concept's claims", async () => {
    await mounted();
    fireEvent.click(node("Gracia"));
    await screen.findByText("La gracia precede a la fe.");
  });

  it("hands a book to the document view on request", async () => {
    await mounted();
    fireEvent.click(node("Historia"));
    fireEvent.click(await screen.findByRole("button", { name: t("graph.openDocument") }));
    expect(open).toHaveBeenCalledWith(
      expect.objectContaining({ versionId: "ver_1", title: "Historia" }),
    );
  });

  it("reports a concept's true degree, not the edges it happens to hold", async () => {
    // 51 books mention "Dios" on the real library and a capped response carries
    // a handful of its edges. Recomputing the degree from those would understate
    // every concept on screen.
    libraryGraph.mockResolvedValue(
      data({
        concepts: [
          { id: "con_shared", name: "Gracia", conceptType: null, mentions: 10, documents: 51 },
        ],
        edges: [{ versionId: "ver_1", conceptId: "con_shared", mentions: 7, confidence: 0.9 }],
      }),
    );
    await mounted();
    fireEvent.click(node("Gracia"));
    await screen.findByText(t("graph.conceptBooks", { count: 51 }));
  });
});

describe("when there is nothing, or something failed", () => {
  it("says an empty library is empty rather than showing an error", async () => {
    libraryGraph.mockResolvedValue(
      data({ documents: [], concepts: [], edges: [] }),
    );
    render(
      <LibrariesProvider>
        <LibraryGraph />
      </LibrariesProvider>,
    );
    await screen.findByText(t("graph.libraryEmpty"));
    expect(document.querySelector(".error")).toBeNull();
  });

  it("shows the guidance for an unreachable graph", async () => {
    libraryGraph.mockRejectedValue({
      kind: "control_unreachable",
      message: "error sending request for url",
    });
    render(
      <LibrariesProvider>
        <LibraryGraph />
      </LibrariesProvider>,
    );
    await screen.findByText(t("graph.failed"));
    await screen.findByText(t("error.controlUnreachable"));
  });

  it("says so when the response was cut short", async () => {
    libraryGraph.mockResolvedValue(
      data({ truncated: { documents: false, edges: true } }),
    );
    await mounted();
    await screen.findByText(new RegExp(t("graph.truncated")));
  });

  it("keeps the previous graph on screen when a refetch fails", async () => {
    // A failed refetch at a new threshold must not blank a picture that was
    // correct a moment ago.
    await mounted();
    libraryGraph.mockRejectedValue({ kind: "control_status", message: "503" });
    fireEvent.change(screen.getByLabelText(t("graph.shared")), {
      target: { value: "5" },
    });
    await screen.findByText(t("graph.failed"));
    expect(nodeNames()).toContain("Gracia");
  });
});

describe("the accessible list", () => {
  it("holds every node the response carried", async () => {
    await mounted();
    const list = within(document.querySelector(".graph-index") as HTMLElement);
    expect(list.getByRole("button", { name: /Gracia/ })).toBeTruthy();
    expect(list.getByRole("button", { name: /Historia/ })).toBeTruthy();
  });

  it("selects from the list the same way the canvas does", async () => {
    await mounted();
    const list = within(document.querySelector(".graph-index") as HTMLElement);
    fireEvent.click(list.getByRole("button", { name: /Historia/ }));
    await screen.findByRole("heading", { name: "Historia" });
  });
});
