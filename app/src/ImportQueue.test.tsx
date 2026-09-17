/**
 * What a queue row says about a run, and what it refuses to say twice.
 *
 * Both properties here came out of a screenshot rather than an assertion, which
 * is why they now have assertions: `vitest` renders the text and never reads it,
 * so a row saying the same thing twice — or saying a finished run is still
 * working — passes every suite.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import i18n from "./i18n";
import { ImportQueue } from "./ImportQueue";
import { api, DEFAULT_STAGES, type GateReport, type RunListItem } from "./lib/api";
import type { QueueItem } from "./lib/importQueue";
import * as transcriber from "./lib/localTranscriber";

/** What the queue looks like with nothing waiting for this machine, which is
 *  what every row below but one is rendered against. */
const IDLE_QUEUE: ReturnType<typeof transcriber.useLocalTranscriber> = {
  status: null, model: "", setModel: () => {}, readiness: "unknown", jobs: [],
  current: null, attempt: null, history: [], paused: false, setPaused: () => {},
  probe: async () => false, probing: false,
  download: async () => false, downloading: null, giveUp: async () => false,
  hoursLeft: null, refresh: async () => {}, error: null,
};

const LIBRARIES = [
  { id: "lib_1", name: "Teología" },
  { id: "lib_yt_UCabc", name: "Casa Sobre La Roca" },
];

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      runAudit: vi.fn(),
      runEvents: vi.fn(),
      versionActivate: vi.fn(),
    },
  };
});

const t = (key: string): string => i18n.t(key);

const run = (over: Partial<RunListItem> = {}): RunListItem => ({
  id: "r1",
  workflowId: "r1",
  kind: "index",
  state: "running",
  stage: "semantics",
  startedAt: "2026-09-01T14:00:00Z",
  finishedAt: null,
  errorKind: null,
  errorDetail: null,
  title: "Un libro",
  libraryId: "lib_1",
  label: null,
  documentId: "d",
  versionId: "v",
  usdSoFar: 0.1255,
  ...over,
});

const item = (
  over: Omit<Partial<QueueItem>, "run"> & { run?: Partial<RunListItem> } = {},
): QueueItem => ({
  state: over.state ?? "running",
  stage: over.stage ?? "semantics",
  progress: over.progress ?? null,
  gate: over.gate ?? null,
  videoGate: over.videoGate ?? null,
  run: run(over.run),
});

const draw = (
  items: QueueItem[],
  onChanged: () => void = () => {},
  onDecide: (
    workflowId: string,
    approved: boolean,
    options: typeof DEFAULT_STAGES,
  ) => void | Promise<void> = () => {},
  queue: { filter?: string | null; onFilter?: (id: string | null) => void } = {},
) =>
  render(
    <ImportQueue
      items={items}
      loaded
      error={null}
      filter={queue.filter ?? null}
      onFilter={queue.onFilter ?? (() => {})}
      libraries={LIBRARIES}
      stages={DEFAULT_STAGES}
      onDecide={onDecide}
      onChanged={onChanged}
      locale="es"
    />,
  );

/** A run that ended the way a structural collision ends it: every stage ran,
 *  the bill is real, and only the promotion was withheld. */
const withheld = (over: Partial<RunListItem> = {}): QueueItem =>
  item({
    state: "blocked",
    stage: "blocked",
    run: {
      state: "blocked",
      stage: "blocked",
      finishedAt: "2026-09-02T15:26:57Z",
      errorKind: "structural_mismatch",
      errorDetail: "las reglas heredadas detectan 0 capítulo(s)",
      versionId: "ver_f1d1",
      libraryId: "lib_teologia",
      usdSoFar: 4.1219,
      ...over,
    },
  });

beforeEach(async () => {
  await i18n.changeLanguage("es");
  vi.clearAllMocks();
});

// --- the queue spans every library ------------------------------------------

it("names the library on every row when the list spans more than one", async () => {
  // The defect this replaces: a probe started from the Channel tab lands in
  // `lib_yt_<channelId>`, the queue was filtered to the picker's library, and
  // the run was invisible on the one screen whose job is to show what is in
  // flight — while the sidebar, which is project-wide, showed it going.
  const { container } = draw([
    item({ run: { id: "a", workflowId: "a", libraryId: "lib_1", title: "Un libro" } }),
    item({
      run: { id: "b", workflowId: "b", libraryId: "lib_yt_UCabc", title: "Una prédica" },
    }),
  ]);
  await waitFor(() => expect(container.textContent).toContain("Casa Sobre La Roca"));
  expect(container.textContent).toContain("Teología");
});

it("leaves the library off the rows once the list is narrowed to one", async () => {
  // Repeating the library somebody just filtered to on every row is noise.
  const { container } = draw(
    [item({ run: { libraryId: "lib_1" } })],
    () => {},
    () => {},
    { filter: "lib_1" },
  );
  expect(container.querySelector(".queue-library")).toBeNull();
});

