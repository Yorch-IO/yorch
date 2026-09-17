import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import i18n from "./i18n";
import type { ProjectSummary, RunState, RunSummary } from "./lib/api";
import { ActivityIndicator } from "./ActivityIndicator";
import { BackendProvider } from "./lib/backend";
import { ChannelsProvider } from "./lib/channels";
import { LibrariesProvider } from "./lib/libraries";

/**
 * The shell's "something is happening" line.
 *
 * The properties worth pinning are the ones the feature exists for: it is
 * *absent* when nothing runs, it reports the stage and progress rather than the
 * catalog's stale state, and stopping a run — which destroys work already paid
 * for — cannot happen on one click.
 */
const { projectSummary, runStatus, cancelRun, libraries, channels } = vi.hoisted(() => ({
  projectSummary: vi.fn(),
  runStatus: vi.fn(),
  cancelRun: vi.fn(),
  libraries: vi.fn(),
  channels: vi.fn(),
}));

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, projectSummary, runStatus, cancelRun, libraries, channels },
  };
});

/** The indicator reads both selections, because "Abrir" has to *set* one
 *  before it navigates — the fix for a button that landed on a queue filtered
 *  to a library the run was not in. The shell mounts these around it. */
function draw(go?: (tab: string) => void) {
  return render(
    <BackendProvider>
      <LibrariesProvider>
        <ChannelsProvider>
          <ActivityIndicator go={go as never} />
        </ChannelsProvider>
      </LibrariesProvider>
    </BackendProvider>,
  );
}

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

const CHANNEL = "UC-Wxkyg4RM5A6lhyXTUJP9w";

beforeEach(() => {
  vi.clearAllMocks();
  cancelRun.mockResolvedValue(undefined);
  libraries.mockResolvedValue({ libraries: [] });
  channels.mockResolvedValue({
    channels: [
      {
        channel: {
          channelId: CHANNEL,
          title: "Casa Sobre La Roca",
          handle: "casaroca",
          description: "",
          uploadsPlaylistId: "UU",
          url: "",
        },
        libraryId: `lib_yt_${CHANNEL}`,
        syncedAt: "2026-09-16T00:00:00+00:00",
        videoCount: 2983,
        unitsSpent: 61,
        complete: true,
      },
    ],
  });
});

afterEach(cleanup);

it("renders nothing at all when no run is in flight", async () => {
  projectSummary.mockResolvedValue(summary([run({ finishedAt: "2026-08-31T03:00:00Z" })]));
  const { container } = draw();
  // An indicator that is always on screen is furniture, and furniture stops
  // being read. Absence is the design, not an empty state.
  await waitFor(() => expect(projectSummary).toHaveBeenCalled());
  expect(container.querySelector(".activity")).toBeNull();
});

it("reports what Temporal says, not the stale state the catalog holds", async () => {
  projectSummary.mockResolvedValue(summary([run()]));
  runStatus.mockResolvedValue(live());
  draw();

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
  draw();
  // $0.0250 renders as "$0.03" at two decimals — a 20% overstatement on a
  // figure somebody reads to decide whether to stop a run.
  await screen.findByText(t("activity.spent", { usd: "$0.0250" }));
});

it("cannot stop a run on one click", async () => {
  projectSummary.mockResolvedValue(summary([run()]));
  runStatus.mockResolvedValue(live());
  draw();

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
  draw();

  fireEvent.click(await screen.findByText(t("activity.cancel")));
  fireEvent.click(screen.getByText(t("activity.confirmCancel")));
  await screen.findByText(t("activity.cancelFailed"));
});

it("keeps showing a run when Temporal cannot be asked", async () => {
  projectSummary.mockResolvedValue(summary([run()]));
  runStatus.mockRejectedValue(new Error("control unreachable"));
  draw();
  // The catalog still says it has not finished. Dropping it would tell the user
  // the work had stopped, which is the one thing nobody can afford to believe
  // wrongly about a run that is spending.
  await screen.findByText("01 RetoDeDios");
});

// --- "Abrir" lands where the run actually is ---------------------------------

it("opens a channel's video on the Channel tab, with that channel selected", async () => {
  // Reported as "a video requested from Channel does not appear in Import".
  // It was parked correctly; the queue was filtered to the picker's library and
  // a channel's runs live in `lib_yt_<channelId>`, which that picker is never
  // on — the Channel tab does not use it.
  projectSummary.mockResolvedValue(
    summary([run({ libraryId: `lib_yt_${CHANNEL}`, kind: "video", title: "Una prédica" })]),
  );
  runStatus.mockResolvedValue(live({ state: "awaiting_approval", progress: null }));
  const go = vi.fn();
  draw(go);

  await waitFor(() => expect(channels).toHaveBeenCalled());
  fireEvent.click(await screen.findByText(t("activity.view")));
  expect(go).toHaveBeenCalledWith("channel");
  // And the channel is remembered, so the screen mounts already looking at it.
  expect(window.localStorage.getItem("companyBrain.channelId")).toBe(CHANNEL);
});

it("opens anything else on the import queue, with its library selected", async () => {
  projectSummary.mockResolvedValue(summary([run({ libraryId: "lib_teologia" })]));
  runStatus.mockResolvedValue(live());
  const go = vi.fn();
  draw(go);

  await waitFor(() => expect(libraries).toHaveBeenCalled());
  fireEvent.click(await screen.findByText(t("activity.view")));
  expect(go).toHaveBeenCalledWith("import");
  // Selecting it is the whole fix: the queue narrows by that choice, so
  // navigating without it lands on a list that cannot hold the run.
  expect(window.localStorage.getItem("companyBrain.libraryId")).toBe("lib_teologia");
});
