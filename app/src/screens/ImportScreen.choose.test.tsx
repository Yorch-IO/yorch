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

const t = (key: string): string => i18n.t(key);
const buttons = (c: HTMLElement) => Array.from(c.querySelectorAll("button"));
const chooser = (c: HTMLElement) => buttons(c)[0] as HTMLButtonElement;
/** By its text, not by position: an error panel renders its own Dismiss button
 *  below this one, so "the last button" stops being Preview exactly in the test
 *  that needs it most. */
const preview = (c: HTMLElement) =>
  buttons(c).find(
    (b) => b.textContent === t("import.start") || b.textContent === t("import.working"),
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
