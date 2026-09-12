/**
 * What a version's statistics pane says about how well its index can be
 * searched — and, more importantly, what it refuses to say.
 *
 * The property that matters is not that the numbers render. It is that they
 * render **only when a run measured them**, and that they never render as
 * zeros: most versions on this installation predate the eval stage, so "recall
 * 0.00" would be shown for all of them and would send somebody to fix an index
 * that is fine. "Nobody asked" and "it scored nothing" are different statements
 * and only one of them is a fact about the corpus.
 *
 * The figures moved out of the version row and into this pane when the route
 * that carries them was added, so that all three clients read them from one
 * place — the paid plane's `document_detail` has never sent `scores`, which is
 * why cloud mode and the whole web client showed none.
 *
 * Testing Library's automatic `cleanup` is not registered in this project, so
 * this file calls it by hand — see `AskScreen.test.tsx`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { LibraryScreen } from "./LibraryScreen";

const { libraryDocuments, documentDetail, versionStatistics } = vi.hoisted(() => ({
  libraryDocuments: vi.fn(),
  documentDetail: vi.fn(),
  versionStatistics: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, libraryDocuments, documentDetail, versionStatistics },
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

/** A leg that answered, with whatever figures the caller cares about. */
const leg = (over: Record<string, unknown> = {}) => ({
  available: true,
  detail: "",
  ...over,
});

/** A leg that could not answer — and therefore carries no figures at all. */
const down = (detail: string) => ({ available: false, detail });

function stats(over: Record<string, unknown> = {}) {
  return {
    libraryId: "lib_1",
    documentId: "doc_1",
    versionId: "ver_1",
    catalog: leg({ byteSize: 1024, pageCount: null, state: "indexed", runs: 2 }),
    structure: leg({
      graph: leg({ chunks: 274, sections: 12, citations: 274, claims: 0, kinds: {}, sectionLevels: {} }),
      qdrant: leg({ points: 274 }),
      artifacts: down("no chunks.jsonl"),
      countsAgree: null,
    }),
    semantics: down("bolt://127.0.0.1:7789: refused"),
    retrieval: down("ningún run midió esta versión"),
    ledger: leg({ byStage: {}, totalUsd: 0, chargedInMoreThanOneRun: [], usdByRunState: {} }),
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
  documentDetail.mockResolvedValue({
    id: "doc_1", libraryId: "lib_1", title: "Catecismo", author: null,
    format: "pdf", sourceKey: "x/catecismo.pdf", sourcePath: "/w/x.pdf",
    present: true, tags: [], activeVersionId: "ver_1",
    canReindex: true, canRebuild: true,
    versions: [version()],
  });
  versionStatistics.mockResolvedValue(stats());
});

async function openTheStatistics() {
  const view = render(<LibraryScreen />);
  await screen.findByText("Catecismo");
  fireEvent.click(screen.getByText("Ver detalles"));
  await screen.findByText("estadísticas");
  fireEvent.click(screen.getByText("estadísticas"));
  return view;
}

describe("measured retrieval quality on a version", () => {
  it("shows the figures when a run measured them", async () => {
    versionStatistics.mockResolvedValue(
      stats({
        retrieval: leg({
          sourceRun: "ingest-1",
          scores: SCORES,
          floor: { minScore: 0.6, noiseFloor: 0.6232, headroom: -0.0232, honest: false },
        }),
      }),
    );

    await openTheStatistics();

    await waitFor(() => expect(screen.getByText("recall@5")).toBeTruthy());
    expect(screen.getByText("90%")).toBeTruthy();
    expect(screen.getByText("0.764")).toBeTruthy();
  });

  it("shows the noise floor and the dense-only leg beside the recall", async () => {
    // Not extras. The questions are written from the chunks they must find, so
    // the hybrid figure alone flatters the index; and recall says how often the
    // right chunk came back while the floor says what a wrong one scores.
    versionStatistics.mockResolvedValue(
      stats({ retrieval: leg({ scores: SCORES, floor: null }) }),
    );

    await openTheStatistics();

    await waitFor(() => expect(screen.getByText("piso de ruido")).toBeTruthy());
    expect(screen.getByText("0.623")).toBeTruthy();
    expect(screen.getByText("solo denso")).toBeTruthy();
    expect(screen.getByText("100%")).toBeTruthy();
  });

  it("says nothing at all about a version nobody measured", async () => {
    const { container } = await openTheStatistics();
    await waitFor(() => expect(screen.getByText("Calidad de búsqueda")).toBeTruthy());

    expect(container.querySelector(".scores dt")?.textContent).not.toBe("recall@5");
    // The specific thing that must not happen: a zero rendered as a measurement.
    expect(screen.queryByText("0%")).toBeNull();
    expect(screen.queryByText("recall@5")).toBeNull();
    // And it says *why* rather than rendering an empty panel.
    expect(screen.getByText(/ningún run midió/)).toBeTruthy();
  });

  it("warns when the similarity floor sits below what a wrong chunk scores", async () => {
    // 0.50 scored best of everything the recorded sweep tried and is wrong,
    // because that index's noise floor is 0.5153: it wins the metric by
    // admitting exactly what the floor was measured to exclude, and every eval
    // question has a right answer to find so nothing in the numbers shows it.
    versionStatistics.mockResolvedValue(
      stats({
        retrieval: leg({
          scores: SCORES,
          floor: { minScore: 0.5, noiseFloor: 0.5153, headroom: -0.0153, honest: false },
        }),
      }),
    );

    await openTheStatistics();

    const warning = await screen.findByText(/admite justo lo que se midió para excluir/);
    expect(warning.className).toContain("warn");
  });
});

