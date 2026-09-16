/**
 * The channel screen, at the two moments money is decided.
 *
 * jsdom lays out nothing, so what is asserted here is *what is sent* — the
 * stage options a probe is started with, and the approval each gate receives.
 * The arithmetic behind the table is asserted as numbers in
 * `lib/channel.test.ts`, which is where it can be.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { BackendProvider } from "../lib/backend";
import { ChannelsProvider } from "../lib/channels";
import { LibrariesProvider } from "../lib/libraries";
import { ChannelScreen } from "./ChannelScreen";

const {
  channels,
  channelDetail,
  channelQuote,
  channelDiscover,
  channelReading,
  channelTopics,
  videoStart,
  videoGate,
  runStatus,
  ingestApprove,
  libraries,
} = vi.hoisted(() => ({
  channels: vi.fn(),
  channelDetail: vi.fn(),
  channelQuote: vi.fn(),
  channelDiscover: vi.fn(),
  channelReading: vi.fn(),
  channelTopics: vi.fn(),
  videoStart: vi.fn(),
  videoGate: vi.fn(),
  runStatus: vi.fn(),
  ingestApprove: vi.fn(),
  libraries: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      channels,
      channelDetail,
      channelQuote,
      channelDiscover,
      channelReading,
      channelTopics,
      videoStart,
      videoGate,
      runStatus,
      ingestApprove,
      libraries,
    },
  };
});

/** The screen reads its channel from the provider the shell mounts, so the
 *  test mounts the same three. `BackendProvider`'s own read fails here and it
 *  falls back to the local plane, which is the default the app ships with. */
function mount() {
  return render(
    <BackendProvider>
      <LibrariesProvider>
        <ChannelsProvider>
          <ChannelScreen />
        </ChannelsProvider>
      </LibrariesProvider>
    </BackendProvider>,
  );
}

afterEach(cleanup);

const CHANNEL = "UCabcdefghijklmnopqrstuv";
const LIBRARY = `lib_yt_${CHANNEL}`;

const t = (key: string, o?: Record<string, unknown>): string => i18n.t(key, o ?? {});

const button = (c: HTMLElement, key: string, o?: Record<string, unknown>) =>
  Array.from(c.querySelectorAll("button")).find(
    (b) => b.textContent === t(key, o),
  ) as HTMLButtonElement | undefined;

const summary = {
  channel: {
    channelId: CHANNEL,
    title: "Casa Sobre la Roca",
    handle: "casarocachannel",
    description: "",
    uploadsPlaylistId: "UUabc",
    url: "https://www.youtube.com/@casarocachannel",
  },
  libraryId: LIBRARY,
  syncedAt: "2026-09-16T00:00:00+00:00",
  videoCount: 2,
  unitsSpent: 3,
};

const videos = ["aaaaaaaaaaa", "bbbbbbbbbbb"].map((id, i) => ({
  videoId: id,
  // Two titles sharing one word and differing in another, so a chip can
  // narrow the table to exactly one of them.
  title: i === 0 ? "Prédica sobre la justicia" : "Prédica sobre el perdón",
  description: "d",
  descriptionTruncated: false,
  publishedAt: "2026-01-01T00:00:00Z",
  durationS: 4573,
  liveState: "none",
  thumbnail: "",
  url: `https://youtu.be/${id}`,
  documentId: null,
  activeVersionId: null,
}));

const estimate = {
  stages: [
    {
      stage: "channel-preselect",
      model: "gemini-3.6-flash",
      inputTokens: 1000,
      outputTokens: 700,
      usd: 0.0067,
      outputTokensHigh: 700,
      usdHigh: 0.0067,
    },
  ],
  totalUsd: 0.0067,
  totalUsdHigh: 0.0067,
  priceSource: "terceros",
  unpricedStages: [],
};

