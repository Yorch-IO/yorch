/**
 * The Bucket screen against a faked plane.
 *
 * What is measured: that local mode renders an explanation and not a form;
 * that registering sends the source the person typed with the manifest
 * mapping intact; that the table derives its rows from the plane's payload;
 * that a quote sends exactly the ticked, quotable keys; and that an approval
 * answers each parked gate with the gate's own recommendation — the property
 * that keeps the number spent the number quoted.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { BackendProvider } from "../lib/backend";
import { BucketPicker, BucketsProvider } from "../lib/buckets";
import { LibrariesProvider } from "../lib/libraries";
import { BucketScreen } from "./BucketScreen";

const {
  buckets, bucketDetail, bucketRegister, bucketSync, bucketProbe, bucketForget,
  audioGate, ingestApprove, libraries,
} = vi.hoisted(() => ({
  buckets: vi.fn(),
  bucketDetail: vi.fn(),
  bucketRegister: vi.fn(),
  bucketSync: vi.fn(),
  bucketProbe: vi.fn(),
  bucketForget: vi.fn(),
  audioGate: vi.fn(),
  ingestApprove: vi.fn(),
  libraries: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      buckets, bucketDetail, bucketRegister, bucketSync, bucketProbe, bucketForget,
      audioGate, ingestApprove, libraries,
    },
  };
});

function mount(withBar = false) {
  return render(
    <BackendProvider>
      <LibrariesProvider>
        <BucketsProvider>
          {withBar && <BucketPicker />}
          <BucketScreen />
        </BucketsProvider>
      </LibrariesProvider>
    </BackendProvider>,
  );
}

afterEach(cleanup);

const BUCKET_ID = "ce2d1a0695b0";
const LIBRARY = `lib_s3_${BUCKET_ID}`;
const t = (key: string, o?: Record<string, unknown>): string => i18n.t(key, o ?? {});
const button = (c: HTMLElement, key: string, o?: Record<string, unknown>) =>
  Array.from(c.querySelectorAll("button")).find((b) => b.textContent === t(key, o)) as
    | HTMLButtonElement
    | undefined;

const SOURCE = {
  bucket: "tenant-bucket", prefix: "audios/", role_arn: "arn:aws:iam::123456789012:role/reader",
  region: "", archive_prefix: "transcripciones/", manifest_key: "metadatos/m.csv",
  manifest_map: { file: "archivo", title: "titulo" }, language: "es-US",
};

const stored = {
  bucketId: BUCKET_ID, source: SOURCE, libraryId: LIBRARY, libraryName: "s3://tenant-bucket/audios/",
  syncedAt: "2026-09-16T00:00:00+00:00", objectCount: 3, complete: true, estimated: 0,
  manifestRows: 3, unmatchedRows: 0, unmatchedObjects: 0, warnings: [],
};

function object(key: string, over: Record<string, unknown> = {}) {
  return {
    key, etag: "e1", size: 34_000_000, lastModified: "", container: "mp3", durationS: 3600,
    durationEstimated: false, title: key.split("/").pop(), author: "", recordedAt: "1995-04-02",
    publishedAt: "", source: "iVoox", url: "", available: true, warnings: [],
    documentId: null, activeVersionId: null, runId: null, runState: null, state: "unindexed",
    ...over,
  };
}

const detail = {
  bucket: stored,
  objects: [
    object("audios/a.mp3"),
    object("audios/b.mp3", { state: "pending", runId: "audio-b", runState: "awaiting_approval", documentId: "doc_b" }),
    object("audios/c.mp3", { state: "indexed", activeVersionId: "ver_c", documentId: "doc_c" }),
  ],
  totals: { objects: 3, seconds: 10800, indexed: 1, pending: 1, unindexed: 1 },
};

const gateB = {
  runId: "audio-b", documentId: "doc_b", versionId: "ver_b",
  probe: { videoId: "audios/b.mp3", title: "b" },
  estimate: {
    stages: [{ stage: "transcription", model: "aws-transcribe-batch", inputTokens: 0,
               outputTokens: 0, usd: 1.44, outputTokensHigh: 0, usdHigh: 1.44 }],
    totalUsd: 1.44, totalUsdHigh: 1.44, priceSource: "published", unpricedStages: [],
  },
  preview: null, transcript: null, warnings: [],
  recommended: { correct: false, embed: true, extractSemantics: false, generateEvalset: false,
                 learnProfile: false, ignoreProfile: false, reviewCorrection: false, tune: false,
                 buildEpub: false, condenseDescriptions: false },
};

beforeEach(() => {
  vi.clearAllMocks();
  libraries.mockResolvedValue({ libraries: [] });
  buckets.mockResolvedValue({ buckets: [stored] });
  bucketDetail.mockResolvedValue(detail);
  audioGate.mockResolvedValue(null);
  ingestApprove.mockResolvedValue(undefined);
});

describe("on a plane that serves no bucket route", () => {
  it("says so instead of rendering a form or a red panel", async () => {
    buckets.mockRejectedValue({
      kind: "control_status",
      message: "the control API returned 404: {\"detail\":\"Not Found\"}",
    });
    const { container } = mount();
    await waitFor(() => expect(container.textContent).toContain(t("bucket.paidOnly")));
    expect(container.querySelector("form")).toBeNull();
    expect(container.querySelector(".error")).toBeNull();
  });
});

describe("registering", () => {
  it("shows the form when nothing is registered and sends what was typed, mapping and all", async () => {
    buckets.mockResolvedValueOnce({ buckets: [] });
    bucketRegister.mockResolvedValue({
      bucket: stored,
      synced: { bucketId: BUCKET_ID, libraryId: LIBRARY, objects: 3, added: 3, changed: 0, absent: 0,
                estimated: 0, manifestRows: 3, unmatchedRows: 0, unmatchedObjects: 0, warnings: [] },
    });
    const { container } = mount(true);
    await waitFor(() => expect(container.querySelector("form.bucket-register")).not.toBeNull());
    const field = (label: string) =>
      Array.from(container.querySelectorAll("label")).find((l) => l.textContent?.startsWith(label))
        ?.querySelector("input,select") as HTMLInputElement;
    fireEvent.change(field(t("bucket.bucket")), { target: { value: "tenant-bucket" } });
    fireEvent.change(field(t("bucket.prefix")), { target: { value: "audios/" } });
    fireEvent.change(field(t("bucket.roleArn")), { target: { value: SOURCE.role_arn } });
    fireEvent.change(field(t("bucket.manifestKey")), { target: { value: "metadatos/m.csv" } });
    fireEvent.change(field(t("bucket.manifestField.file")), { target: { value: "archivo" } });
    fireEvent.change(field(t("bucket.manifestField.title")), { target: { value: "titulo" } });
    fireEvent.click(button(container, "bucket.register")!);
    await waitFor(() => expect(bucketRegister).toHaveBeenCalledTimes(1));
    expect(bucketRegister.mock.calls[0]![0]).toEqual(SOURCE);
    // Registered and selected: the table replaces the form.
    await waitFor(() => expect(container.querySelector("table.candidates")).not.toBeNull());
    expect(container.textContent).toContain(t("bucket.synced", { objects: 3, added: 3, changed: 0 }));
  });
});

describe("the table", () => {
  it("renders every object with its state, and only the quotable ones can be ticked", async () => {
    const { container } = mount();
    await waitFor(() => expect(container.querySelectorAll("table.candidates tbody tr").length).toBe(3));
    const boxes = Array.from(container.querySelectorAll("tbody input[type=checkbox]")) as HTMLInputElement[];
    // a: unindexed, tickable. b: pending, tickable once its gate parks. c: indexed, not.
    expect(boxes.map((b) => b.disabled)).toEqual([false, true, true]);
    expect(container.textContent).toContain(t("bucket.stateIndexed", { count: 1 }));
    expect(container.textContent).toContain(t("bucket.minutes", { count: 180 }));
  });

  it("quotes exactly the ticked, quotable keys with the stages chosen before quoting", async () => {
    bucketProbe.mockResolvedValue({ started: [{ key: "audios/a.mp3", workflowId: "audio-a" }], failed: [] });
    const { container } = mount();
    await waitFor(() => expect(container.querySelectorAll("tbody tr").length).toBe(3));
    fireEvent.click(container.querySelector("#bucket-semantics")!);
    fireEvent.click(button(container, "bucket.pickAll", { count: 1 })!);
    fireEvent.click(button(container, "bucket.quoteStart", { count: 1 })!);
    await waitFor(() => expect(bucketProbe).toHaveBeenCalledTimes(1));
    const [bucketId, keys, options, reindex, transcriber] = bucketProbe.mock.calls[0]!;
    expect(bucketId).toBe(BUCKET_ID);
    expect(keys).toEqual(["audios/a.mp3"]);
    expect(options.extractSemantics).toBe(true);
    expect(options.learnProfile).toBe(false);
    expect(reindex).toBe(false);
    // Amazon unless somebody picks otherwise, and the engine travels with the
    // *probe*: each gate quotes the engine its own run is on, so it cannot be
    // decided later at the approval.
    expect(transcriber).toBe("transcribe");
    await waitFor(() => expect(container.textContent).toContain(t("bucket.quoted", { count: 1 })));
  });

  it("offers this machine only when it can actually transcribe, and says what is missing", async () => {
    const { container } = mount();
    await waitFor(() => expect(container.querySelectorAll("tbody tr").length).toBe(3));
    const local = container.querySelector("#bucket-engine-local") as HTMLInputElement;
    const amazon = container.querySelector("#bucket-engine-transcribe") as HTMLInputElement;
    // Rendered outside the provider, which is the state of a build with no
    // sidecar: the option is visible and refused rather than hidden, because a
    // control that disappears teaches nobody that the feature exists.
    expect(amazon.checked).toBe(true);
    expect(local.disabled).toBe(true);
    expect(container.textContent).toContain(t("bucket.local.unknown"));
  });

  it("reads a parked gate, shows its price, and approves it with the gate's recommendation", async () => {
    audioGate.mockImplementation(async (id: string) => (id === "audio-b" ? gateB : null));
    const { container } = mount();
    await waitFor(() => expect(container.textContent).toContain("$1.44"));
    // b is now tickable — its gate parked — and the sum follows the tick.
    const boxes = Array.from(container.querySelectorAll("tbody input[type=checkbox]")) as HTMLInputElement[];
    expect(boxes[1]!.disabled).toBe(false);
    fireEvent.click(boxes[1]!);
    await waitFor(() => expect(button(container, "bucket.approveStart", { count: 1 })).toBeDefined());
    fireEvent.click(button(container, "bucket.approveStart", { count: 1 })!);
    await waitFor(() => expect(ingestApprove).toHaveBeenCalledTimes(1));
    const [runId, approval] = ingestApprove.mock.calls[0]!;
    expect(runId).toBe("audio-b");
    expect(approval.approved).toBe(true);
    // The gate decided correction; the person decided nothing else here.
    expect(approval.options.correct).toBe(false);
    expect(approval.options.embed).toBe(true);
    expect(approval.options.extractSemantics).toBe(false);
  });

  it("filters by state without losing the totals of the whole table", async () => {
    const { container } = mount();
    await waitFor(() => expect(container.querySelectorAll("tbody tr").length).toBe(3));
    const state = container.querySelector(`select[aria-label="${t("bucket.filterState")}"]`)!;
    fireEvent.change(state, { target: { value: "indexed" } });
    await waitFor(() => expect(container.querySelectorAll("tbody tr").length).toBe(1));
    expect(container.textContent).toContain(t("bucket.filtered", { shown: 1, total: 3 }));
  });
});
