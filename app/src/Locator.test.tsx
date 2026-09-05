/**
 * A locator's URL has to be openable, and everything else has to stay text.
 *
 * `cleanup()` by hand: vitest exposes no global `afterEach` here, so RTL never
 * registers its automatic cleanup and `screen` would otherwise find both trees.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Locator } from "./Locator";

const openUrl = vi.hoisted(() => vi.fn(async () => {}));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl }));

afterEach(() => {
  cleanup();
  openUrl.mockClear();
});

const VIDEO = "12:34 · https://youtu.be/dQw4w9WgXcQ?t=752";
const BOOK = "Catecismo Menor · 1.1 De la regla dada por Dios · [267:445]";

describe("Locator", () => {
  it("makes the URL part of a video locator openable", async () => {
    render(<Locator text={VIDEO} />);
    const button = screen.getByRole("button", {
      name: "https://youtu.be/dQw4w9WgXcQ?t=752",
    });
    button.click();
    expect(openUrl).toHaveBeenCalledWith("https://youtu.be/dQw4w9WgXcQ?t=752");
  });

  it("keeps the rest of the locator as text beside it", () => {
    const { container } = render(<Locator text={VIDEO} />);
    // The whole string still reads as one locator; only the address is a
    // control. A reader needs the time as much as the link.
    expect(container.textContent).toBe(VIDEO);
  });

  it("leaves a document locator entirely inert", () => {
    const { container } = render(<Locator text={BOOK} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.textContent).toBe(BOOK);
  });

  it("renders nothing at all for an empty locator", () => {
    // `answer._verify` drops a citation with no locator, so this should not
    // arrive — but a chunk browsed in Explore can genuinely have none, and an
    // empty control would be a button that does nothing.
    const { container } = render(<Locator text="" />);
    expect(container.textContent).toBe("");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("survives a host where opening a URL is not possible", async () => {
    openUrl.mockRejectedValueOnce(new Error("no opener here"));
    render(<Locator text={VIDEO} />);
    const button = screen.getByRole("button", { name: /youtu\.be/ });
    expect(() => button.click()).not.toThrow();
    // The address is the label, so it can still be read and copied.
    expect(button.textContent).toBe("https://youtu.be/dQw4w9WgXcQ?t=752");
  });
});