it("narrows by library, and says which emptiness it is showing", async () => {
  const onFilter = vi.fn();
  const { container } = draw([], () => {}, () => {}, { onFilter });
  // Unfiltered and empty is "nothing has been imported"; filtered and empty is
  // "nothing *here*", which has a way out the other does not.
  expect(container.textContent).toContain(t("queue.empty"));

  const select = container.querySelector(".queue-filter select") as HTMLSelectElement;
  await waitFor(() => expect(select.options.length).toBe(3));
  fireEvent.change(select, { target: { value: "lib_yt_UCabc" } });
  expect(onFilter).toHaveBeenCalledWith("lib_yt_UCabc");
  fireEvent.change(select, { target: { value: "" } });
  // Empty string is the "all libraries" option, and it must reach the hook as
  // `null` — an empty `library_id` on the wire would narrow to nothing.
  expect(onFilter).toHaveBeenLastCalledWith(null);
});

it("says nothing is here rather than nothing exists when narrowed", async () => {
  const { container } = draw([], () => {}, () => {}, { filter: "lib_1" });
  expect(container.textContent).toContain(t("queue.emptyHere"));
});

afterEach(cleanup);

it("does not print the state and the stage when they are the same word", () => {
  // A run parked at its gate read "esperando aprobación · esperando aprobación",
  // because that is both its state and its stage.
  const { container } = draw([
    item({
      state: "awaiting_approval",
      stage: "awaiting_approval",
      run: { state: "awaiting_approval" },
    }),
  ]);
  const label = t("home.run.state.awaiting_approval");
  expect(container.textContent).toContain(label);
  expect(container.textContent?.split(label).length - 1).toBe(1);
});

it("does not claim a finished run is still doing something", () => {
  // `activity.stage.*` is a present-continuous progress label, so a run that
  // ended half an hour ago read "completada · terminando" and a failed one read
  // "fallida · extrayendo conceptos". Where it died is a real question, and the
  // ledger answers it properly; the row is not the place.
  const { container } = draw([
    item({
      state: "succeeded",
      stage: "done",
      run: { state: "succeeded", finishedAt: "2026-09-01T14:10:00Z", stage: "done" },
    }),
  ]);
  expect(container.textContent).toContain(t("home.run.state.succeeded"));
  expect(container.textContent).not.toContain(t("activity.stage.done"));
});

it("still names the stage of a run that is going", () => {
  const { container } = draw([item({ state: "running", stage: "semantics" })]);
  expect(container.textContent).toContain(t("activity.stage.semantics"));
});

it("offers no way to stop a run that has already finished", () => {
  draw([
    item({
      state: "succeeded",
      run: { state: "succeeded", finishedAt: "2026-09-01T14:10:00Z" },
    }),
  ]);
  expect(screen.queryByText(t("activity.cancel"))).toBeNull();
  expect(screen.getByText(t("queue.showDetail"))).toBeTruthy();
});

it("says a run has been billed nothing yet, rather than nothing at all", () => {
  // Null is not zero: a run still in its free stages legitimately has no
  // figure, and `$0.0000` would claim it spent and got nothing.
  const { container } = draw([item({ run: { usdSoFar: null } })]);
  expect(container.textContent).not.toContain("$0.0000");
});

it("offers a way to publish an index whose activation was withheld", async () => {
  // The run that prompted this button spent $4.1219 and left 444 points in
  // Qdrant that nothing on screen could reach.
  const activate = vi.mocked(api.versionActivate);
  activate.mockResolvedValue({ versionId: "ver_f1d1", documents: ["doc_1"] });
  const changed = vi.fn();
  draw([withheld()], changed);

  screen.getByText(t("queue.activate")).click();

  await waitFor(() => expect(activate).toHaveBeenCalledWith("lib_teologia", "ver_f1d1"));
  // And it says what happened, rather than leaving the same button offering the
  // press again: the run stays `blocked` afterwards, because it *was* withheld.
  await screen.findByText(t("queue.activateDone").replace("{{count}}", "1"));
  expect(screen.queryByText(t("queue.activate"))).toBeNull();
  expect(changed).toHaveBeenCalled();
});

it("never offers to publish an index whose run did not finish building it", () => {
  // Four versions in the real catalog are `pending` because their run was
  // cancelled, and their indexes are partial. Only `structural_mismatch` means
  // "complete, and deliberately not promoted", which is why the button keys on
  // the reason and not on the state.
  draw([
    withheld({ state: "cancelled", errorKind: null }),
    withheld({ errorKind: "activity_failed", state: "failed" }),
    withheld({ finishedAt: null }),
    withheld({ versionId: null }),
  ]);
  expect(screen.queryByText(t("queue.activate"))).toBeNull();
});

it("says why publishing is safe, beside the button that does it", () => {
  const { container } = draw([withheld()]);
  expect(container.textContent).toContain(t("queue.activateWhy"));
});

it("names a run that failed before it had a title", () => {
  // The defect this whole change exists for. A video refused by YouTube fails
  // in `probe_video`, which is *before* `register_document` — so there is no
  // document and therefore no title, and the row used to have nothing to show
  // but a workflow id. `label` is what the run knew about itself: the URL
  // somebody pasted.
  const { container } = draw([
    item({
      state: "failed",
      run: {
        kind: "video",
        state: "failed",
        stage: "probing",
        finishedAt: "2026-09-05T16:03:16Z",
        title: null,
        label: "https://youtu.be/yq6uVBsVkeQ",
        documentId: null,
        versionId: null,
        errorKind: "youtube_refused_this_host",
      },
    }),
  ]);
  expect(container.textContent).toContain("https://youtu.be/yq6uVBsVkeQ");
  expect(container.textContent).not.toContain("r1");
});

