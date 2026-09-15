import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { Tab } from "../App";
import type { ProjectSummary, RunSummary, StackStatus } from "../lib/api";
import { HomeScreen } from "./HomeScreen";

/**
 * Inicio's behaviour, and above all the one property it exists for: **a figure
 * that could not be read renders as a dash, never as a zero.** Those are two
 * different facts with two different fixes, and the difference is invisible to
 * anyone reading `0 conceptos`.
 *
 * Only `api` is replaced; `errorMessage` and `errorGuidanceKey` are pure and
 * worth exercising for real.
 */
const { stackStatus, projectSummary } = vi.hoisted(() => ({
  stackStatus: vi.fn(),
  projectSummary: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, stackStatus, projectSummary } };
});

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});

function status(services: { name: string; state: string }[]): StackStatus {
  return {
    project: "company-brain",
    composeDir: "/infra",
    workspace: "/workspace",
    ports: {
      qdrantHttp: 6433,
      qdrantGrpc: 6434,
      postgres: 5532,
      temporal: 7333,
      temporalUi: 8380,
      api: 8787,
    },
    devMode: false,
    services: services.map((s) => ({ ...s, health: "", publishers: [] })),
  };
}

const UP = status([
  { name: "api", state: "running" },
  { name: "worker", state: "running" },
]);

function run(over: Partial<RunSummary> = {}): RunSummary {
  return {
    id: "run_1",
    workflowId: "ingest-1787395834305-e60e8f92",
    kind: "index",
    state: "succeeded",
    stage: "semantics",
    startedAt: "2026-08-22T10:53:57.240966Z",
    finishedAt: "2026-08-22T11:00:53.734138Z",
    errorKind: null,
    title: "01 Liderazgo",
    libraryId: "lib_teologia",
    usdSoFar: null,
    ...over,
  };
}

function summary(over: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    catalog: {
      available: true,
      detail: null,
      libraries: 1,
      documents: 72,
      absentDocuments: 0,
      activeVersions: 70,
      indexedVersions: 69,
      indexedBytes: 16548853,
    },
    graph: {
      available: true,
      detail: null,
      nodes: { Chunk: 4722, Concept: 11348, Claim: 24151 },
      deterministicEdges: { HAS_CHUNK: 8570 },
      semanticEdges: { MENTIONS: 24424, ABOUT: 24174, INVOLVES: 521 },
    },
    // The shipping state: nothing writes `page_count`, so a factory defaulting
    // to a recorded page count would test only the case that does not happen.
    pages: { available: false, recorded: 0, of: 69, pages: null },
    recentRuns: [run()],
    ...over,
  };
}

/** The value rendered beside a label, read off the card rather than the page:
 *  "—" appears many times once more than one leg is down. */
function metric(label: string): string {
  const node = Array.from(document.querySelectorAll(".metric")).find(
    (el) => el.querySelector(".metric-label")?.textContent === label,
  );
  if (!node) throw new Error(`no metric card labelled ${label}`);
  return node.querySelector(".metric-value")?.textContent ?? "";
}

async function mounted(go?: (tab: Tab) => void) {
  const view = render(<HomeScreen go={go} />);
  await waitFor(() => expect(stackStatus).toHaveBeenCalled());
  return view;
}

beforeEach(() => {
  // `restoreMocks: true` in vite.config.ts restores *spies*; these are plain
  // `vi.fn()`s and keep their call log across tests in the same file. Without
  // this, "called twice" reads whatever the previous thirteen tests left behind
  // — and "never called" passes for free in any test that runs early.
  vi.clearAllMocks();
  stackStatus.mockResolvedValue(UP);
  projectSummary.mockResolvedValue(summary());
});

afterEach(() => cleanup());

describe("before the stack is running", () => {
  it("reports a stopped stack as a state rather than as an error", async () => {
    stackStatus.mockResolvedValue(status([{ name: "api", state: "exited" }]));
    await mounted();
    await screen.findByText(t("home.offline"));
    // Not the red panel every other screen shows: the containers being down is
    // the ordinary state of a freshly opened app, and this is the first screen
    // a user sees.
    expect(document.querySelector(".error")).toBeNull();
    expect(document.querySelector(".notice")).toBeTruthy();
  });

  it("does not call the control API before something is listening", async () => {
    stackStatus.mockResolvedValue(status([{ name: "api", state: "exited" }]));
    await mounted();
    await screen.findByText(t("home.offline"));
    // Reaching a control API that is not there costs the full request timeout
    // before it says anything, which would be the landing screen's first act.
    expect(projectSummary).not.toHaveBeenCalled();
  });

  it("offers a way to the screen that starts it", async () => {
    const go = vi.fn();
    stackStatus.mockResolvedValue(status([]));
    await mounted(go);
    fireEvent.click(await screen.findByRole("button", { name: t("home.goToServices") }));
    expect(go).toHaveBeenCalledWith("stack");
  });
});