describe("a leg that could not answer", () => {
  it("names the store it could not reach instead of reporting no concepts", async () => {
    await openTheStatistics();

    await waitFor(() =>
      expect(screen.getByText("Lo que encontró el extractor")).toBeTruthy(),
    );
    expect(screen.getByText(/bolt:\/\/127\.0\.0\.1:7789/)).toBeTruthy();
    // The failure this rule exists for: a stopped Memgraph rendering as a
    // version whose extractor found nothing.
    expect(screen.queryByText("conceptos")).toBeNull();
  });

  it("does not claim the stores agree when one of them did not answer", async () => {
    // `null` is "could not compare" and only `false` is the claim that they
    // disagree. Neither line may be printed for the first.
    await openTheStatistics();

    await waitFor(() => expect(screen.getByText("Estructura")).toBeTruthy());
    expect(screen.queryByText(/coinciden en el número de fragmentos/)).toBeNull();
    expect(screen.queryByText(/no coinciden en cuántos fragmentos/)).toBeNull();
  });

  it("reports a page count nothing wrote as unrecorded rather than zero", async () => {
    // `register_version` runs before extraction, so the column is never filled.
    // The figure starts working by itself the day something fills it.
    await openTheStatistics();

    await waitFor(() => expect(screen.getByText("páginas")).toBeTruthy());
    expect(screen.getByText("sin registrar")).toBeTruthy();
  });
});

describe("what this version cost", () => {
  it("names a stage charged in more than one run", async () => {
    // The finding the leg exists for: on one real version the eval set was
    // generated twice, the first time inside a run that was cancelled —
    // $1.1965 of that document's $3.7572, and no screen could show it.
    versionStatistics.mockResolvedValue(
      stats({
        ledger: leg({
          byStage: {
            evalset: { usd: 1.1463, runs: ["run-a", "run-b"], unpricedEntries: 0 },
          },
          totalUsd: 1.1463,
          chargedInMoreThanOneRun: ["evalset"],
          usdByRunState: { cancelled: 0.5753, succeeded: 0.571 },
        }),
      }),
    );

    await openTheStatistics();

    // The warning names the stage with the same label the table above uses.
    // Joining the raw ids printed "evalset" beside a row reading "conjunto de
    // evaluación", so the sentence named a stage the reader could not find.
    const warning = await screen.findByText(/Cobrado en más de un run/);
    expect(warning.textContent).toContain("conjunto de evaluación");
    expect(screen.getAllByText(/conjunto de evaluación/)).toHaveLength(2);
  });

  it("never renders an unpriced stage as free", async () => {
    // A missing price means the model id is absent from the table, which
    // under-reports the bill rather than describing a call that cost nothing.
    versionStatistics.mockResolvedValue(
      stats({
        ledger: leg({
          byStage: { semantics: { usd: 0, runs: ["run-a"], unpricedEntries: 3 } },
          totalUsd: 0,
          chargedInMoreThanOneRun: [],
          usdByRunState: {},
        }),
      }),
    );

    await openTheStatistics();

    await waitFor(() => expect(screen.getByText("sin precio")).toBeTruthy());
  });
});

describe("the stylesheet the pane borrows", () => {
  it("does not inherit the ledger's column-hiding rule", async () => {
    // `.audit-table th:nth-child(2)` is hidden below 76rem, because the run
    // ledger can afford to lose its timestamp there. The statistics pane reuses
    // `.audit-table` for its look, and column 2 of its cost table is the
    // **cost** — so the unscoped rule deleted the one figure the table exists
    // for, at every narrow window, with nothing in this suite able to see it.
    // A nth-child rule is a statement about one table's column order, so it has
    // to name that table.
    const { readFileSync } = await import("node:fs");
    const css = readFileSync("src/styles.css", "utf8");
    expect(css).not.toMatch(/^\s*\.audit-table (th|td):nth-child\(2\)/m);
    expect(css).toMatch(/^\s*\.audit (th|td):nth-child\(2\)/m);
  });
});
