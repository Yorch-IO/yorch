/**
 * Choosing the file, which stopped being a text field.
 *
 * A path typed by hand could never work in either mode: the local worker opens
 * `/workspace` and the cloud worker is on another machine, and nothing between
 * the app and either one translates. So the only paths that may reach
 * `stage_source` are ones the operating system produced — the chooser's, or a
 * native drop's.
 *
 * What is *not* here is the drag and drop itself. It is bound to Tauri's own
 * webview event, which jsdom does not provide, and the subscription is
 * deliberately allowed to fail so the screen degrades to "no drag and drop"
 * rather than to a blank panel. That degradation is the thing this file can
 * assert; the drop path needs a real window.
 *
 * Testing Library's automatic `cleanup` is not registered in this project, so
 * this file calls it by hand — see `AskScreen.test.tsx`.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { ImportScreen, fileName } from "./ImportScreen";

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
  ingestGate.mockResolvedValue(null);
  ingestStart.mockResolvedValue({ workflowId: "ingest-1", state: "running" });
});

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});
const buttons = (c: HTMLElement) => Array.from(c.querySelectorAll("button"));
/** Every button here is found by its text, never by position.
 *
 *  Position stopped working when the screen became a queue: each chosen file
 *  carries its own "remove" link inside the drop zone, and an error panel
 *  renders a Dismiss below the one being looked for — so "the first button" and
 *  "the last button" both drift exactly in the tests that need them most. */
const byText = (c: HTMLElement, ...texts: string[]) =>
  buttons(c).find((b) => texts.includes(b.textContent ?? "")) as HTMLButtonElement;
const chooser = (c: HTMLElement) =>
  byText(c, t("import.choose"), t("import.chooseMore"), t("import.picking"));
const preview = (c: HTMLElement) =>
  buttons(c).find(
    (b) =>
      b.textContent === t("import.working") ||
      b.textContent?.startsWith(
        t("import.startBatch", { count: 0 }).split("0")[0] ?? "",
      ),
  ) as HTMLButtonElement;

