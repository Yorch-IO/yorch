/**
 * What the queue asks, and what it refuses to conclude from an answer.
 *
 * These properties moved here when the Import screen stopped holding one run in
 * `useState` and started reading the catalog. The screen's own poll is gone; the
 * questions it was asking are not, and they are the ones worth keeping:
 *
 * `ingestGate` answers `null` for the first seconds of every run — the free
 * stages have not finished — and it answers `null` in exactly the same way, for
 * as long as anybody asks, for a run that *died* before publishing a report.
 * Observed 2026-08-28: an ingest whose activity retries were exhausted left the
 * screen spinning while Temporal already knew it had failed. So the property is
 * not "it polls" but "it asks the second question, and it does not read silence
 * as failure".
 *
 * Tested as a hook rather than through a rendered screen, for the reason
 * `askSession.test.ts` gives about the Ask reducer: the decisions are about data,
 * and asserting them on the data beats rendering a screen to find out.
 */
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { RunListItem } from "./api";
import { unfinished, useImportQueue, waiting } from "./importQueue";

const { runsList, runStatus, ingestGate } = vi.hoisted(() => ({
  runsList: vi.fn(),
  runStatus: vi.fn(),
  ingestGate: vi.fn(),
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, api: { ...actual.api, runsList, runStatus, ingestGate } };
});

function row(over: Partial<RunListItem> = {}): RunListItem {
  return {
    id: "ingest-1",
    workflowId: "ingest-1",
    kind: "index",
    state: "running",
    stage: "extracting",
    startedAt: "2026-09-01T14:00:00Z",
    finishedAt: null,
    errorKind: null,
    errorDetail: null,
    title: "Institución",
    libraryId: "lib_1",
    label: null,
    documentId: "doc_1",
    versionId: "ver_1",
    usdSoFar: null,
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  ingestGate.mockResolvedValue(null);
});

afterEach(() => vi.useRealTimers());

it("asks Temporal only about runs the catalog says are unfinished", async () => {
  // Asking for finished runs would pay the round trip for a fact the catalog
  // already holds, once per run per poll, for the whole session.
  runsList.mockResolvedValue({
    runs: [row(), row({ id: "ingest-0", workflowId: "ingest-0", state: "succeeded",
                        finishedAt: "2026-09-01T14:10:00Z" })],
    nextBefore: null,
  });
  runStatus.mockResolvedValue({
    workflowId: "ingest-1", stage: "semantics", state: "running",
    progress: null, scores: null,
  });

  const { result } = renderHook(() => useImportQueue("lib_1"));
  await waitFor(() => expect(result.current.items.length).toBe(2));
  expect(runStatus).toHaveBeenCalledTimes(1);
  expect(runStatus).toHaveBeenCalledWith("ingest-1");
});

it("keeps the catalog's answer when the plane cannot say, rather than blanking it", async () => {
  // `null` means an older control plane, a run whose history has aged out, or a
  // workflow inside a long activity that is not answering queries. None of them
  // is a failure, and overwriting a real state with nothing would make a live
  // run render as one nobody can describe.
  runsList.mockResolvedValue({ runs: [row({ state: "awaiting_approval" })], nextBefore: null });
  runStatus.mockResolvedValue({
    workflowId: "ingest-1", stage: null, state: null, progress: null, scores: null,
  });

  const { result } = renderHook(() => useImportQueue("lib_1"));
  await waitFor(() => expect(result.current.items.length).toBe(1));
  expect(result.current.items[0]?.state).toBe("awaiting_approval");
  expect(result.current.items[0]?.stage).toBe("extracting");
});

it("keeps a run the plane could not describe at all", async () => {
  // A queue that dropped it would hide work in flight at exactly the moment
  // something is wrong with the stack.
  runsList.mockResolvedValue({ runs: [row()], nextBefore: null });
  runStatus.mockRejectedValue(new Error("control unreachable"));

  const { result } = renderHook(() => useImportQueue("lib_1"));
  await waitFor(() => expect(result.current.items.length).toBe(1));
  expect(result.current.items[0]?.state).toBe("running");
  expect(result.current.error).toBeNull();
});

it("fetches a gate only for a run that is waiting on a person", async () => {
  runsList.mockResolvedValue({
    runs: [row({ state: "running" }),
           row({ id: "ingest-2", workflowId: "ingest-2", state: "awaiting_approval" })],
    nextBefore: null,
  });
  runStatus.mockResolvedValue({
    workflowId: "x", stage: null, state: "running", progress: null, scores: null,
  });

  const { result } = renderHook(() => useImportQueue("lib_1"));
  await waitFor(() => expect(ingestGate).toHaveBeenCalled());
  expect(ingestGate).toHaveBeenCalledTimes(1);
  expect(ingestGate).toHaveBeenCalledWith("ingest-2");
  expect(result.current.items.length).toBe(2);
});

it("does not re-ask for a report it already has", async () => {
  // A run parked at its gate waits up to seven days. Re-fetching an unchanging
  // report every five seconds for a week is the cost this cache exists to avoid.
  const report = {
    runId: "ingest-2", documentId: "doc_1", versionId: "ver_1",
    preview: { chunkCount: 2, kinds: [], characters: 10, chunksAreFinal: true, warnings: [] },
    estimate: { stages: [], totalUsd: null, totalUsdHigh: null, priceSource: "", unpricedStages: [] },
    profileWarnings: [], profile: null,
  };
  runsList.mockResolvedValue({
    runs: [row({ id: "ingest-2", workflowId: "ingest-2", state: "awaiting_approval" })],
    nextBefore: null,
  });
  runStatus.mockResolvedValue({
    workflowId: "ingest-2", stage: null, state: "running", progress: null, scores: null,
  });
  ingestGate.mockResolvedValue(report);

  const { result } = renderHook(() => useImportQueue("lib_1"));
  await waitFor(() => expect(result.current.items[0]?.gate).not.toBeNull());

  await result.current.refresh();
  expect(ingestGate).toHaveBeenCalledTimes(1);
});

it("asks nothing at all until a library is selected", async () => {
  const { result } = renderHook(() => useImportQueue(null));
  await waitFor(() => expect(result.current.loaded).toBe(true));
  expect(runsList).not.toHaveBeenCalled();
  expect(result.current.items).toEqual([]);
});

it("reports a queue it could not read, without throwing away the screen", async () => {
  runsList.mockRejectedValue({ kind: "control_unreachable", message: "down" });
  const { result } = renderHook(() => useImportQueue("lib_1"));
  await waitFor(() => expect(result.current.error).not.toBeNull());
  expect(result.current.loaded).toBe(true);
});

it("decides unfinished by the timestamp, never by the state column", () => {
  // The state column is written by the workflow, and a run that died without
  // recording an outcome keeps whatever it had. `finished_at` is only ever set
  // by `finish_run`, so its absence is the more honest question.
  expect(unfinished(row({ state: "succeeded", finishedAt: null }))).toBe(true);
  expect(unfinished(row({ state: "running", finishedAt: "2026-09-01T14:10:00Z" }))).toBe(false);
});

it("reads waiting off the catalog, so it survives a Temporal it cannot reach", () => {
  expect(waiting(row({ state: "awaiting_approval" }))).toBe(true);
  expect(waiting(row({ state: "running" }))).toBe(false);
});
