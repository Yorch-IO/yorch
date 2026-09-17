/**
 * The two questions the panel exists to answer at a glance: **is the
 * transcriber installed**, and **will it use the GPU or the processor**.
 *
 * The second one is why this file is longer than it looks. `backend` is what
 * the *build* links against and `device` is what whisper.cpp said it actually
 * used, and they disagree on exactly the machines where it matters — a binary
 * built with CUDA on a host whose driver or card it cannot reach hints `cuda`
 * and runs on the processor at a fraction of the speed. A panel that merged
 * the two into one line would tell somebody a four-day batch was a four-hour
 * one, so the tests below pin that they are rendered differently.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import i18n from "../i18n";
import * as transcriber from "../lib/localTranscriber";
import { LocalTranscriber } from "./LocalTranscriber";

const t = (key: string, o?: Record<string, unknown>): string => i18n.t(key, o ?? {});

const STATE = (over: Partial<ReturnType<typeof transcriber.useLocalTranscriber>> = {}) =>
  ({
    status: {
      installed: true,
      binary: "/opt/company-brain/whisper-cli",
      error: null,
      backend: "cuda",
      device: null,
      canProbe: true,
      models: [
        { name: "large-v3-turbo", file: "f", bytes: 1_624_555_275, note: "the default", present: true, path: "p" },
      ],
      defaultModel: "large-v3-turbo",
      modelsDir: "/m",
      measuredSpeed: null,
      measuredOn: null,
      decodable: ["mp3", "wav", "flac", "ogg"],
    },
    model: "large-v3-turbo",
    setModel: () => {},
    readiness: "ready",
    jobs: [],
    current: null,
    attempt: null,
    history: [],
    paused: false,
    setPaused: () => {},
    probe: async () => true,
    probing: false,
    download: async () => false,
    downloading: null,
    giveUp: async () => false,
    hoursLeft: null,
    refresh: async () => {},
    error: null,
    ...over,
  }) as ReturnType<typeof transcriber.useLocalTranscriber>;

afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

it("says the transcriber is installed, and where it is", () => {
  vi.spyOn(transcriber, "useLocalTranscriber").mockReturnValue(STATE());
  const { container } = render(<LocalTranscriber />);
  expect(container.textContent).toContain(t("whisper.installed"));
  expect(container.textContent).toContain("/opt/company-brain/whisper-cli");
});

it("says it is not installed, and how to fix that, when the build carries none", () => {
  vi.spyOn(transcriber, "useLocalTranscriber").mockReturnValue(
    STATE({
      status: {
        ...STATE().status!,
        installed: false,
        binary: null,
        backend: "none",
        canProbe: false,
        error: "la aplicación no trae whisper-cli: ejecuta app/src-tauri/binaries/fetch-whisper.sh",
      },
    }),
  );
  const { container } = render(<LocalTranscriber />);
  expect(container.textContent).toContain(t("whisper.notInstalled"));
  // The sentence with the command in it, not a bare refusal.
  expect(container.textContent).toContain("fetch-whisper.sh");
  // And no device claim at all: there is nothing to run anything on.
  expect(container.textContent).not.toContain(t("whisper.runsOn", { backend: "" }).slice(0, 12));
});

it("marks the device as a guess until the binary has been asked", () => {
  vi.spyOn(transcriber, "useLocalTranscriber").mockReturnValue(STATE());
  const { container } = render(<LocalTranscriber />);
  expect(container.textContent).toContain(
    t("whisper.runsOn", { backend: t("whisper.backends.cuda") }),
  );
  // The whole point: a build that links CUDA has not thereby found a GPU.
  expect(container.textContent).toContain(t("whisper.deviceGuess"));
  expect(container.textContent).not.toContain(t("whisper.deviceFromProbe", { device: "", model: "" }).slice(0, 12));
});

it("states the device as a fact once something has asked, and says which kind of evidence", async () => {
  const probe = vi.fn().mockResolvedValue(true);
  vi.spyOn(transcriber, "useLocalTranscriber").mockReturnValue(
    STATE({
      probe,
      status: {
        ...STATE().status!,
        device: { backend: "cpu", device: "CPU", how: "probe", model: "base", at: "1789667000" },
      },
    }),
  );
  const { container } = render(<LocalTranscriber />);
  // The hint said cuda; the machine said CPU, and the machine wins.
  expect(container.textContent).toContain(
    t("whisper.runsOn", { backend: t("whisper.backends.cpu") }),
  );
  expect(container.textContent).not.toContain(t("whisper.deviceGuess"));
  expect(container.textContent).toContain(
    t("whisper.deviceFromProbe", { device: "CPU", model: "base" }),
  );

  const button = Array.from(container.querySelectorAll("button")).find(
    (b) => b.textContent === t("whisper.probe"),
  )!;
  fireEvent.click(button);
  await waitFor(() => expect(probe).toHaveBeenCalledTimes(1));
});

it("refuses to offer the check when no model is downloaded, and says why", () => {
  vi.spyOn(transcriber, "useLocalTranscriber").mockReturnValue(
    STATE({
      readiness: "no_model",
      status: {
        ...STATE().status!,
        canProbe: false,
        models: [{ ...STATE().status!.models[0]!, present: false }],
      },
    }),
  );
  const { container } = render(<LocalTranscriber />);
  const button = Array.from(container.querySelectorAll("button")).find(
    (b) => b.textContent === t("whisper.probe"),
  )!;
  // whisper.cpp names its device while loading a model; with none there is no
  // price at which the question can be answered.
  expect(button.disabled).toBe(true);
  expect(container.textContent).toContain(t("whisper.probeNeedsModel"));
});
