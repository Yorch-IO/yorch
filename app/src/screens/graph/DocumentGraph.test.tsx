import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../../i18n";
import type { Claim, Concept, DocumentRow, RelatedDocument } from "../../lib/api";
import { BackendProvider } from "../../lib/backend";
import { LibrariesProvider } from "../../lib/libraries";
import { DocumentGraph } from "./DocumentGraph";

/**
 * The document view's *behaviour*. Its geometry is in `lib/radial.test.ts`, because
 * jsdom implements no SVG layout — no `getBBox`, no resolved `transform` — so
 * nothing here can tell whether two labels overlap. What it can tell is whether
 * a node exists, carries an accessible name, responds to a key, and puts the
 * right thing in the panel. Between the two files the picture and the behaviour
 * are both covered; from either alone, neither is.
 *
 * Only `api` is replaced. `errorMessage` is pure and worth exercising for real.
 */
const { libraryDocuments, exploreConcepts, exploreRelated, exploreClaims, libraries } =
  vi.hoisted(() => ({
    libraryDocuments: vi.fn(),
    exploreConcepts: vi.fn(),
    exploreRelated: vi.fn(),
    exploreClaims: vi.fn(),
    libraries: vi.fn(),
  }));

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      libraryDocuments,
      exploreConcepts,
      exploreRelated,
      exploreClaims,
      libraries,
    },
  };
});

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});

const FLOOR = 0.4;

function document_(over: Partial<DocumentRow> = {}): DocumentRow {
  return {
    id: "doc_1",
    title: "Historia de la iglesia",
    author: null,
    format: "pdf",
    sourceKey: "historia.pdf",
    present: true,
    tags: [],
    activeVersionId: "ver_1",
    updatedAt: null,
    ...over,
  };
}

function concept(i: number, over: Partial<Concept> = {}): Concept {
  return {
    id: `con_${i}`,
    name: `concepto ${i}`,
    conceptType: "doctrina",
    mentions: i + 1,
    confidence: 0.8,
    ...over,
  };
}

function relatedDoc(i: number, over: Partial<RelatedDocument> = {}): RelatedDocument {
  // Neighbour i shares concepts 0..i, so a test can name exactly which nodes
  // must light up and which must fade — and `sharedConcepts` is the length of
  // that list, the way the template returns it.
  const shared = Array.from({ length: i + 1 }, (_, k) => k);
  return {
    id: `ver_r${i}`,
    title: `vecino ${i}`,
    documentId: `doc_r${i}`,
    sharedConcepts: shared.length,
    sharedConceptIds: shared.map((k) => `con_${k}`),
    sharedConceptNames: shared.map((k) => `concepto ${k}`),
    ...over,
  };
}

function claim(i: number, over: Partial<Claim> = {}): Claim {
  return {
    id: `cl_${i}`,
    text: `la afirmación ${i}`,
    confidence: 0.66,
    sourceChunkId: `chk_${i}`,
    // The state the API returns for a claim extracted before the field existed.
    // Chosen as the default here on purpose: most claims in a real graph today
    // are exactly that, and a factory defaulting to `afirma` would test the
    // pleasant case only.
    status: "sin_estado",
    ...over,
  };
}

/** The screen draws twelve concepts and eight documents at most, and several
 *  properties below are only interesting at those counts. */
const CONCEPTS = Array.from({ length: 12 }, (_, i) => concept(i));
const RELATED = Array.from({ length: 8 }, (_, i) => relatedDoc(i));

/** Render, and wait for the provider's one library fetch to land. Until it does
 *  there is no selected library and the shelf is never asked for. */
async function mounted() {
  render(
    <BackendProvider>
      <LibrariesProvider>
      <DocumentGraph />
    </LibrariesProvider>
    </BackendProvider>,
  );
  await waitFor(() => expect(libraryDocuments).toHaveBeenCalled());
}

