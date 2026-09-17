/**
 * The decisions behind transcribing here, asserted on the numbers.
 *
 * Same split as `bucket.test.ts`: the provider holds the polling and the
 * calls, and everything a person could get wrong — which run goes next, what
 * "ready" means, how many hours a batch is — is a function with an answer.
 */
import { describe, expect, it } from "vitest";

import type { BucketObjectRow, RunListItem, WhisperStatus } from "./api";
import {
  AWAITING,
  MAX_ATTEMPTS,
  chosenModel,
  hoursFor,
  languageOf,
  nextJob,
  percent,
  readiness,
  rowsByRun,
  speedLabel,
  toJob,
  waitingRuns,
  type Job,
} from "./transcribing";

const status = (over: Partial<WhisperStatus> = {}): WhisperStatus => ({
  installed: true,
  binary: "/opt/whisper-cli",
  error: null,
  backend: "cuda",
  models: [
    { name: "large-v3-turbo", file: "f", bytes: 1, note: "", present: true, path: "p" },
    { name: "base", file: "b", bytes: 1, note: "", present: false, path: "q" },
  ],
  device: null,
  canProbe: true,
  defaultModel: "large-v3-turbo",
  modelsDir: "/models",
  measuredSpeed: null,
  measuredOn: null,
  decodable: ["mp3", "wav", "flac", "ogg"],
  ...over,
});

const run = (over: Partial<RunListItem> = {}): RunListItem => ({
  id: "r", workflowId: "audio-1", kind: "audio", state: AWAITING, stage: "transcribing",
  startedAt: "2026-09-17T10:00:00Z", finishedAt: null, errorKind: null, errorDetail: null,
  title: "Una prédica", libraryId: "lib_s3_abc", label: null, documentId: "doc_1",
  versionId: null, usdSoFar: 0,
  ...over,
});

const object = (over: Partial<BucketObjectRow> = {}): BucketObjectRow => ({
  key: "audios/a.mp3", etag: "e", size: 1, lastModified: "", container: "mp3",
  durationS: 720, durationEstimated: false, title: "A", author: "", recordedAt: "",
  publishedAt: "", source: "", url: "", available: true, warnings: [], documentId: "doc_1",
  activeVersionId: null, runId: "audio-1", runState: AWAITING, state: "pending",
  ...over,
});

const job = (over: Partial<Job> = {}): Job => ({
  workflowId: "audio-1", libraryId: "lib_s3_abc", documentId: "doc_1", title: "A",
  container: "mp3", durationS: 720, language: "es",
  ...over,
});

describe("what this machine can do", () => {
  it("tells a missing binary from a missing model, because they need different buttons", () => {
    expect(readiness(null, "large-v3-turbo")).toBe("unknown");
    expect(readiness(status({ installed: false, binary: null }), "large-v3-turbo")).toBe("no_binary");
    expect(readiness(status(), "base")).toBe("no_model");
    expect(readiness(status(), "large-v3-turbo")).toBe("ready");
  });

  it("falls back to the build's default when the remembered model is not one it has", () => {
    expect(chosenModel(status(), "base")).toBe("base");
    // A bundle downgraded under a remembered preference must not leave the
    // queue pointing at a model this build has never heard of.
    expect(chosenModel(status(), "enormous-v9")).toBe("large-v3-turbo");
    expect(chosenModel(status(), null)).toBe("large-v3-turbo");
  });

  it("hands whisper a bare language, because Transcribe's region code is refused", () => {
    expect(languageOf("es-US")).toBe("es");
    expect(languageOf("en_GB")).toBe("en");
    expect(languageOf("")).toBe("es");
  });
});

describe("the queue", () => {
  it("takes only runs parked for this machine, oldest first", () => {
    const rows = [
      run({ workflowId: "audio-2", startedAt: "2026-09-17T12:00:00Z" }),
      run({ workflowId: "audio-1", startedAt: "2026-09-17T09:00:00Z" }),
      run({ workflowId: "audio-3", state: "awaiting_approval" }),
      run({ workflowId: "audio-4", finishedAt: "2026-09-17T13:00:00Z" }),
      // A run that never registered a document has nothing to presign against.
      run({ workflowId: "audio-5", documentId: null }),
    ];
    expect(waitingRuns(rows).map((r) => r.workflowId)).toEqual(["audio-1", "audio-2"]);
  });

  it("skips a run it has already failed at twice, and one it cannot decode", () => {
    const jobs = [job({ workflowId: "audio-1" }), job({ workflowId: "audio-2" })];
    const decodable = ["mp3", "wav", "flac", "ogg"];
    expect(nextJob(jobs, new Map(), decodable)?.workflowId).toBe("audio-1");
    expect(
      nextJob(jobs, new Map([["audio-1", MAX_ATTEMPTS]]), decodable)?.workflowId,
    ).toBe("audio-2");
    expect(
      nextJob(jobs, new Map([["audio-1", MAX_ATTEMPTS], ["audio-2", MAX_ATTEMPTS]]), decodable),
    ).toBeNull();
    // An M4A here means the plane started locally something `transcriberFor`
    // refuses, which is a disagreement worth leaving visible rather than
    // burning two attempts on.
    expect(nextJob([job({ container: "mp4" })], new Map(), decodable)).toBeNull();
  });

  it("joins a run to the catalogue row that knows its container and duration", () => {
    const rows = rowsByRun([object(), object({ key: "audios/b.mp3", runId: null })]);
    expect(rows.size).toBe(1);
    const joined = toJob(run(), rows, "es")!;
    expect(joined).toEqual(job({ title: "A" }));
    // A bucket forgotten while its runs were parked: the run is still real and
    // still has a presignable document; it loses only the container check the
    // plane already made at quote time.
    const orphan = toJob(run(), new Map(), "es")!;
    expect(orphan.container).toBe("");
    expect(orphan.title).toBe("Una prédica");
    expect(toJob(run({ libraryId: null }), rows, "es")).toBeNull();
  });
});

describe("the figures on the screen", () => {
  it("reports hours only when a speed has been measured", () => {
    // 147 recordings, 9,704 minutes, at 1.8x real time.
    expect(hoursFor(9704 * 60, 1.8)).toBeCloseTo(89.85, 1);
    expect(hoursFor(9704 * 60, null)).toBeNull();
    expect(hoursFor(0, 1.8)).toBeNull();
    // A machine slower than real time is a real answer, not a refusal.
    expect(hoursFor(3600, 0.5)).toBeCloseTo(2, 5);
  });

  it("renders a speed a person can read, and nothing at all when there is none", () => {
    expect(speedLabel(1.84)).toBe("1.8x");
    expect(speedLabel(12.4)).toBe("12x");
    expect(speedLabel(null)).toBe("");
  });

  it("clamps progress rather than dividing by a total nobody sent", () => {
    expect(percent({ phase: "transcribe", done: 12, total: 100 })).toBe(12);
    expect(percent({ phase: "audio", done: 5, total: 0 })).toBe(0);
    expect(percent({ phase: "audio", done: 500, total: 100 })).toBe(100);
    expect(percent(null)).toBe(0);
  });
});
