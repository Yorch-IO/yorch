/**
 * What the screen does while it waits for a gate, and what it does when the
 * gate is never coming.
 *
 * `ingestGate` answers `null` for the first few seconds of every run — the
 * free stages have not finished — and the screen polls until it stops. A run
 * that *died* before publishing its report answers `null` in exactly the same
 * way, for as long as anybody asks, because a `gate_report` query against a
 * failed workflow hands back the last value it recorded. Observed 2026-08-28:
 * an ingest whose activity retries were exhausted left the screen spinning
 * while Temporal already knew it had failed.
 *
 * So the property under test is not "it polls" but "it asks the second
 * question". Testing Library's automatic `cleanup` is not registered in this
 * project, so this file calls it by hand — see `AskScreen.test.tsx`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { ImportScreen } from "./ImportScreen";

/** The screen polls every 1500 ms, so every assertion here has to outlive a
 *  tick — `waitFor`'s own default is 1000 and would fail before the first one. */
const TICK = 1500;
const settle = { timeout: 4 * TICK, interval: 50 };
const oneMoreTick = () => new Promise((r) => setTimeout(r, TICK + 200));

const { stageSource, ingestStart, ingestGate, runStatus } = vi.hoisted(() => ({
  stageSource: vi.fn(),
  ingestStart: vi.fn(),
  ingestGate: vi.fn(),
  runStatus: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, stageSource, ingestStart, ingestGate, runStatus },
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

afterEach(cleanup);

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage("es");
  stageSource.mockResolvedValue({
    sourcePath: "/home/a/libros/x/libro.pdf",
    sourceKey: "x/libro.pdf",
    byteSize: 10,
  });
  ingestStart.mockResolvedValue({ workflowId: "ingest-1", state: "running" });
  ingestGate.mockResolvedValue(null);
});

const start = (container: HTMLElement) => {
  const input = container.querySelector("input:not([type=checkbox])");
  fireEvent.change(input as HTMLInputElement, {
    target: { value: "/home/a/libros/x/libro.pdf" },
  });
  fireEvent.click(container.querySelector("button") as HTMLButtonElement);
};

describe("waiting for a gate that may never arrive", () => {
  it("stops and says so when the run ended without reaching its gate", async () => {
    runStatus.mockResolvedValue({ workflowId: "ingest-1", stage: "learning", state: "failed" });
    const { container } = render(<ImportScreen />);
    start(container);

    // The message names the state rather than saying "something went wrong":
    // a run that failed and one that was cancelled have different causes.
    await waitFor(() => expect(screen.getByText(/falló/)).toBeTruthy(), settle);
    const asked = ingestGate.mock.calls.length;

    // And it really stopped, rather than rendering the message on every tick.
    await oneMoreTick();
    expect(ingestGate.mock.calls.length).toBe(asked);
  });

  it("keeps waiting while the run is still running", async () => {
    runStatus.mockResolvedValue({ workflowId: "ingest-1", stage: "extracting", state: "running" });
    const { container } = render(<ImportScreen />);
    start(container);

    await waitFor(() => expect(runStatus.mock.calls.length).toBeGreaterThan(0), settle);
    expect(container.querySelector(".error")).toBeNull();
  });

  it("keeps waiting when the plane cannot say, rather than calling it a failure", async () => {
    // `null` means an older control plane, or a run whose history has aged out
    // of Temporal. Neither is a failure, and reading it as one would abandon a
    // run that is still working.
    runStatus.mockResolvedValue({ workflowId: "ingest-1", stage: null, state: null });
    const { container } = render(<ImportScreen />);
    start(container);

    await waitFor(() => expect(runStatus.mock.calls.length).toBeGreaterThan(0), settle);
    expect(container.querySelector(".error")).toBeNull();
  });

  it("does not ask about the run once the gate has answered", async () => {
    // The extra call is only worth making while there is nothing to show. A
    // gate in hand ends the poll, and asking anyway would bill a request per
    // tick for the rest of the session.
    ingestGate.mockResolvedValue({
      runId: "ingest-1",
      documentId: `doc_${"a".repeat(24)}`,
      versionId: `ver_${"a".repeat(24)}`,
      preview: {
        chunkCount: 2,
        kinds: [{ kind: "cuerpo", count: 2 }],
        characters: 1514,
        chunksAreFinal: true,
        warnings: [],
      },
      estimate: {
        stages: [],
        totalUsd: null,
        totalUsdHigh: null,
        priceSource: "",
        unpricedStages: [],
      },
      profileWarnings: [],
      profile: null,
    });
    const { container } = render(<ImportScreen />);
    start(container);

    await waitFor(() => expect(ingestGate.mock.calls.length).toBeGreaterThan(0), settle);
    await oneMoreTick();
    expect(runStatus).not.toHaveBeenCalled();
  });
});
