import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { AnswerStyles } from "./AnswerStyles";

const { answerStyles, setAnswerStyle } = vi.hoisted(() => ({
  answerStyles: vi.fn(),
  setAnswerStyle: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, answerStyles, setAnswerStyle } };
});

const t = (key: string) => i18n.t(key);

function levels(over: Record<string, unknown> = {}) {
  return {
    maxChars: 2000,
    levels: [
      { effort: "brief", body: "corto", defaultBody: "corto", custom: false },
      { effort: "standard", body: "medio", defaultBody: "medio", custom: false },
      { effort: "thorough", body: "en verso", defaultBody: "largo", custom: true },
      ...[],
    ].map((l) => ({ ...l, ...(over[l.effort] as object ?? {}) })),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  answerStyles.mockResolvedValue(levels());
  setAnswerStyle.mockResolvedValue({ effort: "thorough", custom: true });
});

describe("editing the answer wording", () => {
  it("shows one box per level, filled with what would actually be used", async () => {
    const { container } = render(<AnswerStyles />);
    await waitFor(() => expect(answerStyles).toHaveBeenCalled());
    const boxes = container.querySelectorAll("textarea");
    expect([...boxes].map((b) => (b as HTMLTextAreaElement).value)).toEqual([
      "corto",
      "medio",
      "en verso",
    ]);
    cleanup();
  });

  it("saves only the level that was edited", async () => {
    const { container } = render(<AnswerStyles />);
    await waitFor(() => expect(answerStyles).toHaveBeenCalled());
    const box = container.querySelectorAll("textarea")[0]!;
    fireEvent.change(box, { target: { value: "más corto todavía" } });

    const save = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === t("styles.save") && !b.disabled,
    )!;
    fireEvent.click(save);
    await waitFor(() => expect(setAnswerStyle).toHaveBeenCalled());
    expect(setAnswerStyle).toHaveBeenCalledWith("brief", "más corto todavía");
    cleanup();
  });

  it("offers to restore only where an override exists", async () => {
    // Restoring a level nobody overrode changes nothing, and offering it
    // suggests the default is something other than what is already shown.
    const { container } = render(<AnswerStyles />);
    await waitFor(() => expect(answerStyles).toHaveBeenCalled());
    const restores = [...container.querySelectorAll("button")].filter(
      (b) => b.textContent === t("styles.restore"),
    );
    expect(restores.map((b) => b.disabled)).toEqual([true, true, false]);
    cleanup();
  });

  it("restores by saving an empty body", async () => {
    const { container } = render(<AnswerStyles />);
    await waitFor(() => expect(answerStyles).toHaveBeenCalled());
    const restore = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === t("styles.restore") && !b.disabled,
    )!;
    fireEvent.click(restore);
    await waitFor(() => expect(setAnswerStyle).toHaveBeenCalled());
    expect(setAnswerStyle).toHaveBeenCalledWith("thorough", "");
    cleanup();
  });

  it("re-reads after a save rather than patching what it already had", async () => {
    // Clearing an override makes the server fall back to a default this screen
    // would otherwise have to reproduce, and two places deciding what the
    // default is is how they come to disagree.
    const { container } = render(<AnswerStyles />);
    await waitFor(() => expect(answerStyles).toHaveBeenCalledTimes(1));
    const restore = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === t("styles.restore") && !b.disabled,
    )!;
    fireEvent.click(restore);
    await waitFor(() => expect(answerStyles).toHaveBeenCalledTimes(2));
    cleanup();
  });

  it("degrades to a notice rather than to the screen's error panel", async () => {
    // It sits inside Services, which is mostly about something else, and it is
    // the first block there to need the control API. Shouting would say the
    // whole screen was broken whenever the stack is merely not up yet.
    answerStyles.mockRejectedValue(new Error("nope"));
    const { container } = render(<AnswerStyles />);
    await waitFor(() => expect(screen.getByText(t("styles.retry"))).toBeTruthy());
    expect(container.querySelector(".error")).toBeNull();
    expect(container.querySelector(".notice")).toBeTruthy();
    cleanup();
  });
});