it("explains an error kind rather than printing it", () => {
  // `youtube_refused_this_host` and `video_unavailable` call for opposite
  // things — one is this machine, the other is the video — and a reader who is
  // shown the identifier has to already know which. The raw kind is what the
  // row printed before.
  const { container } = draw([
    item({
      state: "failed",
      run: {
        state: "failed",
        finishedAt: "2026-09-05T16:03:16Z",
        errorKind: "youtube_refused_this_host",
      },
    }),
  ]);
  expect(container.textContent).toContain(
    t("queue.errorKind.youtube_refused_this_host"),
  );
  expect(container.textContent).not.toContain("youtube_refused_this_host");
});

it("still shows a kind nobody has written words for", () => {
  // `defaultValue` keeps every other kind rendering as itself. Without it a new
  // kind renders as a bare translation key, which is worse than the identifier.
  const { container } = draw([
    item({
      state: "failed",
      run: {
        state: "failed",
        finishedAt: "2026-09-05T16:03:16Z",
        errorKind: "some_kind_with_no_wording",
      },
    }),
  ]);
  expect(container.textContent).toContain("some_kind_with_no_wording");
  expect(container.textContent).not.toContain("queue.errorKind.");
});

/** The report the gate panel renders. Only the fields it reads. */
const gateReport = () =>
  ({
    runId: "ingest-1",
    documentId: "doc_1",
    versionId: "ver_1",
    preview: {
      chunkCount: 299,
      kinds: [{ kind: "cuerpo", count: 243 }],
      characters: 214589,
      chunksAreFinal: false,
      warnings: [],
    },
    estimate: {
      stages: [],
      totalUsd: null,
      totalUsdHigh: null,
      priceSource: "table",
      unpricedStages: [],
    },
    profileWarnings: [],
    profile: null,
  }) as unknown as GateReport;

it("gives the buttons back when an approval is refused", async () => {
  // **The failure this was written against.** `setDeciding(true)` had no
  // counterpart anywhere in the file, so the first press disabled both buttons
  // for the life of the component — and on the happy path nobody noticed,
  // because the row moves on and takes the component with it. On a refusal the
  // panel stayed on screen, inert, over a run that was still waiting for an
  // answer. Seen in the real window when the paid plane answered 422: the only
  // remedy was relaunching the app, which is not a remedy anybody guesses.
  const refused = vi.fn().mockRejectedValue(new Error("422"));
  const { container } = draw(
    [item({ run: { state: "awaiting_approval" }, gate: gateReport() })],
    () => {},
    // The parent swallows its own failure — `ImportScreen.decide` catches and
    // renders the error panel — so what reaches the row is a settled promise
    // either way. This asserts the row does not depend on which way.
    (id, approved, options) => refused(id, approved, options).catch(() => {}),
  );

  const approve = () =>
    [...container.querySelectorAll("button")].find((b) =>
      b.textContent?.includes(t("gate.approve")),
    )!;

  expect(approve().disabled).toBe(false);
  fireEvent.click(approve());
  await waitFor(() => expect(refused).toHaveBeenCalled());
  await waitFor(() => expect(approve().disabled).toBe(false));
});

/**
 * A bucket run parked for this machine's GPU.
 *
 * Two things are asserted and both were reachable only from this screen. The
 * row has to say *why* an approved run is not moving — "waiting for this
 * machine" is a state nothing else in the product has — and it has to carry
 * the way out, because the alternative is a settings panel two tabs away that
 * nobody thinks to open while looking at a stalled run.
 */
it("says a run is waiting for this machine, and offers Amazon beside it", async () => {
  const switched = vi.fn().mockResolvedValue(true);
  vi.spyOn(transcriber, "useLocalTranscriber").mockReturnValue({
    ...IDLE_QUEUE,
    giveUp: switched,
  });
  const changed = vi.fn();
  draw([item({ state: "awaiting_transcript", stage: "transcribing", run: { kind: "audio", state: "awaiting_transcript", workflowId: "audio-1" } })], changed);

  expect(screen.getByText(t("home.run.state.awaiting_transcript"))).toBeTruthy();
  expect(document.body.textContent).toContain(t("queue.transcribeHere"));

  const amazon = Array.from(document.querySelectorAll("button")).find(
    (b) => b.textContent === t("queue.transcribeOnAmazon"),
  )!;
  fireEvent.click(amazon);
  await waitFor(() => expect(switched).toHaveBeenCalledWith("audio-1"));
  // The queue is asked to re-read: the run is back at a gate with a price on
  // it, and the row that offered the switch is no longer the right row.
  await waitFor(() => expect(changed).toHaveBeenCalled());
  await waitFor(() =>
    expect(document.body.textContent).toContain(t("queue.transcribeSwitched")),
  );
});
