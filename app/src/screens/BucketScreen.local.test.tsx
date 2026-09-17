/**
 * The Bucket screen with a machine that can transcribe.
 *
 * The engine is chosen here, before quoting, and that is the decision under
 * test: the choice has to reach the probe, because each run is quoted for the
 * engine it is on — a gate that said $1.90 over a run then transcribed for
 * nothing would be a receipt for work nobody did.
 *
 * The other half is what the screen tells a person before they choose. Amazon
 * quotes dollars; this quotes *hours*, from the speed this machine last
 * measured rather than from the model's name, and it says plainly when there
 * is no measurement yet. Nobody can trade the two without both figures.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { BackendProvider } from "../lib/backend";
import { BucketsProvider } from "../lib/buckets";
import { LibrariesProvider } from "../lib/libraries";
import * as transcriber from "../lib/localTranscriber";
import { BucketScreen } from "./BucketScreen";

const { buckets, bucketDetail, bucketProbe, audioGate, libraries } = vi.hoisted(() => ({
  buckets: vi.fn(),
  bucketDetail: vi.fn(),
  bucketProbe: vi.fn(),
  audioGate: vi.fn(),
  libraries: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, buckets, bucketDetail, bucketProbe, audioGate, libraries },
  };
});

const BUCKET_ID = "ce2d1a0695b0";
const LIBRARY = `lib_s3_${BUCKET_ID}`;
const t = (key: string, o?: Record<string, unknown>): string => i18n.t(key, o ?? {});
const button = (c: HTMLElement, key: string, o?: Record<string, unknown>) =>
  Array.from(c.querySelectorAll("button")).find((b) => b.textContent === t(key, o)) as
    | HTMLButtonElement
    | undefined;

const stored = {
  bucketId: BUCKET_ID,
  source: {
    bucket: "tenant-bucket", prefix: "audios/", role_arn: "", region: "",
    archive_prefix: "transcripciones/", manifest_key: "", manifest_map: {}, language: "es-US",
  },
  libraryId: LIBRARY, libraryName: "s3://tenant-bucket/audios/", syncedAt: "2026-09-16T00:00:00+00:00",
  objectCount: 2, complete: true, estimated: 0, manifestRows: 0, unmatchedRows: 0,
  unmatchedObjects: 0, warnings: [],
};

const object = (key: string, over: Record<string, unknown> = {}) => ({
  key, etag: "e1", size: 1, lastModified: "", container: "mp3", durationS: 3600,
  durationEstimated: false, title: key, author: "", recordedAt: "", publishedAt: "",
  source: "", url: "", available: true, warnings: [], documentId: null,
  activeVersionId: null, runId: null, runState: null, state: "unindexed", ...over,
});

const READY: ReturnType<typeof transcriber.useLocalTranscriber> = {
  status: {
    installed: true, binary: "/opt/whisper-cli", error: null, backend: "cuda",
    device: { backend: "cuda", device: "CUDA0", how: "run", model: "large-v3-turbo", at: "0" },
    canProbe: true,
    models: [{ name: "large-v3-turbo", file: "f", bytes: 1, note: "", present: true, path: "p" }],
    defaultModel: "large-v3-turbo", modelsDir: "/m", measuredSpeed: 1.8,
    measuredOn: "large-v3-turbo · cuda", decodable: ["mp3", "wav", "flac", "ogg"],
  },
  model: "large-v3-turbo", setModel: () => {}, readiness: "ready", jobs: [], current: null,
  attempt: null, history: [], paused: false, setPaused: () => {},
  probe: async () => false, probing: false, download: async () => false,
  downloading: null, giveUp: async () => false, hoursLeft: null, refresh: async () => {},
  error: null,
};

const mount = () =>
  render(
    <BackendProvider>
      <LibrariesProvider>
        <BucketsProvider>
          <BucketScreen />
        </BucketsProvider>
      </LibrariesProvider>
    </BackendProvider>,
  );

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  libraries.mockResolvedValue({ libraries: [] });
  buckets.mockResolvedValue({ buckets: [stored] });
  bucketDetail.mockResolvedValue({
    bucket: stored,
    // One the decoder reads and one it does not: four of the first corpus's
    // 147 recordings are M4A.
    objects: [object("audios/a.mp3"), object("audios/b.m4a", { container: "mp4" })],
    totals: { objects: 2, seconds: 7200, indexed: 0, pending: 0, unindexed: 2 },
  });
  audioGate.mockResolvedValue(null);
  bucketProbe.mockResolvedValue({ started: [], failed: [] });
  vi.spyOn(transcriber, "useLocalTranscriber").mockReturnValue(READY);
});

it("sends the chosen engine with the probe and prices the choice in hours", async () => {
  const { container } = mount();
  await waitFor(() => expect(container.querySelectorAll("tbody tr").length).toBe(2));
  fireEvent.click(container.querySelector("#bucket-engine-local")!);

  // Two hours of audio at the 1.8x this machine measured. Not from the model's
  // name and not from the backend: a desktop 4060 and a laptop's integrated
  // chip both report `cuda` and are an order of magnitude apart.
  fireEvent.click(button(container, "bucket.pickAll", { count: 2 })!);
  await waitFor(() =>
    expect(container.textContent).toContain(
      t("bucket.localHours", { hours: "1.1", speed: "1.8x" }),
    ),
  );
  // And what will go to Amazon whatever is ticked here, said before the button
  // rather than afterwards in a per-key line of the answer.
  expect(container.textContent).toContain(t("bucket.localUndecodable", { count: 1 }));

  fireEvent.click(button(container, "bucket.quoteStart", { count: 2 })!);
  await waitFor(() => expect(bucketProbe).toHaveBeenCalledTimes(1));
  expect(bucketProbe.mock.calls[0]![4]).toBe("local");
});

it("says there is no measurement yet rather than guessing one", async () => {
  vi.spyOn(transcriber, "useLocalTranscriber").mockReturnValue({
    ...READY,
    status: { ...READY.status!, measuredSpeed: null, measuredOn: null },
  });
  const { container } = mount();
  await waitFor(() => expect(container.querySelectorAll("tbody tr").length).toBe(2));
  fireEvent.click(container.querySelector("#bucket-engine-local")!);
  expect(container.textContent).toContain(t("bucket.localUnmeasured"));
});