const gate = (runId: string, captions = true) => ({
  runId,
  documentId: "doc",
  versionId: "ver",
  probe: { chosen: captions ? { language: "es-orig", kind: "auto", ext: "vtt" } : null },
  estimate: { ...estimate, totalUsd: 0.15, totalUsdHigh: 0.18 },
  preview: null,
  transcript: null,
  warnings: [],
  recommended: { correct: true, embed: true },
});

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage("es");
  libraries.mockResolvedValue({ libraries: [] });
  channels.mockResolvedValue({ channels: [summary] });
  channelDetail.mockResolvedValue({ ...summary, videos });
  channelQuote.mockResolvedValue({
    channelId: CHANNEL,
    libraryId: LIBRARY,
    topic: "justicia social",
    evaluated: 2,
    read: 2,
    unmeasured: 0,
    estimate,
  });
  channelDiscover.mockResolvedValue({ workflowId: "channel-1", state: "running" });
  runStatus.mockResolvedValue({ workflowId: "channel-1", state: "succeeded" });
  channelReading.mockResolvedValue({
    channelId: CHANNEL,
    workflowId: "channel-1",
    state: "succeeded",
    stage: "done",
    usdSoFar: 0.007,
    estimate,
    preselection: {
      topic: "justicia social",
      model: "gemini-3.6-flash",
      prompt_version: "preselect/1",
      evaluated: videos.map((v) => v.videoId),
      candidates: videos.map((v, i) => ({
        video_id: v.videoId,
        relevancia: i === 0 ? "relevante" : "dudoso",
        puntaje: 90 - i * 10,
        razon: "el título lo dice",
        incertidumbre: "baja",
      })),
      invented: 0,
      malformed: 0,
      unevaluated: 0,
    },
    topics: null,
  });
  videoStart.mockImplementation(async () => ({
    workflowId: `video-${videoStart.mock.calls.length}`,
    state: "running",
  }));
  videoGate.mockImplementation(async (id: string) => gate(id));
  ingestApprove.mockResolvedValue(undefined);
});

async function upToProbes(container: HTMLElement) {
  await waitFor(() => expect(channelDetail).toHaveBeenCalled());
  const topic = container.querySelector(
    'input[placeholder="justicia social y pobreza"]',
  ) as HTMLInputElement;
  fireEvent.change(topic, { target: { value: "justicia social" } });
  fireEvent.click(button(container, "channel.quote")!);
  await waitFor(() => expect(channelQuote).toHaveBeenCalled());
  fireEvent.click(button(container, "channel.discover")!);
  await waitFor(() => expect(channelReading).toHaveBeenCalled());
}

