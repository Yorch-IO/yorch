import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { ChannelSummary } from "./api";
import { BackendProvider } from "./backend";
import { ChannelPicker, ChannelsProvider, SYNC_LIMIT, useChannels } from "./channels";
import { LibrariesProvider } from "./libraries";

/**
 * The channel selection, shared between the top bar and the screen.
 *
 * What is worth asserting is what the shell used to get wrong for libraries:
 * that a sync puts the new channel in front of the person and on the shelf,
 * that the picker survives a list it cannot read, and that a remembered choice
 * comes back — scoped, so two planes never inherit each other's.
 */
const { channels, channelSync, libraries } = vi.hoisted(() => ({
  channels: vi.fn(),
  channelSync: vi.fn(),
  libraries: vi.fn(),
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, api: { ...actual.api, channels, channelSync, libraries } };
});

const summary = (channelId: string, title: string): ChannelSummary => ({
  channel: {
    channelId,
    title,
    handle: title.toLowerCase(),
    description: "",
    uploadsPlaylistId: `UU${channelId.slice(2)}`,
    url: `https://www.youtube.com/@${title}`,
  },
  libraryId: `lib_yt_${channelId}`,
  syncedAt: "2026-09-16T00:00:00+00:00",
  videoCount: 3,
  unitsSpent: 3,
});

const A = summary("UCaaaaaaaaaaaaaaaaaaaaaa", "Alfa");
const B = summary("UCbbbbbbbbbbbbbbbbbbbbbb", "Beta");

function Probe() {
  const { selected, select } = useChannels();
  return (
    <div>
      <p data-testid="selected">{selected}</p>
      <button type="button" onClick={() => select(B.channel.channelId)}>
        pick-b
      </button>
    </div>
  );
}

function mount() {
  return render(
    <BackendProvider>
      <LibrariesProvider>
        <ChannelsProvider>
          <ChannelPicker />
          <Probe />
        </ChannelsProvider>
      </LibrariesProvider>
    </BackendProvider>,
  );
}

const t = (key: string, o?: Record<string, unknown>): string => i18n.t(key, o ?? {});

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage("es");
  window.localStorage.clear();
  libraries.mockResolvedValue({ libraries: [] });
  channels.mockResolvedValue({ channels: [A] });
});

afterEach(cleanup);

describe("ChannelsProvider", () => {
  it("selects the first channel when nothing is remembered", async () => {
    mount();
    await waitFor(() =>
      expect(screen.getByTestId("selected").textContent).toBe(A.channel.channelId),
    );
  });

  it("remembers a choice and restores it, and never a channel the list no longer holds", async () => {
    channels.mockResolvedValue({ channels: [A, B] });
    mount();
    await waitFor(() => expect(screen.getByTestId("selected").textContent).toBe(A.channel.channelId));
    fireEvent.click(screen.getByText("pick-b"));
    expect(screen.getByTestId("selected").textContent).toBe(B.channel.channelId);
    cleanup();

    mount();
    await waitFor(() => expect(screen.getByTestId("selected").textContent).toBe(B.channel.channelId));
    cleanup();

    // The remembered channel is gone from the list: fall back rather than
    // produce an empty screen for an id nothing can answer.
    channels.mockResolvedValue({ channels: [A] });
    mount();
    await waitFor(() => expect(screen.getByTestId("selected").textContent).toBe(A.channel.channelId));
  });

  it("syncs at the catalogue ceiling, selects the new channel and reloads the shelf", async () => {
    // A sync creates the channel's library on the server, so the library
    // picker on the other tabs has to see it without a relaunch.
    channelSync.mockImplementation(async () => {
      channels.mockResolvedValue({ channels: [B, A] });
      return B;
    });
    mount();
    await waitFor(() => expect(channels).toHaveBeenCalledTimes(1));
    const before = libraries.mock.calls.length;

    const url = screen.getByPlaceholderText(t("channel.urlPlaceholder")) as HTMLInputElement;
    const button = screen.getByText(t("channel.syncStart")) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.change(url, { target: { value: " https://www.youtube.com/@Beta " } });
    expect(button.disabled).toBe(false);
    fireEvent.click(button);

    await waitFor(() => expect(channelSync).toHaveBeenCalledWith("https://www.youtube.com/@Beta", SYNC_LIMIT));
    await waitFor(() =>
      expect(screen.getByTestId("selected").textContent).toBe(B.channel.channelId),
    );
    expect(libraries.mock.calls.length).toBeGreaterThan(before);
    expect(url.value).toBe("");
  });

  it("renders whole when the list cannot be read, with the error's guidance", async () => {
    // `youtube_key_missing` is the ordinary first failure on a fresh machine,
    // and its remedy is on another screen: say so, and offer a retry.
    channels.mockRejectedValue({ kind: "youtube_key_missing", message: "sin clave" });
    const { container } = mount();
    await waitFor(() => expect(container.textContent).toContain("sin clave"));
    expect(screen.getByPlaceholderText(t("channel.urlPlaceholder"))).toBeTruthy();
    channels.mockResolvedValue({ channels: [A] });
    fireEvent.click(screen.getByText(t("libraries.retry")));
    await waitFor(() =>
      expect(screen.getByTestId("selected").textContent).toBe(A.channel.channelId),
    );
  });

  it("reports a failed sync in the bar and keeps the field", async () => {
    channelSync.mockRejectedValue({ kind: "not_a_channel_url", message: "no es un canal" });
    const { container } = mount();
    await waitFor(() => expect(channels).toHaveBeenCalled());
    const url = screen.getByPlaceholderText(t("channel.urlPlaceholder")) as HTMLInputElement;
    fireEvent.change(url, { target: { value: "https://youtu.be/x" } });
    fireEvent.click(screen.getByText(t("channel.syncStart")));
    await waitFor(() => expect(container.textContent).toContain("no es un canal"));
    // The selection is untouched by a sync that did not happen, and the link
    // stays in the field to be corrected rather than retyped.
    expect(screen.getByTestId("selected").textContent).toBe(A.channel.channelId);
    expect(url.value).toBe("https://youtu.be/x");
  });
});
