import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import i18n from "./i18n";
import type { ProjectSummary, RunState, RunSummary } from "./lib/api";
import { ActivityIndicator } from "./ActivityIndicator";

/**
 * The shell's "something is happening" line.
 *
 * The properties worth pinning are the ones the feature exists for: it is
 * *absent* when nothing runs, it reports the stage and progress rather than the
 * catalog's stale state, and stopping a run — which destroys work already paid
 * for — cannot happen on one click.
 */
const { projectSummary, runStatus, cancelRun } = vi.hoisted(() => ({
  projectSummary: vi.fn(),
  runStatus: vi.fn(),
  cancelRun: vi.fn(),
}));

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, projectSummary, runStatus, cancelRun },
  };
});

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});

function run(over: Partial<RunSummary> = {}): RunSummary {
  return {
    id: "run_1",
    workflowId: "ingest-1788144614256-839dce38",
    kind: "index",
    // Deliberately the stale value a real run carried for its whole paid
    // pipeline. The indicator must not repeat it.
    state: "awaiting_approval",
    stage: "semantics",
    startedAt: "2026-08-31T02:50:14Z",
    finishedAt: null,
    errorKind: null,
    title: "01 RetoDeDios",
    libraryId: "lib_teologia",
    usdSoFar: 0.025,
    ...over,
  };
}

function summary(runs: RunSummary[]): ProjectSummary {
  return {
    catalog: {
      available: true, detail: null, libraries: 2, documents: 74,
      absentDocuments: 0, activeVersions: 70, indexedVersions: 70,
      indexedBytes: 1,
    },
    pages: { available: false, recorded: 0, of: 70, pages: null },
    recentRuns: runs,
    graph: { available: true, detail: null, nodes: {}, deterministicEdges: {}, semanticEdges: {} },
  } as unknown as ProjectSummary;
}

function live(over: Partial<RunState> = {}): RunState {
  return {
    workflowId: "ingest-1788144614256-839dce38",
    stage: "semantics",
    state: "running",
    progress: { activity: "extract_semantics", done: 260, total: 598 },
    // Null, not absent: "nobody measured this run" is the ordinary case and the
    // one every historical run is in.
    scores: null,
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  cancelRun.mockResolvedValue(undefined);
});

afterEach(cleanup);

it("renders nothing at all when no run is in flight", async () => {
  projectSummary.mockResolvedValue(summary([run({ finishedAt: "2026-08-31T03:00:00Z" })]));
  const { container } = render(<ActivityIndicator />);
  // An indicator that is always on screen is furniture, and furniture stops
  // being read. Absence is the design, not an empty state.
  await waitFor(() => expect(projectSummary).toHaveBeenCalled());
  expect(container.querySelector(".activity")).toBeNull();
});

it("reports what Temporal says, not the stale state the catalog holds", async () => {
  projectSummary.mockResolvedValue(summary([run()]));
  runStatus.mockResolvedValue(live());
  render(<ActivityIndicator />);

  // The catalog said `awaiting_approval` for this run through its whole paid
  // pipeline. Showing that would be the exact failure this was built for.
  await screen.findByText(t("activity.stage.semantics"));
  expect(screen.queryByText(t("home.run.state.awaiting_approval"))).toBeNull();

  expect(
    screen.getByText(t("activity.progress", { done: 260, total: 598 })),
  ).toBeTruthy();
  const bar = document.querySelector("progress");
  expect(bar?.getAttribute("value")).toBe("260");
  expect(bar?.getAttribute("max")).toBe("598");
});

it("shows the spend with four decimals, because two would round a lie", async () => {
  projectSummary.mockResolvedValue(summary([run()]));
  runStatus.mockResolvedValue(live());
  render(<ActivityIndicator />);
  // $0.0250 renders as "$0.03" at two decimals — a 20% overstatement on a
  // figure somebody reads to decide whether to stop a run.
  await screen.findByText(t("activity.spent", { usd: "$0.0250" }));
});

it("cannot stop a run on one click", async () => {
  projectSummary.mockResolvedValue(summary([run()]));
  runStatus.mockResolvedValue(live());
  render(<ActivityIndicator />);

  fireEvent.click(await screen.findByText(t("activity.cancel")));
  expect(cancelRun).not.toHaveBeenCalled();

  fireEvent.click(screen.getByText(t("activity.confirmCancel")));
  await waitFor(() =>
    expect(cancelRun).toHaveBeenCalledWith("ingest-1788144614256-839dce38"),
  );
});

it("says so when a stop did not take, because a silent one is the worst case", async () => {
  projectSummary.mockResolvedValue(summary([run()]));
  runStatus.mockResolvedValue(live());
  cancelRun.mockRejectedValue(new Error("nope"));
  render(<ActivityIndicator />);

  fireEvent.click(await screen.findByText(t("activity.cancel")));
  fireEvent.click(screen.getByText(t("activity.confirmCancel")));
  await screen.findByText(t("activity.cancelFailed"));
});

it("keeps showing a run when Temporal cannot be asked", async () => {
  projectSummary.mockResolvedValue(summary([run()]));
  runStatus.mockRejectedValue(new Error("control unreachable"));
  render(<ActivityIndicator />);
  // The catalog still says it has not finished. Dropping it would tell the user
  // the work had stopped, which is the one thing nobody can afford to believe
  // wrongly about a run that is spending.
  await screen.findByText("01 RetoDeDios");
});
