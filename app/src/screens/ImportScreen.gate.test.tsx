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

const { pickSource, stageSource, ingestStart, ingestGate, runStatus } = vi.hoisted(() => ({
  pickSource: vi.fn(),
  stageSource: vi.fn(),
  ingestStart: vi.fn(),
  ingestGate: vi.fn(),
  runStatus: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, pickSource, stageSource, ingestStart, ingestGate, runStatus },
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
  pickSource.mockResolvedValue("/home/a/libros/x/libro.pdf");
  ingestGate.mockResolvedValue(null);
});

const start = async (container: HTMLElement) => {
  // No path field any more: a path typed by hand cannot work in either mode, so
  // the only paths that reach `stage_source` are ones the OS produced. The
  // screen is therefore driven the way a person drives it — press the chooser,
  // which is the first button, then Preview, which is the last.
  const buttons = () => Array.from(container.querySelectorAll("button"));
  fireEvent.click(buttons()[0] as HTMLButtonElement);
  await waitFor(() => expect(pickSource).toHaveBeenCalled());

  const preview = buttons()[buttons().length - 1] as HTMLButtonElement;
  // Disabled until a file *and* a library are set, which is why the pick above
  // has to land first.
  expect(preview.disabled).toBe(false);
  fireEvent.click(preview);
};

describe("waiting for a gate that may never arrive", () => {
  it("stops and says so when the run ended without reaching its gate", async () => {
    runStatus.mockResolvedValue({ workflowId: "ingest-1", stage: "learning", state: "failed" });
    const { container } = render(<ImportScreen />);
    await start(container);

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
    await start(container);

    await waitFor(() => expect(runStatus.mock.calls.length).toBeGreaterThan(0), settle);
    expect(container.querySelector(".error")).toBeNull();
  });

  it("keeps waiting when the plane cannot say, rather than calling it a failure", async () => {
    // `null` means an older control plane, or a run whose history has aged out
    // of Temporal. Neither is a failure, and reading it as one would abandon a
    // run that is still working.
    runStatus.mockResolvedValue({ workflowId: "ingest-1", stage: null, state: null });
    const { container } = render(<ImportScreen />);
    await start(container);

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
    await start(container);

    await waitFor(() => expect(ingestGate.mock.calls.length).toBeGreaterThan(0), settle);
    await oneMoreTick();
    expect(runStatus).not.toHaveBeenCalled();
  });
});
