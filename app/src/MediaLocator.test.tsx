import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "./i18n";
import { MediaLocator, isBucketLocator, startSeconds } from "./MediaLocator";

const { mediaLink, openUrl } = vi.hoisted(() => ({ mediaLink: vi.fn(), openUrl: vi.fn() }));

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return { ...actual, api: { ...actual.api, mediaLink } };
});
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl }));

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  openUrl.mockResolvedValue(undefined);
});

const t = (key: string) => i18n.t(key);

describe("reading the clock off a locator", () => {
  it("takes hh:mm:ss or mm:ss from the first fact, and nothing from a book's", () => {
    expect(startSeconds("00:12:34 · s3://b/audios/x.mp3")).toBe(754);
    expect(startSeconds("12:34 · https://youtu.be/x?t=754")).toBe(754);
    expect(startSeconds("1:02:03 · s3://b/k")).toBe(3723);
    expect(startSeconds("Título · p. 12 · [100:200]")).toBeNull();
    expect(isBucketLocator("00:12:34 · s3://b/audios/x.mp3")).toBe(true);
    expect(isBucketLocator("12:34 · https://youtu.be/x")).toBe(false);
  });
});

describe("the button", () => {
  it("renders a plain locator for anything that is not a bucket recording", () => {
    const { container } = render(
      <MediaLocator locator="Título · p. 12" libraryId="lib_1" documentId="doc_1" />,
    );
    expect(container.textContent).toBe("Título · p. 12");
    expect(container.querySelector("button")).toBeNull();
  });

  it("mints the link on click, at the cited second, and opens it", async () => {
    mediaLink.mockResolvedValue({
      url: "https://signed.example/x#t=754", expiresAt: "", startS: 754, sourceUrl: "https://feed/1",
    });
    const { container } = render(
      <MediaLocator locator="00:12:34 · s3://b/audios/x.mp3" libraryId="lib_s3_x" documentId="doc_1" />,
    );
    const play = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent === t("ask.mediaPlay"),
    )!;
    expect(play).toBeDefined();
    // Nothing is minted before the click: a link dies with the session that
    // signed it, so one made at render would be dead by the time it was used.
    expect(mediaLink).not.toHaveBeenCalled();
    fireEvent.click(play);
    await waitFor(() => expect(openUrl).toHaveBeenCalledWith("https://signed.example/x#t=754"));
    expect(mediaLink).toHaveBeenCalledWith("lib_s3_x", "doc_1", 754);
    // The feed link, which never expires, appears once the plane named it.
    await waitFor(() => expect(container.textContent).toContain(t("ask.mediaFeed")));
  });

  it("shows the refusal beside the locator rather than in a panel", async () => {
    mediaLink.mockRejectedValue({ kind: "control_status", message: "the control API returned 409: bucket_not_registered" });
    const { container } = render(
      <MediaLocator locator="00:00:10 · s3://b/k" libraryId="lib_s3_x" documentId="doc_1" />,
    );
    fireEvent.click(container.querySelector("button")!);
    await waitFor(() => expect(container.textContent).toContain("bucket_not_registered"));
    expect(openUrl).not.toHaveBeenCalled();
  });

  it("falls back to the plain locator without a document to address the link by", () => {
    const { container } = render(
      <MediaLocator locator="00:00:10 · s3://b/k" libraryId="lib_s3_x" documentId={undefined} />,
    );
    expect(container.querySelector("button")).toBeNull();
  });
});
