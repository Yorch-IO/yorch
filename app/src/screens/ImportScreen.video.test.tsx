/**
 * Indexing a video, which is not indexing a file.
 *
 * This screen has a text box for links and none for paths, and the two are not
 * inconsistent. A path typed by hand could never work in either plane — the app
 * and the worker see two different namespaces, so the string a person types is
 * meaningless to the thing that would open it. A URL is the same string
 * everywhere, which is why it can be typed and why nothing stages it.
 *
 * `cleanup` by hand, as everywhere else here.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { DEFAULT_STAGES, type StageOptions, type VideoFetchEvent } from "../lib/api";
import { fetchingLabel, ImportScreen } from "./ImportScreen";

const { stageSource, ingestStart, ingestGate, videoStart } = vi.hoisted(() => ({
  stageSource: vi.fn(),
  ingestStart: vi.fn(),
  ingestGate: vi.fn(),
  videoStart: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      pickSource: vi.fn(),
      stageSource,
      ingestStart,
      ingestGate,
      videoStart,
    },
  };
});

vi.mock("../lib/libraries", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/libraries")>();
  return {
    ...actual,
    useLibraries: () => ({
      selected: "lib_videos",
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
  videoStart.mockResolvedValue({ workflowId: "video-1", state: "running" });
});

const t = (key: string, o?: Record<string, unknown>): string => i18n.t(key, o ?? {});
const box = (c: HTMLElement) => c.querySelector("textarea") as HTMLTextAreaElement;
const startButton = (c: HTMLElement) =>
  Array.from(c.querySelectorAll("button")).find(
    (b) => b.textContent === t("import.videoStart"),
  ) as HTMLButtonElement;

const A = "https://youtu.be/dQw4w9WgXcQ";
const B = "https://www.youtube.com/watch?v=aQw4w9WgXcQ";

describe("indexing videos from links", () => {
  it("offers a box for URLs even though it offers none for paths", () => {
    const { container } = render(<ImportScreen />);
    expect(box(container)).toBeTruthy();
    // Still no path field: the reason that one was removed is unchanged.
    const typeable = Array.from(container.querySelectorAll("input")).filter(
      (i) => i.type !== "checkbox",
    );
    expect(typeable).toEqual([]);
  });

  it("will not start with an empty box", () => {
    const { container } = render(<ImportScreen />);
    expect(startButton(container).disabled).toBe(true);
  });

  it("makes one run per link, so a paste of two is two imports", async () => {
    const { container } = render(<ImportScreen />);
    fireEvent.change(box(container), { target: { value: `${A}\n${B}` } });
    fireEvent.click(startButton(container));

    await waitFor(() => expect(videoStart).toHaveBeenCalledTimes(2));
    expect(videoStart.mock.calls.map((c) => c[0])).toEqual([
      { libraryId: "lib_videos", url: A },
      { libraryId: "lib_videos", url: B },
    ]);
  });

  it("stages nothing, because there is no file", async () => {
    const { container } = render(<ImportScreen />);
    fireEvent.change(box(container), { target: { value: A } });
    fireEvent.click(startButton(container));

    await waitFor(() => expect(videoStart).toHaveBeenCalled());
    expect(stageSource).not.toHaveBeenCalled();
    expect(ingestStart).not.toHaveBeenCalled();
  });

  it("sends the defaults, not the boxes this screen shows, because the probe decides", async () => {
    // What a video should run depends on where its transcript comes from, and
    // that is not known until the probe has looked. The gate carries a
    // `recommended` and opens with those boxes ticked.
    //
    // Asserted on the switches rather than on the call's **arity**, which is
    // what this used to check. Arity was a proxy for "the screen adds nothing"
    // and stopped being one the moment there was something else to add: cloud
    // mode passes a progress callback, because the two YouTube calls now
    // happen on this machine and a window would otherwise sit still for
    // minutes. The property was always about the switches.
    const { container } = render(<ImportScreen />);
    const correct = container.querySelector<HTMLInputElement>(
      'input[type="checkbox"]',
    );
    if (correct) fireEvent.click(correct);

    fireEvent.change(box(container), { target: { value: A } });
    fireEvent.click(startButton(container));

    await waitFor(() => expect(videoStart).toHaveBeenCalled());
    const [, options] = videoStart.mock.calls[0] as [unknown, StageOptions];
    expect(options).toEqual(DEFAULT_STAGES);
    expect(correct?.checked).toBe(false);
  });

  it("reports what this machine is doing while it fetches, and never a measured zero", async () => {
    // The only thing on screen between pressing the button and the run row
    // existing. In cloud mode that gap is seconds of resolving and, for a
    // video with no captions, minutes of downloading audio — all of it before
    // the workflow starts, so the queue has nothing to show.
    //
    // The `total` case is the one worth pinning: yt-dlp reports 0 when it has
    // no estimate, and rendering that as "of 0 MB" or as 0% would be a
    // measured claim about a figure nobody measured.
    const label = (event: VideoFetchEvent) =>
      fetchingLabel(event, i18n.t.bind(i18n) as never);

    expect(label({ step: "resolving" })).toBe(i18n.t("import.fetch.resolving"));
    expect(label({ step: "downloading", bytes: 5 * 1024 * 1024, total: 20 * 1024 * 1024 })).toBe(
      i18n.t("import.fetch.downloadingOf", { done: "5 MB", total: "20 MB" }),
    );
    const unknown = label({ step: "downloading", bytes: 5 * 1024 * 1024, total: 0 });
    expect(unknown).toContain("5 MB");
    expect(unknown).not.toContain("0 MB");
  });

  it("keeps a refused link in the box beside its reason, and clears the rest", async () => {
    // A batch is not all-or-nothing: a playlist URL among four videos is one
    // refusal, and emptying the box would throw away three imports that were
    // fine.
    videoStart.mockImplementation(async (request: { url: string }) => {
      if (request.url === B) throw new Error("no es el enlace de un vídeo");
      return { workflowId: "video-1", state: "running" };
    });

    const { container } = render(<ImportScreen />);
    fireEvent.change(box(container), { target: { value: `${A}\n${B}` } });
    fireEvent.click(startButton(container));

    await waitFor(() => expect(box(container).value).toBe(B));
    expect(container.textContent).toContain("no es el enlace de un vídeo");
  });

  it("splits on any whitespace, so a pasted list works however it was copied", async () => {
    const { container } = render(<ImportScreen />);
    fireEvent.change(box(container), { target: { value: `  ${A}  ,\n\n ${B} \n` } });
    fireEvent.click(startButton(container));

    await waitFor(() => expect(videoStart).toHaveBeenCalledTimes(2));
    expect(videoStart.mock.calls.map((c) => c[0].url)).toEqual([A, B]);
  });
});