/** Choose a document in the picker and wait for both projection reads. */
async function pick(versionId = "ver_1") {
  fireEvent.change(screen.getByLabelText(t("graph.centre")), {
    target: { value: versionId },
  });
  await waitFor(() => expect(exploreRelated).toHaveBeenCalled());
}

async function centred() {
  await mounted();
  await pick();
  await screen.findByLabelText(t("graph.node.concept", { name: "concepto 0", mentions: 1 }));
}

const conceptNode = (i: number) =>
  screen.getByLabelText(
    t("graph.node.concept", { name: `concepto ${i}`, mentions: i + 1 }),
  );

const docNode = (i: number) =>
  screen.getByLabelText(t("graph.node.doc", { title: `vecino ${i}`, count: i + 1 }));

/** The same card once it is being compared. Its accessible name changes on
 *  purpose — a card that can be pressed again to close should say it is
 *  pressed — so the helper has to change with it. */
const selectedDocNode = (i: number) =>
  screen.getByLabelText(
    t("graph.node.docSelected", { title: `vecino ${i}`, count: i + 1 }),
  );

/** The canvas. Nameless on purpose: its accessible name embeds the centred
 *  document's title and changes on every re-centre, so matching on it would
 *  make every helper depend on which document is drawn. The name itself is
 *  asserted once, where it is the point. */
const canvas = () => screen.getByRole("group");

/** The centre card. Queried by class rather than by text: its title also
 *  appears in its own `<title>` and in the picker's `<option>`, so a text query
 *  matches three nodes and says nothing about which. */
const centreCard = (): Element => {
  const node = canvas().querySelector(".node.centre");
  if (node === null) throw new Error("no centre card");
  return node;
};

const picker = () => screen.getByLabelText(t("graph.centre")) as HTMLSelectElement;

beforeEach(() => {
  window.localStorage.clear();
  libraries.mockResolvedValue({
    libraries: [{ id: "lib_a", documents: 3, indexedVersions: 3 }],
  });
  libraryDocuments.mockResolvedValue({
    libraryId: "lib_a",
    documents: [document_(), document_({ id: "doc_2", title: "Sin versión", activeVersionId: null })],
  });
  exploreConcepts.mockResolvedValue({
    versionId: "ver_1",
    semantic: true,
    confidenceFloor: FLOOR,
    concepts: CONCEPTS,
  });
  exploreRelated.mockResolvedValue({
    versionId: "ver_1",
    semantic: true,
    confidenceFloor: FLOOR,
    documents: RELATED,
  });
  exploreClaims.mockResolvedValue({
    conceptId: "con_0",
    semantic: true,
    confidenceFloor: FLOOR,
    claims: [claim(1), claim(2)],
  });
});

afterEach(() => {
  // Vitest exposes no global afterEach here, so RTL's automatic cleanup never
  // registers itself. Without this the second test renders into a document that
  // still holds the first one's tree.
  cleanup();
  vi.clearAllMocks();
});

describe("before a document is chosen", () => {
  it("draws nothing and says so", async () => {
    await mounted();
    expect(screen.getByText(t("graph.empty"))).toBeTruthy();
    expect(exploreConcepts).not.toHaveBeenCalled();
    expect(screen.queryByRole("group")).toBeNull();
  });

  it("offers only documents that have an indexed version", async () => {
    await mounted();
    await waitFor(() => expect(picker().options).toHaveLength(2));
    // The placeholder, and the one document with an activeVersionId. "Sin
    // versión" has nothing to centre on.
    expect([...picker().options].map((o) => o.textContent)).toEqual([
      t("graph.pick"),
      "Historia de la iglesia",
    ]);
  });
});

describe("zoom", () => {
  it("keeps concept circles and labels at their fixed screen size", async () => {
    await centred();
    const concept = conceptNode(0);
    expect(concept.querySelector("circle.shape")?.getAttribute("r")).toBe("9");

    fireEvent.click(screen.getByRole("button", { name: t("graph.zoomIn") }));
    expect(concept.getAttribute("transform")).toContain("scale(1.3)");
    expect(concept.getAttribute("transform")).toContain(`scale(${1 / 1.3})`);

    fireEvent.click(screen.getByRole("button", { name: t("graph.reset") }));
    expect(concept.getAttribute("transform")).toContain("scale(1) translate");
  });
});

