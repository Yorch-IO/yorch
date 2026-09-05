/**
 * What a queue row says about a run, and what it refuses to say twice.
 *
 * Both properties here came out of a screenshot rather than an assertion, which
 * is why they now have assertions: `vitest` renders the text and never reads it,
 * so a row saying the same thing twice — or saying a finished run is still
 * working — passes every suite.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import i18n from "./i18n";
import { ImportQueue } from "./ImportQueue";
import { api, DEFAULT_STAGES, type RunListItem } from "./lib/api";
import type { QueueItem } from "./lib/importQueue";

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

const draw = (items: QueueItem[], onChanged: () => void = () => {}) =>
  render(
    <ImportQueue
      items={items}
      loaded
      error={null}
      stages={DEFAULT_STAGES}
      onDecide={() => {}}
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