describe("the figures", () => {
  it("shows what both legs reported", async () => {
    await mounted();
    await screen.findByText(t("home.metric.concepts"));
    expect(metric(t("home.metric.documents"))).toContain("72");
    expect(metric(t("home.metric.concepts"))).toContain("11");
    expect(metric(t("home.metric.claims"))).toContain("24");
  });

  it("sums the semantic edges and leaves the deterministic ones out", async () => {
    // 24424 + 24174 + 521. Folding HAS_CHUNK in would report a model's
    // proposals as something the corpus stated.
    await mounted();
    await screen.findByText(t("home.metric.relations"));
    expect(metric(t("home.metric.relations")).replace(/\D/g, "")).toBe("49119");
  });

  it("renders an unreadable figure as a dash and never as a zero", async () => {
    projectSummary.mockResolvedValue(
      summary({
        graph: {
          available: false,
          detail: "GraphError: Failed to DNS resolve address memgraph:7687",
          nodes: null,
          deterministicEdges: null,
          semanticEdges: null,
        },
      }),
    );
    await mounted();
    await screen.findByText(t("home.metric.concepts"));
    expect(metric(t("home.metric.concepts"))).toBe("—");
    expect(metric(t("home.metric.claims"))).toBe("—");
    expect(metric(t("home.metric.relations"))).toBe("—");
    // and the half that *was* readable is still shown
    expect(metric(t("home.metric.documents"))).toContain("72");
  });

  it("says why the graph figures are missing", async () => {
    projectSummary.mockResolvedValue(
      summary({
        graph: {
          available: false,
          detail: "GraphError: no route to host",
          nodes: null,
          deterministicEdges: null,
          semanticEdges: null,
        },
      }),
    );
    await mounted();
    await screen.findByText(t("home.graphUnavailable"));
  });

  it("reports pages as not recorded, with the count that makes it self-correcting", async () => {
    await mounted();
    await screen.findByText(t("home.metric.pages"));
    expect(metric(t("home.metric.pages"))).toBe("—");
    await screen.findByText(t("home.pagesUnrecorded", { recorded: 0, of: 69 }));
  });

  it("shows a real page count once something records one", async () => {
    projectSummary.mockResolvedValue(
      summary({ pages: { available: true, recorded: 69, of: 69, pages: 6120 } }),
    );
    await mounted();
    await screen.findByText(t("home.metric.pages"));
    expect(metric(t("home.metric.pages")).replace(/\D/g, "")).toBe("6120");
  });
});

describe("recent activity", () => {
  it("lists a run by the document it was spent on", async () => {
    await mounted();
    await screen.findByText("01 Liderazgo");
  });

  it("names a run whose document is gone by its workflow id", async () => {
    // `run.document_id` is ON DELETE SET NULL: run history outlives removal on
    // purpose, and a row with no title must still say which run it was.
    projectSummary.mockResolvedValue(
      summary({ recentRuns: [run({ title: null })] }),
    );
    await mounted();
    await screen.findByText("ingest-1787395834305-e60e8f92");
  });

  it("distinguishes an unreadable catalog from a project that has run nothing", async () => {
    projectSummary.mockResolvedValue(summary({ recentRuns: null }));
    await mounted();
    await screen.findByText(t("home.activityUnavailable"));

    cleanup();
    projectSummary.mockResolvedValue(summary({ recentRuns: [] }));
    await mounted();
    await screen.findByText(t("home.activityEmpty"));
  });
});

describe("when something fails outright", () => {
  it("shows the technical message and the guidance for its kind", async () => {
    projectSummary.mockRejectedValue({
      kind: "control_unreachable",
      message: "error sending request for url",
    });
    await mounted();
    await screen.findByText(t("error.title"));
    await screen.findByText(t("error.controlUnreachable"));
  });

  it("retries on demand", async () => {
    await mounted();
    await screen.findByText(t("home.metric.concepts"));
    fireEvent.click(screen.getByRole("button", { name: t("home.refresh") }));
    await waitFor(() => expect(projectSummary).toHaveBeenCalledTimes(2));
  });
});

describe("an empty project", () => {
  it("points at the import screen rather than showing a wall of zeros", async () => {
    projectSummary.mockResolvedValue(
      summary({
        catalog: {
          available: true,
          detail: null,
          libraries: 0,
          documents: 0,
          absentDocuments: 0,
          activeVersions: 0,
          indexedVersions: 0,
          indexedBytes: 0,
        },
        recentRuns: [],
      }),
    );
    await mounted();
    await screen.findByText(t("home.emptyProject"));
  });
});