describe("the drawn graph", () => {
  it("draws one node per concept and one per related document", async () => {
    await centred();
    for (let i = 0; i < 12; i += 1) expect(conceptNode(i), `concept ${i}`).toBeTruthy();
    for (let i = 0; i < 8; i += 1) expect(docNode(i), `document ${i}`).toBeTruthy();
  });

  it("names the centre and counts what surrounds it", async () => {
    await centred();
    expect(centreCard().textContent).toContain("Historia de la iglesia");
    expect(centreCard().textContent).toContain(
      t("graph.centreMeta", { concepts: 12, related: 8 }),
    );
  });

  it("says a model proposed all of it, and names the floor", async () => {
    await centred();
    const badge = screen.getByTitle(
      `${t("explore.proposedHelp")} ${t("graph.floor", { floor: 40 })}`,
    );
    expect(badge.textContent).toContain(t("explore.proposed"));
    expect(badge.textContent).toContain("40%");
  });

  it("is a group rather than an image, so its buttons are reachable", async () => {
    await centred();
    // `role="img"` makes SVG contents presentational, which silenced twenty
    // `role="button"` children — they kept focus and lost their names.
    expect(
      screen.getByRole("group", {
        name: t("graph.alt", { title: "Historia de la iglesia" }),
      }),
    ).toBeTruthy();
    expect(canvas().tagName.toLowerCase()).toBe("svg");
    expect(canvas().querySelectorAll('[role="button"]')).toHaveLength(20);
  });

  it("renders the legend and keeps the note about the edge it will not draw", async () => {
    await centred();
    for (const k of ["concept", "doc", "weight", "dashed"]) {
      expect(screen.getByText(t(`graph.legend.${k}`)), k).toBeTruthy();
    }
    expect(screen.getByText(t("graph.proposed"))).toBeTruthy();
  });
});

