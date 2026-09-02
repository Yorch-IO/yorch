import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../../i18n";
import type { LibraryGraph as Data } from "../../lib/api";
import { BackendProvider } from "../../lib/backend";
import { LibrariesProvider } from "../../lib/libraries";
import { THRESHOLDS } from "../../lib/graphModel";
import { clearGraphCache } from "../../lib/libraryGraphStore";
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

/** Two books and three concepts, one at each side of the default threshold.
 *
 *  `con_shared` reaches four books, `con_a` three and `con_b` one, so the
 *  fixture exercises "drawn at the default", "drawn only when the bar is
 *  lowered" and "in the list but never on the canvas at 3". The degrees are
 *  deliberately larger than the edges present: that is what a capped response
 *  looks like, and `documents` is the number the *database* counted.
 *
 *  Those degrees were 2/1/1 until the degree filter moved to the client. Left
 *  alone they would have drawn nothing at the default of 3, and several
 *  assertions here would have inverted in silence rather than failed. */
function data(over: Partial<Data> = {}): Data {
  return {
    libraryId: "lib_a",
    semantic: true,
    confidenceFloor: 0.6,
    // The envelope is always the widest one; a threshold is a view of it.
    minDocuments: 1,
    documents: [
      { documentId: "doc_1", versionId: "ver_1", title: "Historia", format: "pdf" },
      { documentId: "doc_2", versionId: "ver_2", title: "Doctrina", format: "pdf" },
    ],
    concepts: [
      { id: "con_shared", name: "Gracia", conceptType: "Doctrina", mentions: 10, documents: 4 },
      { id: "con_a", name: "Concilio", conceptType: null, mentions: 4, documents: 3 },
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

/** Move the degree slider to a threshold, by the index it is addressed with. */
function setThreshold(threshold: number): void {
  fireEvent.change(screen.getByLabelText(t("graph.degreeLabel")), {
    target: { value: String(THRESHOLDS.indexOf(threshold as (typeof THRESHOLDS)[number])) },
  });
}

async function mounted() {
  const view = render(
    <BackendProvider>
      <LibrariesProvider>
      <LibraryGraph onOpenDocument={open} active />
    </LibrariesProvider>
    </BackendProvider>,
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

afterEach(() => {
  cleanup();
  // The envelope cache is a module, so it outlives the tree on purpose — that
  // is what makes it survive the remount `App` does on a plane change. Here it
  // would serve the previous test's response to the next one, whose mock has
  // just been cleared, and the fetch assertions would wait for a call that
  // never comes.
  clearGraphCache();
});

describe("what reaches the canvas", () => {
  it("draws every node above the threshold, and leaves the rest in the list", async () => {
    // The degree filter runs here now, not on the server, so the canvas holds
    // what the threshold admits and the response holds everything. `Bautismo`
    // reaches one book, so it cannot join two — which is the one thing this
    // graph is for — and it stays reachable in the list beside the canvas.
    await mounted();
    expect(nodeNames().sort()).toEqual(
      ["Concilio", "Doctrina", "Gracia", "Historia"].sort(),
    );
    const list = within(document.querySelector(".graph-index-groups") as HTMLElement);
    expect(list.getByRole("button", { name: /Bautismo/ })).toBeTruthy();
  });

  it("asks for the whole library once and draws the readable subgraph", async () => {
    // The request is always the widest envelope; the default of 3 is a *view*
    // of it. `library_mentions` counts a concept's degree before it applies
    // `min_documents`, so filtering the envelope here gives the same answer the
    // server would have — `graphModel.test.ts` asserts that equality against a
    // second implementation of the route.
    //
    // Which is why 3 stays the default: on the real library 12,775 concepts
    // fall to 2,034 at >= 2 and to 858 at >= 3, and two thousand nodes is not a
    // picture. Drawing the lot is now a control away and costs no request.
    await mounted();
    expect(libraryGraph).toHaveBeenCalledWith("lib_a", 0.6, 1);
    expect(libraryGraph).toHaveBeenCalledTimes(1);
  });

  it("says how much is on screen", async () => {
    await mounted();
    await screen.findByText(
      // What is *drawn*, not what was fetched: at the default threshold
      // `Bautismo` and its edge are in memory and off the canvas.
      new RegExp(t("graph.libraryCounts", { books: 2, concepts: 2, edges: 3 })),
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
  it("changes the degree threshold without touching the network", async () => {
    await mounted();
    const calls = libraryGraph.mock.calls.length;
    setThreshold(1);
    // The picture changes...
    await waitFor(() => expect(nodeNames()).toContain("Bautismo"));
    // ...and nothing was asked of the server to do it.
    expect(libraryGraph.mock.calls.length).toBe(calls);
  });

  it("labels every stop of the slider with what it would draw", async () => {
    // The counts come from a histogram built once per envelope, so moving the
    // control derives nothing — which is what lets it update on every step.
    await mounted();
    const slider = screen.getByLabelText(t("graph.degreeLabel")) as HTMLInputElement;
    // The value is an index, not a threshold.
    expect(slider.value).toBe(String(THRESHOLDS.indexOf(3)));

    const shown = (n: number) => t("graph.degreeValueSome", { n, shown: 2, total: 3 });
    expect(screen.getByText(shown(3))).toBeTruthy();

    setThreshold(1);
    expect(
      screen.getByText(t("graph.degreeValueAll", { n: 1, shown: 3, total: 3 })),
    ).toBeTruthy();
  });

  it("narrows the canvas to one book and the concepts it mentions", async () => {
    // The document view's question, asked without leaving the map.
    await mounted();
    setThreshold(1);
    fireEvent.change(screen.getByLabelText(t("graph.onlyBook")), {
      target: { value: "ver_2" },
    });
    // Awaited, because the widest layout is now computed *last*: it costs an
    // order of magnitude more than the others (~8.8 s against 364 ms on the
    // real library), so everything else is ready first and "every book" fills
    // in when it lands. Until then `chooseLayout` draws with the widest layout
    // that exists and leaves the nodes it has no position for out — which is
    // exactly what this waits through.
    await waitFor(() => expect(nodeNames()).toContain("Bautismo"));
    const names = nodeNames();
    expect(names).toContain("Doctrina");
    expect(names).not.toContain("Historia");
    // ver_2 mentions con_shared and con_b, and not con_a.
    expect(names).not.toContain("Concilio");
  });

  it("hides what the search did not find, but only when asked", async () => {
    // Marking stays the default: cutting the canvas down to the hits hides the
    // neighbourhood, which is the only thing that makes a hit worth looking at.
    await mounted();
    fireEvent.change(screen.getByLabelText(t("graph.search")), {
      target: { value: "grac" },
    });
    expect(nodeNames()).toContain("Concilio");

    fireEvent.click(screen.getByLabelText(t("graph.searchOnly")));
    expect(nodeNames()).toEqual(["Gracia"]);
  });

  it("offers the hard filter only when there is something to filter by", async () => {
    await mounted();
    expect((screen.getByLabelText(t("graph.searchOnly")) as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(t("graph.search")), {
      target: { value: "grac" },
    });
    expect((screen.getByLabelText(t("graph.searchOnly")) as HTMLInputElement).disabled).toBe(false);
  });

  it("hides what joins nothing once the other filters have run", async () => {
    // A third book with nothing in the graph, because the shared fixture's two
    // are both connected — and a filter that removes nothing proves nothing.
    // `library_documents` takes no threshold, so the server keeps a book like
    // this and so must the canvas until a reader says otherwise.
    libraryGraph.mockResolvedValue(
      data({
        documents: [
          { documentId: "doc_1", versionId: "ver_1", title: "Historia", format: "pdf" },
          { documentId: "doc_2", versionId: "ver_2", title: "Doctrina", format: "pdf" },
          { documentId: "doc_3", versionId: "ver_3", title: "Sin proyectar", format: "pdf" },
        ],
      }),
    );
    await mounted();
    expect(nodeNames()).toContain("Sin proyectar");

    fireEvent.click(screen.getByLabelText(t("graph.hideIsolated")));
    expect(nodeNames()).not.toContain("Sin proyectar");
    expect(nodeNames()).toContain("Historia");
    expect(nodeNames()).toContain("Gracia");
  });

  it("reloads the envelope on request, which resetting the view does not", async () => {
    await mounted();
    const calls = libraryGraph.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: t("graph.refresh") }));
    await waitFor(() => expect(libraryGraph.mock.calls.length).toBe(calls + 1));
  });

  it("refetches when the confidence floor changes", async () => {
    await mounted();
    fireEvent.change(screen.getByLabelText(t("graph.confidence")), {
      target: { value: "0.9" },
    });
    // Still the server's: a concept's degree is counted *after* the floor
    // predicate, so no client can re-derive it from an envelope fetched at
    // another floor. The third argument is 1 because the envelope is always the
    // widest one.
    await waitFor(() => expect(libraryGraph).toHaveBeenCalledWith("lib_a", 0.9, 1));
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
    expect(node("Concilio").getAttribute("class")).not.toContain("is-shared");
    expect(nodeNames()).toHaveLength(4);
  });

  it("narrows the list to the search, which is the keyboard path", async () => {
    await mounted();
    fireEvent.change(screen.getByLabelText(t("graph.search")), {
      target: { value: "grac" },
    });
    const list = document.querySelector(".graph-index-groups");
    expect(list?.querySelectorAll("li")).toHaveLength(1);
  });

  it("puts the canvas under the SVG, in the shape the stylesheet selects", async () => {
    // Not decoration, and the one property here a screenshot would find far
    // too late. Two things the stylesheet depends on and nothing else asserts:
    //
    // - The SVG is a child of `.graph-stage`, which is a child of
    //   `.graph-main`. `.graph-main > .graph-canvas` — which the *document*
    //   view still matches — would otherwise stop matching this one, and the
    //   canvas would fall back to a replaced element's default 300x150.
    // - The edge canvas comes *before* the SVG and is marked `aria-hidden`. An
    //   absolutely positioned element paints above a static in-flow sibling, so
    //   the stacking is what keeps the nodes on top of their own edges.
    await mounted();
    const stage = document.querySelector(".graph-main > .graph-stage");
    expect(stage).toBeTruthy();
    expect(stage?.querySelector(":scope > canvas.graph-edges")).toBeTruthy();
    expect(stage?.querySelector(":scope > svg.graph-canvas")).toBeTruthy();
    expect(stage?.firstElementChild?.tagName.toLowerCase()).toBe("canvas");
    expect(stage?.querySelector("canvas")?.getAttribute("aria-hidden")).toBe("true");
    // And no edge survives in the DOM: that is the element budget this change
    // exists for — 17,814 of them at the widest threshold on the real library.
    // Scoped past the legend, which draws its marks with the same classes on
    // purpose so they cannot drift from the real ones.
    expect(
      document.querySelectorAll("svg.graph-canvas:not(.graph-swatch) line.edge"),
    ).toHaveLength(0);
  });

  it("drags the picture as far as the pointer went, within one frame", async () => {
    // What this pins is the *arithmetic*: `pan` is applied inside a group whose
    // units are viewBox units, and it was being fed client pixels. At a 600 px
    // canvas for a 1,200-unit viewBox that is a drag running at twice the speed
    // of the hand. Checked by reverting: without the division this reads 60.
    //
    // What it cannot pin is the reason the gesture is imperative at all. A
    // `setPan` per pointermove reaches the same transform; it just re-executes
    // the render function and reconciles every node on the way, and jsdom
    // measures no cost, so a test cannot tell the two apart. Verified by
    // reverting as well — the state version passes this. The mechanism's
    // guarantee is structural and lives in the component's comment.
    await mounted();
    const svg = document.querySelector("svg.graph-canvas:not(.graph-swatch)") as SVGSVGElement;
    svg.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 600, height: 410, right: 600, bottom: 410, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;

    const group = () => svg.querySelector("g") as SVGGElement;
    const pan = (el: SVGGElement) => {
      const m = /translate\(([-\d.]+) ([-\d.]+)\)/.exec(el.getAttribute("transform") ?? "");
      return { x: Number(m?.[1]), y: Number(m?.[2]) };
    };

    expect(pan(group())).toEqual({ x: 0, y: 0 });

    fireEvent.pointerDown(svg, { clientX: 100, clientY: 100, pointerId: 1 });
    fireEvent.pointerMove(svg, { clientX: 160, clientY: 100, pointerId: 1 });
    await act(async () => {
      await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
    });

    // 60 client pixels on a canvas drawn at half scale is 120 viewBox units.
    expect(pan(group()).x).toBeCloseTo(120, 3);
    expect(pan(group()).y).toBeCloseTo(0, 3);

    // And the state catches up on release, so the tree and the attribute agree.
    fireEvent.pointerUp(svg, { clientX: 160, clientY: 100, pointerId: 1 });
    expect(pan(group()).x).toBeCloseTo(120, 3);
  });

  it("zooms with the wheel, towards the pointer", async () => {
    // **The first test the wheel has ever had, and it fails against the code
    // that shipped.** The listener was bound in an effect depending on
    // `[zoomBy]`, whose identity never changed, so it ran once at mount — when
    // the `<svg>` had not been rendered yet — found a null ref and never ran
    // again. Nothing zoomed, and nothing said so.
    await mounted();
    const svg = document.querySelector("svg.graph-canvas:not(.graph-swatch)") as SVGSVGElement;

    // jsdom measures everything as 0x0, and the pointer mapping is a function
    // of the box. A real one is stubbed so the assertion is about the zoom
    // rather than about jsdom's idea of layout.
    svg.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 1200, height: 820, right: 1200, bottom: 820, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;

    const group = svg.querySelector("g") as SVGGElement;
    const read = (el: SVGGElement) => {
      const m = /translate\(([-\d.]+) ([-\d.]+)\) scale\(([-\d.]+)\)/.exec(
        el.getAttribute("transform") ?? "",
      );
      if (m === null) throw new Error(`unreadable transform: ${el.getAttribute("transform")}`);
      return { x: Number(m[1]), y: Number(m[2]), zoom: Number(m[3]) };
    };

    const at = { x: 900, y: 200 };
    const before = read(group);
    const under = (v: { x: number; y: number; zoom: number }) => ({
      x: (at.x - v.x) / v.zoom,
      y: (at.y - v.y) / v.zoom,
    });

    const event = new WheelEvent("wheel", { deltaY: -100, clientX: at.x, clientY: at.y, cancelable: true, bubbles: true });
    // Through `fireEvent` rather than `dispatchEvent`, so the state the handler
    // sets is flushed before the transform is read. The listener itself is a
    // plain DOM one either way — that is the point of it.
    fireEvent(svg, event);

    const after = read(svg.querySelector("g") as SVGGElement);
    // It zoomed at all — which is the part that never happened...
    expect(after.zoom).toBeGreaterThan(before.zoom);
    // ...and whatever was under the pointer is still under it.
    expect(under(after).x).toBeCloseTo(under(before).x, 3);
    expect(under(after).y).toBeCloseTo(under(before).y, 3);
    // And the page does not scroll out from under the canvas.
    expect(event.defaultPrevented).toBe(true);
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

  it("shows a concept's full name on hover, from one overlay rather than per node", async () => {
    // The name used to be a `<text>` rendered inside the hovered node, gated on
    // a piece of state — so every node the pointer crossed re-rendered the
    // whole screen. It is one element for the whole canvas now, moved and
    // filled by a delegated listener, and the property is the same one: hover a
    // concept and its full name is beside it.
    await mounted();
    const concept = node("Gracia");
    const overlay = document.querySelector(".graph-hover") as SVGGElement;
    expect(overlay.getAttribute("class")).not.toContain("is-showing");

    fireEvent.pointerOver(concept.querySelector("circle.shape") as Element);
    expect(concept.getAttribute("class")).toContain("is-hovered");
    expect(overlay.querySelector("text")?.textContent).toBe("Gracia");
    expect(overlay.getAttribute("class")).toContain("is-showing");
    // It sits where the node sits — position only, since the counter-scale is
    // now the overlay's own child group rather than copied off the node, which
    // is what lets a hovered *book* (whose own transform carries no scale at
    // all) keep a fixed screen size too.
    const [translate] = concept.getAttribute("transform")?.match(/translate\([^)]*\)/) ?? [];
    expect(overlay.getAttribute("transform")).toBe(translate);

    // And there is exactly one of them, whatever the graph holds.
    expect(document.querySelectorAll(".graph-hover")).toHaveLength(1);
  });

  it("lets go of the hover when the pointer leaves the canvas", async () => {
    await mounted();
    const concept = node("Gracia");
    const group = document.querySelector("svg.graph-canvas:not(.graph-swatch) > g") as SVGGElement;
    fireEvent.pointerOver(concept.querySelector("circle.shape") as Element);
    expect(concept.getAttribute("class")).toContain("is-hovered");

    fireEvent.pointerLeave(group);
    expect(concept.getAttribute("class")).not.toContain("is-hovered");
    expect(
      (document.querySelector(".graph-hover") as SVGGElement).getAttribute("class"),
    ).not.toContain("is-showing");
  });

  it("does not chase names while the picture is being dragged", async () => {
    // A drag crosses everything on the way, and what the reader is doing is
    // moving the picture, not reading a label.
    await mounted();
    const svg = document.querySelector("svg.graph-canvas:not(.graph-swatch)") as SVGSVGElement;
    const concept = node("Gracia");

    fireEvent.pointerDown(svg, { clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerOver(concept.querySelector("circle.shape") as Element);
    expect(concept.getAttribute("class")).not.toContain("is-hovered");

    fireEvent.pointerUp(svg, { clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerOver(concept.querySelector("circle.shape") as Element);
    expect(concept.getAttribute("class")).toContain("is-hovered");
  });
});

describe("selection", () => {
  it("lights a book's concepts and fades the rest", async () => {
    await mounted();
    fireEvent.click(node("Historia"));
    // Historia holds Gracia and Concilio; Doctrina is the book that does not
    // share Concilio. Bautismo is below the threshold and off the canvas.
    expect(node("Gracia").getAttribute("class")).not.toContain("is-faded");
    expect(node("Concilio").getAttribute("class")).not.toContain("is-faded");
    expect(node("Doctrina").getAttribute("class")).toContain("is-faded");
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
      <BackendProvider>
      <LibrariesProvider>
        <LibraryGraph active />
      </LibrariesProvider>
    </BackendProvider>,
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
      <BackendProvider>
      <LibrariesProvider>
        <LibraryGraph active />
      </LibrariesProvider>
    </BackendProvider>,
    );
    await screen.findByText(t("graph.failed"));
    await screen.findByText(t("error.controlUnreachable"));
  });

  it("says a view was cut short only when that view was", async () => {
    // Derived per threshold rather than copied from the envelope. The response
    // was cut, so the widest view is short — but the default view's rows all
    // fit, and "recortado" over a whole picture is a claim about the corpus
    // that is not true.
    libraryGraph.mockResolvedValue(
      data({ truncated: { documents: false, edges: true } }),
    );
    await mounted();
    expect(screen.queryByText(new RegExp(t("graph.truncated")))).toBeNull();

    setThreshold(1);
    await screen.findByText(new RegExp(t("graph.truncated")));
  });

  it("keeps the previous graph on screen when a refetch fails", async () => {
    // A failed refetch at a new threshold must not blank a picture that was
    // correct a moment ago.
    await mounted();
    libraryGraph.mockRejectedValue({ kind: "control_status", message: "503" });
    // The confidence floor, because it is the only control that still asks.
    fireEvent.change(screen.getByLabelText(t("graph.confidence")), {
      target: { value: "0.9" },
    });
    await screen.findByText(t("graph.failed"));
    expect(nodeNames()).toContain("Gracia");
  });
});

describe("the accessible list", () => {
  it("holds every node the response carried", async () => {
    await mounted();
    const list = within(document.querySelector(".graph-index-groups") as HTMLElement);
    expect(list.getByRole("button", { name: /Gracia/ })).toBeTruthy();
    expect(list.getByRole("button", { name: /Historia/ })).toBeTruthy();
  });

  it("selects from the list the same way the canvas does", async () => {
    await mounted();
    const list = within(document.querySelector(".graph-index-groups") as HTMLElement);
    fireEvent.click(list.getByRole("button", { name: /Historia/ }));
    await screen.findByRole("heading", { name: "Historia" });
  });

  it("grows in place when the reader scrolls near the bottom, rather than paginating", async () => {
    // 250 concepts, all sharing one type, so the list holds more than the
    // initial 200-row cap and the second page is reachable only by growing it.
    const concepts = Array.from({ length: 250 }, (_, i) => ({
      id: `con_${i}`,
      name: `Concepto ${String(i).padStart(3, "0")}`,
      conceptType: "Doctrina",
      mentions: 1,
      documents: 1,
    }));
    libraryGraph.mockResolvedValue(
      data({
        concepts,
        edges: concepts.map((c) => ({
          versionId: "ver_1",
          conceptId: c.id,
          mentions: 1,
          confidence: 0.9,
        })),
      }),
    );
    await mounted();
    const groups = document.querySelector(".graph-index-groups") as HTMLElement;
    expect(within(groups).queryByRole("button", { name: "Concepto 249" })).toBeNull();

    Object.defineProperty(groups, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(groups, "clientHeight", { value: 500, configurable: true });
    Object.defineProperty(groups, "scrollTop", { value: 600, configurable: true });
    fireEvent.scroll(groups);

    expect(within(groups).getByRole("button", { name: "Concepto 249" })).toBeTruthy();
  });

  it("colours every drawn concept by its structural cluster, in the list and on the canvas", async () => {
    // Not by `conceptType`: measured on the running corpus that field is free
    // text with 1,619 distinct values after folding case and accents, so the
    // colour comes from `clusterConcepts` — which books connect a concept to
    // others — instead. Both "Gracia" and "Concilio" are drawn at the default
    // threshold (`the accessible list` fixture data), and every drawn concept
    // gets a cluster, merged together or not.
    await mounted();
    const list = within(document.querySelector(".graph-index-groups") as HTMLElement);
    const listDot = list.getByRole("button", { name: /Gracia/ }).querySelector(".node-dot");
    const canvasClass = node("Gracia").getAttribute("class") ?? "";
    expect(listDot?.className).toMatch(/type-\d/);
    expect(canvasClass).toMatch(/type-\d/);
    // The same cluster gets the same colour in both places.
    const slot = /type-(\d)/.exec(canvasClass)?.[1];
    expect(listDot?.className).toContain(`type-${slot}`);
    expect(node("Concilio").getAttribute("class")).toMatch(/type-\d/);

    // "Gracia" and "Concilio" are the only two concepts present at this
    // threshold and share no book, so each is its own group — and a group is
    // named after its own most-mentioned concepts rather than numbered, so
    // the legend reads with the concepts themselves in it.
    const legend = within(document.querySelector(".graph-type-legend") as HTMLElement);
    expect(legend.getByText(t("graph.clusterNamed", { name: "Gracia", count: 1 }))).toBeTruthy();
    expect(legend.getByText(t("graph.clusterNamed", { name: "Concilio", count: 1 }))).toBeTruthy();
  });

  it("writes each group's name across the region the layout gave it", () => {
    // The canvas half of the same naming. Under the nodes and inert to the
    // pointer, because it names ground rather than a thing: a name lying over
    // a concept must not take the click meant for it.
    //
    // jsdom lays out no SVG, so what is asserted is the contract the
    // stylesheet selects on and the coordinates the component computed — not
    // that the words land anywhere in particular, which only a window can say.
    return mounted().then(() => {
      const regions = document.querySelector(".graph-canvas .graph-regions");
      expect(regions).not.toBeNull();
      const canvasChildren = [...(regions?.parentElement?.children ?? [])];
      expect(canvasChildren.indexOf(regions as Element)).toBe(0);
      expect(regions?.getAttribute("aria-hidden")).toBe("true");
      // The fixture's two groups hold one concept each, which is below
      // `CLUSTER_MARK_MIN` — a name floating over a single dot labels nothing,
      // so the layer is present and empty rather than absent.
      expect(regions?.querySelectorAll("text")).toHaveLength(0);
    });
  });
});