describe("choosing a file to import", () => {
  it("offers no way to type a path, because no typed path could work", () => {
    const { container } = render(<ImportScreen />);
    const typeable = Array.from(container.querySelectorAll("input")).filter(
      (i) => i.type !== "checkbox",
    );
    expect(typeable).toEqual([]);
  });

  it("cannot preview until a file has been chosen", async () => {
    pickSource.mockResolvedValue("/home/a/libros/x/libro.pdf");
    const { container } = render(<ImportScreen />);
    expect(preview(container).disabled).toBe(true);

    fireEvent.click(chooser(container));
    await waitFor(() => expect(preview(container).disabled).toBe(false));
  });

  it("shows the file's name, and the path underneath it", async () => {
    pickSource.mockResolvedValue("/home/a/libros/x/El reto de Dios.txt");
    const { container } = render(<ImportScreen />);
    fireEvent.click(chooser(container));

    await waitFor(() =>
      expect(container.textContent).toContain("El reto de Dios.txt"),
    );
    // The full path stays visible: it is the only way to tell two files with
    // the same name apart, and it is what a person quotes in a bug report.
    expect(container.textContent).toContain("/home/a/libros/x/El reto de Dios.txt");
  });

  it("keeps the previous choice when the chooser is dismissed", async () => {
    pickSource.mockResolvedValue("/home/a/libros/x/libro.pdf");
    const { container } = render(<ImportScreen />);
    fireEvent.click(chooser(container));
    await waitFor(() => expect(container.textContent).toContain("libro.pdf"));

    // `null` is a dismissed dialog, not a failure and not a deselection:
    // opening the chooser and changing your mind must leave what you had.
    pickSource.mockResolvedValue(null);
    fireEvent.click(chooser(container));

    await waitFor(() => expect(pickSource).toHaveBeenCalledTimes(2));
    expect(container.textContent).toContain("libro.pdf");
    expect(container.querySelector(".error")).toBeNull();
  });

  it("stages exactly the path the chooser returned", async () => {
    pickSource.mockResolvedValue("/home/a/libros/x/libro.pdf");
    stageSource.mockResolvedValue({
      sourcePath: "/workspace/inbox/libro.pdf",
      sourceKey: "x/libro.pdf",
      byteSize: 12,
    });
    const { container } = render(<ImportScreen />);
    fireEvent.click(chooser(container));
    await waitFor(() => expect(preview(container).disabled).toBe(false));
    fireEvent.click(preview(container));

    await waitFor(() => expect(stageSource).toHaveBeenCalled());
    expect(stageSource).toHaveBeenCalledWith("/home/a/libros/x/libro.pdf");
    // And the ingest gets what staging returned, never what was picked.
    await waitFor(() => expect(ingestStart).toHaveBeenCalled());
    expect(ingestStart.mock.calls[0]?.[0].sourcePath).toBe("/workspace/inbox/libro.pdf");
  });

  it("reports a chooser that failed, rather than starting anyway", async () => {
    pickSource.mockRejectedValue({ kind: "io", message: "no se pudo abrir" });
    const { container } = render(<ImportScreen />);
    fireEvent.click(chooser(container));

    await waitFor(() => expect(container.querySelector(".error")).not.toBeNull());
    expect(preview(container).disabled).toBe(true);
    expect(stageSource).not.toHaveBeenCalled();
  });

  it("keeps every file chosen, rather than the first and a count of the rest", async () => {
    // It used to keep `[first]` and report the others as ignored, because one
    // screen held one run. Enqueuing is starting a *free* workflow that parks at
    // its own gate, so five books are five imports approved one at a time.
    pickSource.mockResolvedValueOnce("/home/a/libros/uno.pdf");
    const { container } = render(<ImportScreen />);
    fireEvent.click(chooser(container));
    await waitFor(() => expect(container.textContent).toContain("uno.pdf"));

    pickSource.mockResolvedValueOnce("/home/a/libros/dos.pdf");
    fireEvent.click(chooser(container));
    await waitFor(() => expect(container.textContent).toContain("dos.pdf"));
    expect(container.textContent).toContain("uno.pdf");
  });

  it("does not queue the same file twice", async () => {
    // Two runs over one file would both be billed, and choosing it twice is a
    // slip rather than a request.
    pickSource.mockResolvedValue("/home/a/libros/uno.pdf");
    const { container } = render(<ImportScreen />);
    fireEvent.click(chooser(container));
    await waitFor(() => expect(container.textContent).toContain("uno.pdf"));
    fireEvent.click(chooser(container));
    await waitFor(() => expect(pickSource).toHaveBeenCalledTimes(2));

    const shown = container.querySelectorAll(".chosen-files li");
    expect(shown.length).toBe(1);
  });

  it("starts one run per file, and keeps going past one that is refused", async () => {
    // A batch is not all-or-nothing: refusing four good books because the fifth
    // is a `.pptx` throws away work that was fine.
    pickSource.mockResolvedValueOnce("/home/a/libros/uno.pdf");
    stageSource.mockResolvedValue({
      sourcePath: "/workspace/inbox/x.pdf",
      sourceKey: "x.pdf",
      byteSize: 1,
    });
    const { container } = render(<ImportScreen />);
    fireEvent.click(chooser(container));
    await waitFor(() => expect(container.textContent).toContain("uno.pdf"));
    pickSource.mockResolvedValueOnce("/home/a/libros/dos.pptx");
    fireEvent.click(chooser(container));
    await waitFor(() => expect(container.textContent).toContain("dos.pptx"));

    ingestStart
      .mockResolvedValueOnce({ workflowId: "ingest-1", state: "running" })
      .mockRejectedValueOnce({ kind: "unsupported_format", message: "pptx no" });

    fireEvent.click(preview(container));
    await waitFor(() => expect(ingestStart).toHaveBeenCalledTimes(2));

    // The one that worked leaves the chooser; the one that did not stays put,
    // beside the reason, so it can be removed or retried without re-picking.
    await waitFor(() => expect(container.textContent).not.toContain("uno.pdf"));
    expect(container.textContent).toContain("dos.pptx");
    expect(container.textContent).toContain("pptx no");
  });

  it("renders without drag and drop rather than failing without a webview", () => {
    // jsdom provides no Tauri webview, so the subscription throws. The chooser
    // does the same job, so losing the drop must cost nothing else on screen.
    const { container } = render(<ImportScreen />);
    expect(container.textContent).toContain(t("import.dropHint"));
    expect(container.querySelector(".error")).toBeNull();
  });
});

describe("the name shown for a chosen file", () => {
  it("is the last component, for either separator", () => {
    expect(fileName("/home/a/libros/El reto de Dios.txt")).toBe("El reto de Dios.txt");
    expect(fileName("C:\\Users\\a\\libro.pdf")).toBe("libro.pdf");
  });

  it("falls back to the whole string rather than rendering a blank", () => {
    expect(fileName("libro.pdf")).toBe("libro.pdf");
    expect(fileName("")).toBe("");
  });
});