describe("the claims behind a concept", () => {
  it("fetches and lists them, with the fragment each can be checked against", async () => {
    await centred();
    fireEvent.click(conceptNode(3));
    await waitFor(() => expect(exploreClaims).toHaveBeenCalledWith("con_3"));

    expect(await screen.findByText("la afirmación 1")).toBeTruthy();
    expect(screen.getByText("la afirmación 2")).toBeTruthy();
    expect(screen.getAllByText("66%")).toHaveLength(2);
    // Fetched since this screen was written and never shown until now.
    expect(screen.getByText(t("graph.sourceChunk", { id: "chk_1" }))).toBeTruthy();
  });

  it("says what the document does with a claim, and quotes it when it can", async () => {
    // A doctrine a text is about to rebut is written with the same words as one
    // it holds. Without the state on screen the two are indistinguishable, and
    // the quote is the only line here a reader can check without opening
    // anything — the claim above it is the model's paraphrase.
    exploreClaims.mockResolvedValue({
      conceptId: "con_3",
      semantic: true,
      confidenceFloor: FLOOR,
      claims: [
        claim(1, { status: "niega", quote: "no se sigue de las Escrituras" }),
        claim(2, { status: "atribuido" }),
      ],
    });
    await centred();
    fireEvent.click(conceptNode(3));

    expect(await screen.findByText(t("claim.status.niega"))).toBeTruthy();
    expect(screen.getByText(t("claim.status.atribuido"))).toBeTruthy();
    expect(screen.getByText("no se sigue de las Escrituras")).toBeTruthy();
  });

  it("does not let a claim with no state read as one the document asserts", async () => {
    // `sin_estado` is a claim extracted before the field existed, not a fourth
    // judgement. Rendering it as `afirma` would put words in the document's
    // mouth, which is the exact confusion the field was added to remove.
    await centred();
    fireEvent.click(conceptNode(3));

    expect(await screen.findByText("la afirmación 1")).toBeTruthy();
    expect(screen.queryByText(t("claim.status.afirma"))).toBeNull();
    expect(screen.getAllByText(t("claim.status.sin_estado"))).toHaveLength(2);
  });

  it("shows no description for a concept no run has condensed", async () => {
    // Condensing is a paid stage and it is off by default, so a bare name is the
    // common case and the panel has to read correctly without one.
    await centred();
    fireEvent.click(conceptNode(3));
    await waitFor(() => expect(exploreClaims).toHaveBeenCalled());

    expect(document.querySelector(".concept-description")).toBeNull();
  });

  it("shows a concept's description once a run has condensed one", async () => {
    exploreConcepts.mockResolvedValue({
      versionId: "ver_1",
      semantic: true,
      confidenceFloor: FLOOR,
      concepts: [concept(0, { description: "El gobierno de Dios sobre lo creado." })],
    });
    await centred();
    fireEvent.click(conceptNode(0));

    expect(
      await screen.findByText("El gobierno de Dios sobre lo creado."),
    ).toBeTruthy();
  });

  it("marks the open concept unambiguously", async () => {
    await centred();
    expect(conceptNode(3).getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(conceptNode(3));
    await waitFor(() => expect(conceptNode(3).getAttribute("aria-pressed")).toBe("true"));
    expect(conceptNode(3).getAttribute("class")).toContain("is-open");
    expect(conceptNode(4).getAttribute("aria-pressed")).toBe("false");
  });

  it("shows loading rather than 'nothing clears the floor' while fetching", async () => {
    await centred();
    // `claims.length === 0` used to mean both, so the empty answer flashed
    // before the real one at every click.
    let land = (_: unknown) => {};
    exploreClaims.mockReturnValue(new Promise((resolve) => { land = resolve; }));

    fireEvent.click(conceptNode(0));
    await waitFor(() => expect(screen.getByText(t("graph.loading"))).toBeTruthy());
    expect(screen.queryByText(t("graph.noClaims"))).toBeNull();

    land({ conceptId: "con_0", semantic: true, confidenceFloor: FLOOR, claims: [] });
    expect(await screen.findByText(t("graph.noClaims"))).toBeTruthy();
  });

  it("prompts for a concept until one is open", async () => {
    await centred();
    expect(screen.getByText(t("graph.selectConcept"))).toBeTruthy();
    fireEvent.click(conceptNode(0));
    await waitFor(() => expect(screen.queryByText(t("graph.selectConcept"))).toBeNull());
  });

  it("closes again on a second click, because it reports itself as pressed", async () => {
    await centred();
    fireEvent.click(conceptNode(0));
    await screen.findByText("la afirmación 1");
    fireEvent.click(conceptNode(0));
    await waitFor(() => expect(screen.getByText(t("graph.selectConcept"))).toBeTruthy());
  });
});

describe("comparing the centre against a related document", () => {
  /** The concept nodes the canvas currently marks as shared. */
  const sharedNodes = () =>
    Array.from(canvas().querySelectorAll(".node.concept.is-shared"));
  const fadedNodes = () =>
    Array.from(canvas().querySelectorAll(".node.concept.is-faded"));

  it("names which concepts are shared, not only how many", async () => {
    // The whole point. `31 shared` told the reader a number and left them with
    // no way to learn *which* 31 without opening the other document and
    // comparing two lists by eye.
    await centred();
    fireEvent.click(docNode(3));

    // Neighbour 3 shares concepts 0..3.
    expect(await screen.findByText(t("graph.compare.shared"))).toBeTruthy();
    const pane = document.querySelector(".compare-list.is-shared");
    expect(pane?.textContent).toContain("concepto 0");
    expect(pane?.textContent).toContain("concepto 3");
    expect(pane?.textContent).not.toContain("concepto 4");
  });

  it("lights the shared concepts and fades the rest", async () => {
    await centred();
    expect(sharedNodes()).toHaveLength(0);

    fireEvent.click(docNode(3));
    await waitFor(() => expect(sharedNodes()).toHaveLength(4));
    // Twelve drawn, four shared, eight faded — and the faded ones are still
    // drawn, because dimming them to nothing would remove half the comparison.
    expect(fadedNodes()).toHaveLength(8);
  });

  it("says how many it shares and agrees with what it listed", async () => {
    // A count and a list that disagreed would be worse than the count alone.
    await centred();
    fireEvent.click(docNode(5));
    await screen.findByText(t("graph.compare.count", { count: 6 }));
    const items = document.querySelectorAll(".compare-list.is-shared li");
    expect(items).toHaveLength(6);
  });

  it("separates what only the centre has", async () => {
    await centred();
    fireEvent.click(docNode(0));
    await screen.findByText(t("graph.compare.shared"));
    const lists = document.querySelectorAll(".compare-list");
    // Neighbour 0 shares one concept; the centre draws twelve.
    expect(lists[0]?.querySelectorAll("li")).toHaveLength(1);
    expect(lists[1]?.querySelectorAll("li")).toHaveLength(11);
  });

  it("asks the other document for its own concepts, and survives a refusal", async () => {
    // Which concepts the *other* document has and the centre does not is the
    // one group no single response knows. It is a secondary read: losing it
    // must not cost the comparison that is already on screen.
    await centred();
    // *After* centring: the first read draws the graph, and rejecting that one
    // would test a blank screen instead of a degraded comparison.
    exploreConcepts.mockRejectedValueOnce(new Error("nope"));
    fireEvent.click(docNode(2));

    expect(await screen.findByText(t("graph.compare.otherUnavailable"))).toBeTruthy();
    expect(sharedNodes()).toHaveLength(3);
  });

  it("closes the comparison and returns the graph to normal", async () => {
    await centred();
    fireEvent.click(docNode(3));
    await waitFor(() => expect(sharedNodes()).toHaveLength(4));

    fireEvent.click(selectedDocNode(3));
    await waitFor(() => expect(sharedNodes()).toHaveLength(0));
    expect(fadedNodes()).toHaveLength(0);
    expect(screen.getByText(t("graph.selectConcept"))).toBeTruthy();
  });

  it("says when the shared list is truncated rather than showing a short one", async () => {
    // `sharedConcepts` is the true count and the list is capped at 200. A
    // shorter list means truncated, and quietly showing it would make the
    // number and the names disagree with no way to tell.
    exploreRelated.mockResolvedValue({
      versionId: "ver_1",
      semantic: true,
      confidenceFloor: FLOOR,
      documents: [
        relatedDoc(0, { sharedConcepts: 240, sharedConceptNames: ["Biblia"], sharedConceptIds: ["con_0"] }),
      ],
    });
    await centred();
    fireEvent.click(
      screen.getByLabelText(t("graph.node.doc", { title: "vecino 0", count: 240 })),
    );
    expect(
      await screen.findByText(t("graph.compare.truncated", { shown: 1, total: 240 })),
    ).toBeTruthy();
  });
});

describe("re-centring on a related document", () => {
  /** Re-centring moved from the card to an explicit button: the click on the
   *  card now answers the question people arrive with — *which* concepts —
   *  and one gesture means one thing. */
  const centreHere = async (i: number) => {
    fireEvent.click(docNode(i));
    fireEvent.click(await screen.findByText(t("graph.compare.centreHere")));
  };

  it("reads the clicked version, not the document behind it", async () => {
    await centred();
    exploreConcepts.mockClear();
    await centreHere(2);
    // `RelatedDocument.id` is a *version* id; `documentId` is the separate
    // navigable one and would read the wrong thing.
    await waitFor(() => expect(exploreConcepts).toHaveBeenCalledWith("ver_r2"));
  });

  it("keeps the picker naming what is actually drawn", async () => {
    await centred();
    await centreHere(2);
    await waitFor(() => expect(centreCard().textContent).toContain("vecino 2"));

    // The re-centred version is not any shelf document's activeVersionId, so
    // the picker used to find no matching option and silently revert to "pick a
    // document" while the canvas showed one.
    expect(picker().value).toBe("ver_r2");
    expect(picker().selectedOptions[0]?.textContent).toBe("vecino 2");
  });

  it("clears the comparison, whose other half is no longer on screen", async () => {
    await centred();
    await centreHere(1);
    await waitFor(() => expect(centreCard().textContent).toContain("vecino 1"));
    expect(screen.getByText(t("graph.selectConcept"))).toBeTruthy();
  });

  it("clears the open concept, whose claims belong to the old centre", async () => {
    await centred();
    fireEvent.click(conceptNode(0));
    await screen.findByText("la afirmación 1");
    await centreHere(1);
    await waitFor(() => expect(screen.getByText(t("graph.selectConcept"))).toBeTruthy());
  });
});

describe("keyboard and pointer reach the same thing", () => {
  it("opens a concept with Enter and with Space", async () => {
    await centred();
    fireEvent.keyDown(conceptNode(5), { key: "Enter" });
    await waitFor(() => expect(exploreClaims).toHaveBeenCalledWith("con_5"));

    exploreClaims.mockClear();
    fireEvent.keyDown(conceptNode(6), { key: " " });
    await waitFor(() => expect(exploreClaims).toHaveBeenCalledWith("con_6"));
  });

  it("stops Space scrolling the workspace out from under the node", async () => {
    await centred();
    const event = new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true });
    conceptNode(0).dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  it("selects a document for comparison from the keyboard too", async () => {
    await centred();
    fireEvent.keyDown(docNode(3), { key: "Enter" });
    await waitFor(() =>
      expect(canvas().querySelectorAll(".node.concept.is-shared")).toHaveLength(4),
    );
  });

  it("ignores other keys", async () => {
    await centred();
    fireEvent.keyDown(conceptNode(0), { key: "a" });
    expect(exploreClaims).not.toHaveBeenCalled();
  });
});

describe("the inspector line", () => {
  it("gives the full name to focus as well as to hover", async () => {
    await centred();
    const long = t("graph.probe.concept", {
      name: "concepto 0",
      mentions: 1,
      confidence: 80,
    });

    expect(screen.getByText(t("graph.hint"))).toBeTruthy();

    fireEvent.focus(conceptNode(0));
    // A name available only on hover is not available to anyone who cannot
    // hover, which is why this is not a tooltip.
    //
    // Matched as a prefix, not as the whole line: the inspector now appends
    // which other documents share the concept, and pinning the exact string
    // would make this test about the sentence rather than about the name being
    // reachable without a pointer.
    await waitFor(() =>
      expect(document.querySelector(".graph-inspector")?.textContent).toContain(long),
    );

    fireEvent.blur(conceptNode(0));
    await waitFor(() => expect(screen.getByText(t("graph.hint"))).toBeTruthy());
  });

  it("names which other documents share the concept under the pointer", async () => {
    // The other direction of the same question. The sets arrived with the outer
    // ring, so this is a map lookup rather than an intersection recomputed on
    // every mouse move — and it is the same data the highlighting uses, so the
    // line and the picture cannot disagree.
    await centred();
    // Concept 11 is shared by neighbour 11 only… and the ring holds eight, so
    // concept 7 is the last one anybody shares: neighbour 7 alone.
    fireEvent.mouseEnter(conceptNode(7));
    expect(conceptNode(7).querySelector(".concept-label")?.textContent).toBe("concepto 7");
    expect(conceptNode(7).getAttribute("class")).toContain("is-hovered");
    await waitFor(() =>
      expect(document.querySelector(".graph-inspector")?.textContent).toContain(
        t("graph.probe.alsoIn", { count: 1, titles: "vecino 7" }),
      ),
    );

    fireEvent.mouseLeave(conceptNode(7));
    expect(conceptNode(7).getAttribute("class")).not.toContain("is-hovered");

    // Concept 0 is shared by every neighbour on the ring.
    fireEvent.mouseEnter(conceptNode(0));
    await waitFor(() =>
      expect(document.querySelector(".graph-inspector")?.textContent).toContain(
        t("graph.probe.alsoIn", {
          count: 8,
          titles: Array.from({ length: 8 }, (_, i) => `vecino ${i}`).join(", "),
        }),
      ),
    );
  });

  it("says nothing extra about a concept no related document shares", async () => {
    // Absence of evidence is not an empty list to render: a concept nobody else
    // mentions simply reads as itself.
    await centred();
    fireEvent.mouseEnter(conceptNode(9));
    await waitFor(() =>
      expect(document.querySelector(".graph-inspector")?.textContent).not.toContain(
        "vecino",
      ),
    );
  });

  it("reports a document's shared count on hover", async () => {
    await centred();
    fireEvent.mouseEnter(docNode(4));
    await waitFor(() =>
      expect(
        screen.getByText(t("graph.probe.doc", { title: "vecino 4", count: 5 })),
      ).toBeTruthy(),
    );
  });

  it("dims the rest of the canvas while probing, and only then", async () => {
    await centred();
    expect(canvas().getAttribute("class")).not.toContain("is-probing");
    fireEvent.mouseEnter(conceptNode(0));
    await waitFor(() => expect(canvas().getAttribute("class")).toContain("is-probing"));
    expect(conceptNode(0).getAttribute("class")).toContain("is-active");
    expect(conceptNode(1).getAttribute("class")).not.toContain("is-active");
  });
});

describe("when the projection cannot be read", () => {
  it("names the failure instead of drawing an empty picture", async () => {
    exploreConcepts.mockRejectedValue(new Error("memgraph is down"));
    await mounted();
    await pick();
    expect(await screen.findByText(/memgraph is down/)).toBeTruthy();
  });
});

describe("a version with no semantics", () => {
  it("draws the centre and nothing around it, without crashing", async () => {
    exploreConcepts.mockResolvedValue({
      versionId: "ver_1",
      semantic: true,
      confidenceFloor: FLOOR,
      concepts: [],
    });
    exploreRelated.mockResolvedValue({
      versionId: "ver_1",
      semantic: true,
      confidenceFloor: FLOOR,
      documents: [],
    });
    await mounted();
    await pick();

    await waitFor(() => expect(canvas()).toBeTruthy());
    expect(canvas().querySelectorAll('[role="button"]')).toHaveLength(0);
    expect(
      screen.getByText(t("graph.centreMeta", { concepts: 0, related: 0 })),
    ).toBeTruthy();
    // The legend and the evidence note are still the reader's only guide to
    // what an empty canvas means, so they must survive it.
    expect(screen.getByText(t("graph.proposed"))).toBeTruthy();
  });
});

describe("the comparison is reachable, not only rendered", () => {
  it("brings the pane into view when a comparison opens", async () => {
    // Below 76rem the pane stacks under the canvas, so on a laptop window a
    // click lit the graph and left the names off-screen — which reads as
    // "nothing happened" to anyone who does not scroll.
    const seen: unknown[] = [];
    const pane = () => document.querySelector(".graph-detail") as HTMLElement;
    await centred();
    // jsdom has no scrolling at all: the method is absent, which is exactly why
    // the component calls it optionally.
    (pane() as unknown as Record<string, unknown>).scrollIntoView = (o: unknown) =>
      void seen.push(o);

    fireEvent.click(docNode(2));
    await waitFor(() => expect(seen).toHaveLength(1));
  });

  it("survives a browser that has no scrollIntoView", async () => {
    // The guard is the point: without it this throws and takes the comparison
    // down with it.
    await centred();
    expect(() => fireEvent.click(docNode(2))).not.toThrow();
    await screen.findByText(t("graph.compare.shared"));
  });
});
