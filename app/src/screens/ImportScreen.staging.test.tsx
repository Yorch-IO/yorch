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

const { pickSource, stageSource, ingestStart, ingestGate } = vi.hoisted(() => ({
  pickSource: vi.fn(),
  stageSource: vi.fn(),
  ingestStart: vi.fn(),
  ingestGate: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, pickSource, stageSource, ingestStart, ingestGate },
  };
});

vi.mock("../lib/libraries", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/libraries")>();
  return {
    ...actual,
    useLibraries: () => ({
      selected: "lib_1",
      // `rows`, which is what the real hook returns — the queue names each
      // run's library from it. The double said `libraries` and the real state
      // has never had that field, so it agreed with the test and with nothing
      // else.
      rows: [{ id: "lib_1", name: "Teología" }],
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
  pickSource.mockResolvedValue("/home/a/libros/x/libro.pdf");
  ingestGate.mockResolvedValue(null);
  ingestStart.mockResolvedValue({ workflowId: "ingest-1", state: "running" });
});

const typePathAndStart = async (container: HTMLElement) => {
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
    await typePathAndStart(container);

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
    await typePathAndStart(container);

    await waitFor(() => expect(stageSource).toHaveBeenCalled());
    expect(ingestStart).not.toHaveBeenCalled();
    expect(screen.queryByText(/no se pudo leer/)).not.toBeNull();
  });
});
