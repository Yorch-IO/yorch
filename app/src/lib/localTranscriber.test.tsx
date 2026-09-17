/**
 * The background queue, against a faked plane and a faked sidecar.
 *
 * What is measured is the behaviour nothing else can see: that it takes one
 * run at a time, that a finished one leaves the queue without waiting for the
 * next poll, that the transcript is uploaded before the file is forgotten,
 * that a run handed back to Amazon is not attempted again, and that in local
 * mode it asks the plane nothing at all — the last one because a queue that
 * polls a plane with no bucket routes would print a 404 into the Services
 * panel every twenty seconds.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BackendProvider } from "./backend";
import { LocalTranscriberProvider, useLocalTranscriber } from "./localTranscriber";
import { AWAITING } from "./transcribing";

const {
  backendSettings, runsList, bucketDetail, mediaLink, whisperStatus,
  whisperTranscribe, transcriptUpload, runSwitchTranscriber,
} = vi.hoisted(() => ({
  backendSettings: vi.fn(),
  runsList: vi.fn(),
  bucketDetail: vi.fn(),
  mediaLink: vi.fn(),
  whisperStatus: vi.fn(),
  whisperTranscribe: vi.fn(),
  transcriptUpload: vi.fn(),
  runSwitchTranscriber: vi.fn(),
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      backendSettings, runsList, bucketDetail, mediaLink, whisperStatus,
      whisperTranscribe, transcriptUpload, runSwitchTranscriber,
    },
  };
});

const CLOUD = {
  mode: "cloud", baseUrl: "https://api.example", tenantId: "tnt_1", signedIn: true,
  email: "a@b.c", secretStore: "keychain",
};
const LOCAL = { ...CLOUD, mode: "local" };

const STATUS = {
  installed: true, binary: "/opt/whisper-cli", error: null, backend: "cuda",
  device: { backend: "cuda", device: "CUDA0", how: "run", model: "large-v3-turbo", at: "0" },
  canProbe: true,
  models: [{ name: "large-v3-turbo", file: "f", bytes: 1_624_555_275, note: "", present: true, path: "p" }],
  defaultModel: "large-v3-turbo", modelsDir: "/m", measuredSpeed: 1.8,
  measuredOn: "large-v3-turbo · cuda · 1789648691", decodable: ["mp3", "wav", "flac", "ogg"],
};

const run = (id: string, over: Record<string, unknown> = {}) => ({
  id, workflowId: id, kind: "audio", state: AWAITING, stage: "transcribing",
  startedAt: "2026-09-17T10:00:00Z", finishedAt: null, errorKind: null, errorDetail: null,
  title: id, libraryId: "lib_s3_abc", label: null, documentId: `doc_${id}`, versionId: null,
  usdSoFar: 0, ...over,
});

const object = (runId: string, over: Record<string, unknown> = {}) => ({
  key: `audios/${runId}.mp3`, etag: "e", size: 1, lastModified: "", container: "mp3",
  durationS: 720, durationEstimated: false, title: `t-${runId}`, author: "", recordedAt: "",
  publishedAt: "", source: "", url: "", available: true, warnings: [],
  documentId: `doc_${runId}`, activeVersionId: null, runId, runState: AWAITING,
  state: "pending", ...over,
});

function Probe() {
  const { jobs, current, history, giveUp, readiness, hoursLeft } = useLocalTranscriber();
  return (
    <div>
      <p data-testid="ready">{readiness}</p>
      <p data-testid="jobs">{jobs.map((j) => j.workflowId).join(",")}</p>
      <p data-testid="current">{current?.workflowId ?? "-"}</p>
      <p data-testid="hours">{hoursLeft === null ? "-" : hoursLeft.toFixed(2)}</p>
      <p data-testid="history">
        {history.map((h) => `${h.workflowId}:${h.ok ? "ok" : "fail"}`).join(",")}
      </p>
      <button type="button" onClick={() => void giveUp("audio-1")}>
        give up
      </button>
    </div>
  );
}

const mount = () =>
  render(
    <BackendProvider>
      <LocalTranscriberProvider>
        <Probe />
      </LocalTranscriberProvider>
    </BackendProvider>,
  );

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  backendSettings.mockResolvedValue(CLOUD);
  whisperStatus.mockResolvedValue(STATUS);
  runsList.mockResolvedValue({ runs: [run("audio-1"), run("audio-2")], nextBefore: null });
  bucketDetail.mockResolvedValue({
    bucket: { bucketId: "abc", source: { language: "es-US" }, libraryId: "lib_s3_abc" },
    objects: [object("audio-1"), object("audio-2")],
    totals: { objects: 2, seconds: 1440, indexed: 0, pending: 2, unindexed: 0 },
  });
  mediaLink.mockResolvedValue({ url: "https://s3/presigned", expiresAt: "", startS: 0, sourceUrl: "" });
  whisperTranscribe.mockResolvedValue({
    path: "/cache/audio-1.json", engine: "whisper.cpp", model: "large-v3-turbo",
    language: "es", backend: "cuda", audioSeconds: 720, wallSeconds: 400, segments: 210,
  });
  transcriptUpload.mockResolvedValue({ workflowId: "audio-1", state: "running" });
  runSwitchTranscriber.mockResolvedValue({
    workflowId: "audio-1", state: "awaiting_approval", transcriber: "transcribe",
  });
});

describe("in cloud mode with a GPU and a model", () => {
  it("transcribes one run, uploads it, and drops it from the queue", async () => {
    mount();
    await waitFor(() => expect(screen.getByTestId("ready").textContent).toBe("ready"));
    await waitFor(() => expect(whisperTranscribe).toHaveBeenCalledTimes(1));

    // The presigned link is minted per run and never cached: it dies with the
    // assumed-role session, so a queue that kept one would hand whisper a dead
    // URL an hour into a batch.
    expect(mediaLink).toHaveBeenCalledWith("lib_s3_abc", "doc_audio-1", 0);
    expect(whisperTranscribe.mock.calls[0]![0]).toEqual({
      workflowId: "audio-1",
      audioUrl: "https://s3/presigned",
      container: "mp3",
      model: "large-v3-turbo",
      // `es-US` is Transcribe's spelling and whisper.cpp refuses the region.
      language: "es",
      audioSeconds: 720,
    });
    await waitFor(() =>
      expect(transcriptUpload).toHaveBeenCalledWith("audio-1", "/cache/audio-1.json", {
        engine: "whisper.cpp",
        model: "large-v3-turbo",
        language: "es",
      }),
    );
    await waitFor(() => expect(screen.getByTestId("history").textContent).toBe("audio-1:ok"));
    // Dropped now rather than at the next poll: a finished run left on screen
    // for twenty seconds reads as one that did not take.
    await waitFor(() => expect(screen.getByTestId("jobs").textContent).toBe("audio-2"));
    // One at a time, by construction: the second run is not started while the
    // first is in flight, and this tick has ended.
    expect(whisperTranscribe).toHaveBeenCalledTimes(1);
  });

  it("puts the hours left on screen from the speed it measured, not from the model's name", async () => {
    // With the model absent nothing is taken off the queue, so the figure is
    // the whole batch rather than a race against the first run finishing —
    // which is also the state a person is in when they are deciding.
    whisperStatus.mockResolvedValue({
      ...STATUS,
      models: [{ ...STATUS.models[0]!, present: false }],
    });
    mount();
    // Two recordings of twelve minutes at 1.8x real time.
    await waitFor(() => expect(screen.getByTestId("hours").textContent).toBe("0.22"));
  });

  it("keeps a failed run out of the way rather than retrying it for ever", async () => {
    whisperTranscribe.mockRejectedValue({ kind: "whisper_failed", message: "no" });
    mount();
    await waitFor(() => expect(screen.getByTestId("history").textContent).toContain("audio-1:fail"));
    // The transcript was never uploaded, and the run stays parked — the way
    // out is Amazon, which the row offers.
    expect(transcriptUpload).not.toHaveBeenCalled();
    expect(screen.getByTestId("jobs").textContent).toContain("audio-1");
  });

  it("hands a run back to Amazon and stops attempting it", async () => {
    mount();
    await waitFor(() => expect(whisperTranscribe).toHaveBeenCalled());
    screen.getByText("give up").click();
    await waitFor(() => expect(runSwitchTranscriber).toHaveBeenCalledWith("audio-1"));
    await waitFor(() => expect(screen.getByTestId("jobs").textContent).not.toContain("audio-1"));
  });
});

describe("with no model downloaded", () => {
  it("asks the plane what is waiting but transcribes nothing", async () => {
    whisperStatus.mockResolvedValue({
      ...STATUS,
      models: [{ ...STATUS.models[0]!, present: false }],
    });
    mount();
    await waitFor(() => expect(screen.getByTestId("ready").textContent).toBe("no_model"));
    await waitFor(() => expect(screen.getByTestId("jobs").textContent).toContain("audio-1"));
    expect(whisperTranscribe).not.toHaveBeenCalled();
  });
});

describe("in local mode", () => {
  it("asks the plane nothing at all", async () => {
    backendSettings.mockResolvedValue(LOCAL);
    mount();
    await waitFor(() => expect(whisperStatus).toHaveBeenCalled());
    expect(runsList).not.toHaveBeenCalled();
    expect(screen.getByTestId("jobs").textContent).toBe("");
  });
});
