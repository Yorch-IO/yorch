/**
 * The Recast screen, at the two moments money is decided.
 *
 * jsdom lays out nothing, so what is asserted here is *what is sent* — the
 * request a transformation is started with, and the approval each gate
 * receives — plus the one rendering decision that matters: which of the two
 * gate panels a run parked at the second one shows.
 *
 * `cleanup` by hand, as everywhere else here.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { BackendProvider } from "../lib/backend";
import { LibrariesProvider } from "../lib/libraries";
import { TransformScreen } from "./TransformScreen";

const {
  genres,
  libraryDocuments,
  libraries,
  runsList,
  startTransform,
  transformGate,
  transformPlan,
  approveTransform,
  artifactSave,
} = vi.hoisted(() => ({
  genres: vi.fn(),
  libraryDocuments: vi.fn(),
  libraries: vi.fn(),
  runsList: vi.fn(),
  startTransform: vi.fn(),
  transformGate: vi.fn(),
  transformPlan: vi.fn(),
  approveTransform: vi.fn(),
  artifactSave: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      genres,
      libraryDocuments,
      libraries,
      runsList,
      startTransform,
      transformGate,
      transformPlan,
      approveTransform,
      artifactSave,
    },
  };
});

afterEach(cleanup);

const LIBRARY = "lib_teologia";

const VOCABULARY = {
  genres: ["treatise", "essay", "novel"],
  modes: ["faithful", "adaptive"],
  defaultMode: "faithful",
  purposes: ["context", "verification", "completion", "contradiction"],
};

const DOCUMENTS = [
  {
    id: "doc_indexed",
    title: "Un Libro Indexado",
    author: null,
    format: "pdf",
    sourceKey: "a.pdf",
    present: true,
    tags: [],
    activeVersionId: "ver_1",
    updatedAt: null,
  },
  {
    id: "doc_pending",
    title: "Un Libro Sin Indexar",
    author: null,
    format: "pdf",
    sourceKey: "b.pdf",
    present: true,
    tags: [],
    activeVersionId: null,
    updatedAt: null,
  },
];

const RUN = {
  id: "1",
  workflowId: "transform-abc",
  kind: "transform",
  state: "awaiting_approval",
  stage: "awaiting_approval",
  startedAt: "2026-09-18T00:00:00Z",
  finishedAt: null,
  errorKind: null,
  title: "Un Libro Indexado",
  libraryId: LIBRARY,
  usdSoFar: null,
};

const ESTIMATE = {
  stages: [],
  totalUsd: 4.5,
  totalUsdHigh: 6.75,
  priceSource: "Precios de terceros",
  unpricedStages: [],
};

beforeEach(async () => {
  await i18n.changeLanguage("es");
  vi.clearAllMocks();
  genres.mockResolvedValue(VOCABULARY);
  libraryDocuments.mockResolvedValue({ libraryId: LIBRARY, documents: DOCUMENTS });
  libraries.mockResolvedValue({
    libraries: [{ id: LIBRARY, name: "Teología", documents: 2, indexedVersions: 1 }],
  });
  runsList.mockResolvedValue({ runs: [], nextBefore: null });
  startTransform.mockResolvedValue({ workflowId: "transform-abc", state: "running" });
  transformGate.mockResolvedValue(null);
  transformPlan.mockResolvedValue(null);
  approveTransform.mockResolvedValue(undefined);
  artifactSave.mockResolvedValue("/home/a/obra.md");
});

function mount() {
  return render(
    <BackendProvider>
      <LibrariesProvider>
        <TransformScreen active />
      </LibrariesProvider>
    </BackendProvider>,
  );
}

describe("choosing what to recast", () => {
  it("offers only documents with an indexed version", async () => {
    // The work is made from that version's `chunks.jsonl`, which is the only
    // artifact carrying the outline the chunker detected. A document with
    // nothing indexed has no such file, and offering it would produce a run
    // that dies in its first activity.
    const { container } = mount();
    await waitFor(() => expect(libraryDocuments).toHaveBeenCalled());
    await waitFor(() =>
      expect(container.textContent).toContain("Un Libro Indexado"),
    );
    expect(container.textContent).not.toContain("Un Libro Sin Indexar");
  });

  it("sends the version, the genre, the mode and only the enabled purposes", async () => {
    const { container } = mount();
    await waitFor(() => expect(genres).toHaveBeenCalled());
    await waitFor(() =>
      expect(container.textContent).toContain("Un Libro Indexado"),
    );

    const selects = container.querySelectorAll("select");
    fireEvent.change(selects[0]!, { target: { value: "doc_indexed" } });
    fireEvent.change(selects[1]!, { target: { value: "novel" } });
    const radios = container.querySelectorAll<HTMLInputElement>('input[type="radio"]');
    fireEvent.click(radios[1]!); // adaptive
    const boxes = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    fireEvent.click(boxes[0]!); // the first purpose

    const start = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === i18n.t("transform.start"),
    );
    fireEvent.click(start!);

    await waitFor(() => expect(startTransform).toHaveBeenCalled());
    const [request, options] = startTransform.mock.calls[0]!;
    expect(request).toMatchObject({
      libraryId: LIBRARY,
      documentId: "doc_indexed",
      versionId: "ver_1",
      genre: "novel",
      mode: "adaptive",
      purposes: ["context"],
    });
    // No `tenantId`: the plane stamps it from the session, and a body carrying
    // one is refused outright by `forbidNonWhitelisted`.
    expect(request).not.toHaveProperty("tenantId");
    expect(options.research).toBe(true);
  });

  it("asks the library for transformations only, in the request", async () => {
    // Narrowed in the request, never over what arrived: the limit is 30, so
    // filtering afterwards would show whichever of the last 30 runs of every
    // kind happened to be transformations.
    mount();
    await waitFor(() => expect(runsList).toHaveBeenCalled());
    expect(runsList.mock.calls[0]![0]).toMatchObject({
      libraryId: LIBRARY,
      kinds: "transform",
    });
  });
});

describe("answering a gate", () => {
  beforeEach(() => {
    runsList.mockResolvedValue({ runs: [RUN], nextBefore: null });
  });

  it("shows the first gate's quote while that is where the run is", async () => {
    transformGate.mockResolvedValue({
      genre: "essay",
      mode: "faithful",
      purposes: ["context"],
      sourceTitle: "Un Libro Indexado",
      sourceChapters: 12,
      characters: 420_000,
      projectedChapters: 18,
      researchBudget: 24,
      supported: 96,
      projection: true,
      estimate: ESTIMATE,
    });
    const { container } = mount();
    await waitFor(() =>
      expect(container.textContent).toContain(i18n.t("transform.gate.title")),
    );
    expect(container.textContent).toContain(i18n.t("transform.gate.projection"));
  });

  it("shows the outline instead once there is one, never both", async () => {
    // The whole reason this feature has two report types and two routes: in the
    // ingest path one report is assigned before the first gate and never
    // cleared, so the second gate serves the first one's preview.
    transformGate.mockResolvedValue({
      genre: "essay",
      mode: "faithful",
      purposes: [],
      sourceTitle: "x",
      sourceChapters: 1,
      characters: 10,
      projectedChapters: 1,
      researchBudget: 0,
      supported: 0,
      projection: true,
      estimate: ESTIMATE,
    });
    transformPlan.mockResolvedValue({
      genre: "essay",
      mode: "faithful",
      chapters: [{ ordinal: 1, title: "Primero", intent: "abre", chars: 100 }],
      uncoveredFraction: 0,
      researchBudget: 0,
      fallback: false,
      notes: [],
      spentSoFar: 0.04,
      estimate: ESTIMATE,
    });
    const { container } = mount();
    await waitFor(() =>
      expect(container.textContent).toContain(i18n.t("transform.plan.title")),
    );
    expect(container.textContent).not.toContain(i18n.t("transform.gate.projection"));
  });

  it("re-enables the buttons when an approval is refused", async () => {
    // The recorded latch, one screen over: `setDeciding(false)` appeared
    // nowhere, so a refused approval left both buttons disabled with the panel
    // still on screen and the only remedy a relaunch, which nobody guesses.
    transformGate.mockResolvedValue({
      genre: "essay",
      mode: "faithful",
      purposes: [],
      sourceTitle: "x",
      sourceChapters: 1,
      characters: 10,
      projectedChapters: 1,
      researchBudget: 0,
      supported: 0,
      projection: true,
      estimate: ESTIMATE,
    });
    approveTransform.mockRejectedValue(new Error("422 no"));
    const { container } = mount();
    await waitFor(() =>
      expect(container.textContent).toContain(i18n.t("transform.gate.title")),
    );
    const approve = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === i18n.t("transform.gate.approve"),
    );
    fireEvent.click(approve!);
    await waitFor(() => expect(approveTransform).toHaveBeenCalled());
    await waitFor(() =>
      expect(
        [...container.querySelectorAll("button")].find(
          (b) => b.textContent === i18n.t("transform.gate.approve"),
        )?.disabled,
      ).toBe(false),
    );
  });
});

describe("the finished work", () => {
  it("saves it as Markdown through the artifact allowlist", async () => {
    runsList.mockResolvedValue({
      runs: [{ ...RUN, state: "succeeded", stage: "done" }],
      nextBefore: null,
    });
    const { container } = mount();
    await waitFor(() =>
      expect(container.textContent).toContain(i18n.t("transform.download")),
    );
    const download = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === i18n.t("transform.download"),
    );
    fireEvent.click(download!);
    await waitFor(() => expect(artifactSave).toHaveBeenCalled());
    expect(artifactSave).toHaveBeenCalledWith(
      "transform-abc",
      "transform",
      "Un Libro Indexado.md",
    );
  });
});