describe("ChannelScreen", () => {
  it("renders whole when the channel list cannot be read", async () => {
    // The stack being down is the ordinary state of a freshly opened app. The
    // failure itself is the picker's to report — it lives in the top bar now —
    // and the screen renders its heading and its empty state rather than blank.
    channels.mockRejectedValue({ kind: "control_unreachable", message: "nope" });
    const { container } = mount();
    await waitFor(() => expect(channels).toHaveBeenCalled());
    expect(container.textContent).toContain(t("channel.title"));
    await waitFor(() => expect(container.textContent).toContain(t("channel.none")));
    expect(channelDetail).not.toHaveBeenCalled();
  });

  it("will not analyse before it has quoted", async () => {
    // Pressing the button is the decision, so the figure has to be on screen
    // before the call that starts the run.
    const { container } = mount();
    await waitFor(() => expect(channelDetail).toHaveBeenCalled());
    const topic = container.querySelector(
      'input[placeholder="justicia social y pobreza"]',
    ) as HTMLInputElement;
    fireEvent.change(topic, { target: { value: "justicia social" } });
    expect(button(container, "channel.discover")!.disabled).toBe(true);

    fireEvent.click(button(container, "channel.quote")!);
    await waitFor(() =>
      expect(button(container, "channel.discover")!.disabled).toBe(false),
    );
    expect(channelDiscover).not.toHaveBeenCalled();
  });

  it("probes the relevant and the doubtful, and never the discarded", async () => {
    const { container } = mount();
    await upToProbes(container);

    fireEvent.click(button(container, "channel.probeStart", { count: 2 })!);
    await waitFor(() => expect(videoStart).toHaveBeenCalledTimes(2));
    expect(videoStart.mock.calls.map((c) => c[0].url)).toEqual([
      "https://youtu.be/aaaaaaaaaaa",
      "https://youtu.be/bbbbbbbbbbb",
    ]);
    expect(videoStart.mock.calls[0]![0].libraryId).toBe(LIBRARY);
  });

  it("starts each probe with the stages its gate will quote", async () => {
    // The whole reason the semantics switch sits before the probe and not at
    // the gate: `estimate_video` quotes the options the run was started with,
    // and approving past a quote is the under-reporting failure.
    const { container } = mount();
    await upToProbes(container);

    // By id, not the first checkbox in the document: the table sits before
    // this panel in the DOM now, so the first checkbox is a video's tick.
    const semantics = container.querySelector("#channel-semantics") as HTMLInputElement;
    fireEvent.click(semantics);
    fireEvent.click(button(container, "channel.probeStart", { count: 2 })!);
    await waitFor(() => expect(videoStart).toHaveBeenCalledTimes(2));

    const options = videoStart.mock.calls[0]![1];
    expect(options.extractSemantics).toBe(true);
    // Stages a video run does not have stay off whatever the document
    // defaults say: the gate must not quote work that cannot happen.
    expect(options.learnProfile).toBe(false);
    expect(options.generateEvalset).toBe(false);
    expect(options.tune).toBe(false);
  });

  it("approves what is ticked and rejects what is not", async () => {
    const { container } = mount();
    await upToProbes(container);
    fireEvent.click(button(container, "channel.probeStart", { count: 2 })!);
    await waitFor(() => expect(videoGate).toHaveBeenCalled());
    await waitFor(() =>
      expect(container.querySelectorAll("table.candidates tbody tr")).toHaveLength(2),
    );

    // Both arrive ticked — they have captions and are not indexed — so untick
    // one and approve. Waiting for *both* rather than the first: a row whose
    // gate has not landed is still disabled, and clicking it does nothing.
    const boxes = () =>
      Array.from(
        container.querySelectorAll('table.candidates input[type="checkbox"]'),
      ) as HTMLInputElement[];
    await waitFor(() => expect(boxes().every((b) => b.checked)).toBe(true));
    fireEvent.click(boxes()[1]!);

    fireEvent.click(button(container, "channel.approve", { count: 1 })!);
    await waitFor(() => expect(ingestApprove).toHaveBeenCalledTimes(2));

    const decisions = ingestApprove.mock.calls.map((c) => [c[0], c[1].approved]);
    expect(decisions).toContainEqual(["video-1", true]);
    expect(decisions).toContainEqual(["video-2", false]);
    // The gate's own recommendation per row, because the probe is what knows
    // whether this video's captions are automatic.
    expect(ingestApprove.mock.calls[0]![1].options.correct).toBe(true);
  });

  it("leaves a video with no captions unticked", async () => {
    // It is both the worst informed row — no transcript, so its only evidence
    // is a title — and the most expensive, by about twelve times.
    videoGate.mockImplementation(async (id: string) => gate(id, id === "video-1"));
    const { container } = mount();
    await upToProbes(container);
    fireEvent.click(button(container, "channel.probeStart", { count: 2 })!);
    await waitFor(() =>
      expect(container.querySelectorAll("table.candidates tbody tr")).toHaveLength(2),
    );
    const boxes = () =>
      Array.from(
        container.querySelectorAll('table.candidates input[type="checkbox"]'),
      ) as HTMLInputElement[];
    await waitFor(() => expect(boxes()[0]!.checked).toBe(true));
    // Seeded ticked by the discovery, then unticked the moment its gate said
    // "no captions" — and left enabled, so re-ticking it is a choice the
    // totals then print the price of.
    await waitFor(() => expect(container.textContent).toContain(t("channel.sourceTranscribe")));
    await waitFor(() => expect(boxes()[1]!.checked).toBe(false));
    expect(boxes()[1]!.disabled).toBe(false);
  });

  // --- the tick, before the probe ------------------------------------------

  it("lets a catalogue video be ticked before it is probed, and probes it", async () => {
    // The fix for the checkbox that read as broken: it was the approval tick,
    // disabled until a gate existed. One tick now drives the whole flow, so a
    // person can shortlist a channel by hand and pay for no metadata pass.
    const { container } = mount();
    await waitFor(() => expect(channelDetail).toHaveBeenCalled());
    const boxes = () =>
      Array.from(
        container.querySelectorAll('table.candidates input[type="checkbox"]'),
      ) as HTMLInputElement[];
    await waitFor(() => expect(boxes()).toHaveLength(2));
    expect(boxes().every((b) => !b.disabled)).toBe(true);

    fireEvent.click(boxes()[1]!);
    expect(boxes()[1]!.checked).toBe(true);
    fireEvent.click(button(container, "channel.probeStart", { count: 1 })!);
    await waitFor(() => expect(videoStart).toHaveBeenCalledTimes(1));
    expect(videoStart.mock.calls[0]![0].url).toBe("https://youtu.be/bbbbbbbbbbb");
    expect(channelDiscover).not.toHaveBeenCalled();
  });

  it("caps the probes at what is left of the transcript budget", async () => {
    // Two rounds against one cap: the second round gets what the first left.
    const { container } = mount();
    await waitFor(() => expect(channelDetail).toHaveBeenCalled());
    const deep = container.querySelector('input[type="number"][max="25"]') as HTMLInputElement;
    fireEvent.change(deep, { target: { value: "1" } });
    fireEvent.click(button(container, "channel.pickAll", { count: 2 })!);
    expect(container.textContent).toContain(t("channel.overCap", { count: 1 }));
    fireEvent.click(button(container, "channel.probeStart", { count: 1 })!);
    await waitFor(() => expect(videoStart).toHaveBeenCalledTimes(1));
    // The budget is spent; the other tick waits until the cap is raised.
    await waitFor(() =>
      expect(button(container, "channel.probeStart", { count: 0 })!.disabled).toBe(true),
    );
  });

  // --- the keyword chips ---------------------------------------------------

  const chip = (c: HTMLElement, label: string) =>
    Array.from(c.querySelectorAll("button.keyword-chip")).find((b) =>
      b.textContent?.startsWith(label),
    ) as HTMLButtonElement | undefined;

  it("offers the words the titles share, minus the ones on every title", async () => {
    const { container } = mount();
    await waitFor(() => expect(channelDetail).toHaveBeenCalled());
    await waitFor(() => expect(chip(container, "justicia")).toBeTruthy());
    expect(chip(container, "perdón")).toBeTruthy();
    // Two titles is under the boilerplate floor, so "Prédica" survives here;
    // the share rule is asserted in `keywords.test.ts`, where it is a number.
    expect(chip(container, "Prédica")).toBeTruthy();
  });

  it("seeds the topic and narrows the table when a chip is pressed", async () => {
    const { container } = mount();
    await waitFor(() => expect(channelDetail).toHaveBeenCalled());
    await waitFor(() => expect(chip(container, "justicia")).toBeTruthy());
    fireEvent.click(chip(container, "justicia")!);

    const topic = container.querySelector(
      'input[placeholder="justicia social y pobreza"]',
    ) as HTMLInputElement;
    expect(topic.value).toBe("justicia");
    expect(chip(container, "justicia")!.getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelectorAll("table.candidates tbody tr")).toHaveLength(1);
    expect(container.textContent).toContain(
      t("channel.filtered", { shown: 1, total: 2 }),
    );

    // The field stays the person's: typing over it leaves the chip pressed.
    fireEvent.change(topic, { target: { value: "justicia y misericordia" } });
    expect(chip(container, "justicia")!.getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelectorAll("table.candidates tbody tr")).toHaveLength(1);

    fireEvent.click(button(container, "channel.keywordsClear")!);
    expect(container.querySelectorAll("table.candidates tbody tr")).toHaveLength(2);
  });

  it("quotes and discovers over the filtered videos only", async () => {
    // The quote is the figure shown and the run is what is approved; both
    // carry the same ids or one could price a set the other does not judge.
    const { container } = mount();
    await waitFor(() => expect(channelDetail).toHaveBeenCalled());
    await waitFor(() => expect(chip(container, "perdón")).toBeTruthy());
    fireEvent.click(chip(container, "perdón")!);
    fireEvent.click(button(container, "channel.quote")!);
    await waitFor(() => expect(channelQuote).toHaveBeenCalled());
    expect(channelQuote.mock.calls[0]![4]).toEqual(["bbbbbbbbbbb"]);
    await waitFor(() => expect(button(container, "channel.discover")!.disabled).toBe(false));
    fireEvent.click(button(container, "channel.discover")!);
    await waitFor(() => expect(channelDiscover).toHaveBeenCalled());
    expect(channelDiscover.mock.calls[0]![4]).toEqual(["bbbbbbbbbbb"]);
  });

  it("sends no filter when no chip is pressed", async () => {
    const { container } = mount();
    await upToProbes(container);
    expect(channelQuote.mock.calls[0]![4]).toBeUndefined();
    expect(channelDiscover.mock.calls[0]![4]).toBeUndefined();
  });

  it("drops the quote when the filter changes, so nothing runs on a stale figure", async () => {
    const { container } = mount();
    await waitFor(() => expect(channelDetail).toHaveBeenCalled());
    const topic = container.querySelector(
      'input[placeholder="justicia social y pobreza"]',
    ) as HTMLInputElement;
    fireEvent.change(topic, { target: { value: "justicia social" } });
    fireEvent.click(button(container, "channel.quote")!);
    await waitFor(() => expect(button(container, "channel.discover")!.disabled).toBe(false));

    await waitFor(() => expect(chip(container, "justicia")).toBeTruthy());
    fireEvent.click(chip(container, "justicia")!);
    expect(button(container, "channel.discover")!.disabled).toBe(true);
    expect(button(container, "channel.readStart")!.disabled).toBe(true);
    expect(channelDiscover).not.toHaveBeenCalled();
  });

  it("keeps a video with a pending probe on screen under a filter that excludes it", async () => {
    // A parked gate is a pending decision, and a filter must not hide one.
    const { container } = mount();
    await waitFor(() => expect(channelDetail).toHaveBeenCalled());
    const boxes = () =>
      Array.from(
        container.querySelectorAll('table.candidates input[type="checkbox"]'),
      ) as HTMLInputElement[];
    await waitFor(() => expect(boxes()).toHaveLength(2));
    fireEvent.click(boxes()[1]!); // el perdón
    fireEvent.click(button(container, "channel.probeStart", { count: 1 })!);
    await waitFor(() => expect(videoGate).toHaveBeenCalled());

    fireEvent.click(chip(container, "justicia")!);
    const rows = container.querySelectorAll("table.candidates tbody tr");
    expect(rows).toHaveLength(2);
    expect(container.textContent).toContain(t("channel.outsideFilter"));
    // And it is not in the judged set the quote is asked about: it is probed.
    fireEvent.click(button(container, "channel.quote")!);
    await waitFor(() => expect(channelQuote).toHaveBeenCalled());
    expect(channelQuote.mock.calls[0]![4]).toEqual(["aaaaaaaaaaa"]);
  });
});
