/**
 * Staging: the one step that differs between the two control planes, and the
 * reason this screen does not know which one it is talking to.
 *
 * Worth its own file because `ImportScreen.test.tsx` deliberately tests `Cost`
 * as a function and renders nothing else. The gap that leaves is exactly the
 * one that let `/libraries` ship broken on the Python side: a call site with no
 * test passes every suite until somebody runs it.
 *
 * Testing Library's automatic `cleanup` is not registered in this project, so
 * this file calls it by hand — see `AskScreen.test.tsx`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { ImportScreen } from "./ImportScreen";

const { stageSource, ingestStart, ingestGate } = vi.hoisted(() => ({
  stageSource: vi.fn(),
  ingestStart: vi.fn(),
  ingestGate: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, stageSource, ingestStart, ingestGate },
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
  ingestGate.mockResolvedValue(null);
  ingestStart.mockResolvedValue({ workflowId: "ingest-1", state: "running" });
});

const typePathAndStart = (container: HTMLElement) => {
  // The path field carries no `type`, so it is the first input on the screen;
  // everything after it is a stage checkbox.
  const input = container.querySelector("input:not([type=checkbox])");
  fireEvent.change(input as HTMLInputElement, {
    target: { value: "/home/a/libros/x/libro.pdf" },
  });
  // Disabled until a path *and* a library are set, which is why the change
  // above has to land first.
  const start = container.querySelector("button") as HTMLButtonElement;
  expect(start.disabled).toBe(false);
  fireEvent.click(start);
};

describe("staging a source before the ingest starts", () => {
  it("sends the ingest the path staging returned, not the one the user typed", async () => {
    // Cloud mode's answer: a path inside the worker's container, which is the
    // only one `ingest_start` can use there and is nothing like the input.
    stageSource.mockResolvedValue({
      sourcePath: "/workspace/tenants/tnt_x/inbox/9f2.pdf",
      sourceKey: "libro.pdf",
      byteSize: 2048,
    });
    const { container } = render(<ImportScreen />);
    typePathAndStart(container);

    await waitFor(() => expect(ingestStart).toHaveBeenCalled());
    expect(stageSource).toHaveBeenCalledWith("/home/a/libros/x/libro.pdf");
    const request = ingestStart.mock.calls[0]?.[0];
    expect(request.sourcePath).toBe("/workspace/tenants/tnt_x/inbox/9f2.pdf");
    expect(request.sourceKey).toBe("libro.pdf");
  });

  it("does not start an ingest when staging failed", async () => {
    // An upload that failed leaves no file, so starting anyway would spend the
    // gate's free stages on a path the worker cannot open — and report the
    // failure as an ingest error rather than an upload one.
    stageSource.mockRejectedValue({ kind: "io", message: "no se pudo leer" });
    const { container } = render(<ImportScreen />);
    typePathAndStart(container);

    await waitFor(() => expect(stageSource).toHaveBeenCalled());
    expect(ingestStart).not.toHaveBeenCalled();
    expect(screen.queryByText(/no se pudo leer/)).not.toBeNull();
  });
});
