/**
 * A video's gate, which differs from a document's in the one place that matters.
 *
 * `cleanup` by hand, as everywhere else here.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "./i18n";
import type { StageOptions, VideoGateReport } from "./lib/api";
import { DEFAULT_STAGES } from "./lib/api";
import { VideoGateReview, duration } from "./VideoGateReview";

afterEach(cleanup);
beforeEach(async () => {
  await i18n.changeLanguage("es");
});

const t = (k: string, o?: Record<string, unknown>): string => i18n.t(k, o ?? {});

const estimate = (withTranscription: boolean) => ({
  stages: [
    ...(withTranscription
      ? [
          {
            stage: "transcription",
            model: "aws-transcribe-batch",
            inputTokens: 0,
            outputTokens: 0,
            usd: 0.72,
            outputTokensHigh: 0,
            usdHigh: 0.72,
          },
        ]
      : []),
    {
      stage: "embedding",
      model: "gemini-embedding-2",
      inputTokens: 4000,
      outputTokens: 0,
      usd: 0.0004,
      outputTokensHigh: 0,
      usdHigh: 0.0004,
    },
  ],
  totalUsd: withTranscription ? 0.7204 : 0.0004,
  totalUsdHigh: null,
  priceSource: "published rates over measured counts",
  unpricedStages: [],
});

const report = (kind: "manual" | "auto" | null): VideoGateReport => ({
  runId: "video-1",
  documentId: "doc_v",
  versionId: "ver_v",
  probe: {
    videoId: "dQw4w9WgXcQ",
    canonicalUrl: "https://youtu.be/dQw4w9WgXcQ",
    sourceKey: "youtube/dQw4w9WgXcQ",
    title: "Charla sobre hermenéutica",
    channel: "Canal",
    durationS: 1800,
    uploadDate: "20260101",
    tracks: [],
    chosen: kind ? { language: "es", kind, ext: "vtt", name: "" } : null,
    warnings: [],
  },
  estimate: estimate(kind === null) as never,
  preview:
    kind === null
      ? null
      : ({
          chunkCount: 13,
          characters: 16000,
          kinds: [{ kind: "transcripcion", count: 13 }],
          chunksAreFinal: false,
          warnings: [],
        } as never),
  transcript: null,
  warnings: [],
  recommended: { correct: kind === "auto", embed: true },
});

const renderGate = (kind: "manual" | "auto" | null) => {
  const onDecide = vi.fn();
  const view = render(
    <VideoGateReview
      report={report(kind)}
      stages={DEFAULT_STAGES}
      busy={false}
      onDecide={onDecide}
    />,
  );
  return { ...view, onDecide };
};

const approve = (c: HTMLElement) =>
  Array.from(c.querySelectorAll("button")).find(
    (b) => b.textContent === t("gate.approve"),
  ) as HTMLButtonElement;
const boxes = (c: HTMLElement) =>
  Array.from(c.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));

describe("the video gate", () => {
  it("says there is nothing to preview when there are no captions", () => {
    const { container } = renderGate(null);
    expect(container.textContent).toContain(t("gate.video.noPreview"));
    // And no fabricated chunk count beside it.
    expect(container.textContent).not.toContain(t("gate.chunks"));
  });

  it("shows the real counts when captions made a preview free", () => {
    const { container } = renderGate("manual");
    expect(container.textContent).toContain("13");
    expect(container.textContent).not.toContain(t("gate.video.noPreview"));
  });

  it("renders a stage that bills no tokens as a dash, not as two zeroes", () => {
    // Amazon Transcribe bills seconds of audio. Zero tokens is true, and a pair
    // of zeroes would read as a stage about to do nothing.
    const row = renderGate(null)
      .container.querySelectorAll("tbody tr")[0] as HTMLTableRowElement;
    const cells = Array.from(row.querySelectorAll("td")).map((c) => c.textContent);
    expect(cells[0]).toBe(t("gate.stages.transcription"));
    expect(cells[2]).toBe("—");
    expect(cells[3]).toBe("—");
  });

  it("starts correction on for automatic captions and off for a manual track", () => {
    const auto = renderGate("auto");
    expect(boxes(auto.container)[0]?.checked).toBe(true);
    cleanup();
    const manual = renderGate("manual");
    expect(boxes(manual.container)[0]?.checked).toBe(false);
  });

  it("offers only the two switches this workflow has a stage for", () => {
    const { container } = renderGate("auto");
    expect(boxes(container)).toHaveLength(2);
  });

  it("never approves a stage the video workflow cannot run", () => {
    // The document defaults have semantics, profile learning and the eval set
    // on. Passing those through would quote work that has no stage at all.
    const { container, onDecide } = renderGate("auto");
    fireEvent.click(approve(container));
    const options = onDecide.mock.calls[0]?.[1] as StageOptions;
    expect(options.extractSemantics).toBe(false);
    expect(options.learnProfile).toBe(false);
    expect(options.generateEvalset).toBe(false);
    expect(options.tune).toBe(false);
    expect(options.correct).toBe(true);
    expect(options.embed).toBe(true);
  });

  it("sends what the person ticked, not what was recommended", () => {
    const { container, onDecide } = renderGate("auto");
    fireEvent.click(boxes(container)[0] as HTMLInputElement);
    fireEvent.click(approve(container));
    expect((onDecide.mock.calls[0]?.[1] as StageOptions).correct).toBe(false);
  });

  it("formats a duration the way the locator does", () => {
    expect(duration(0)).toBe("0:00");
    expect(duration(754)).toBe("12:34");
    expect(duration(3754)).toBe("1:02:34");
  });

  it("explains the correction default in the tense each source deserves", () => {
    // Three cases, not two. Found by looking at a screenshot: the Transcribe
    // card said "this transcript already comes punctuated" about a transcript
    // that does not exist yet — present tense about something that has not
    // happened, printed at the moment somebody decides whether to pay for it.
    const said = (kind: "manual" | "auto" | null) => {
      const { container } = renderGate(kind);
      const text = container.textContent ?? "";
      cleanup();
      return text;
    };
    expect(said("auto")).toContain(t("gate.video.correctAuto"));
    expect(said("manual")).toContain(t("gate.video.correctManual"));
    expect(said(null)).toContain(t("gate.video.correctTranscribe"));
    // And the three genuinely differ, or the branch is decoration.
    const all = [t("gate.video.correctAuto"), t("gate.video.correctManual"),
                 t("gate.video.correctTranscribe")];
    expect(new Set(all).size).toBe(3);
  });

  it("does not repeat, as a warning, what the panel already says twice", () => {
    // `probe.chosen === null` is stated on the transcript line and again in the
    // no-preview paragraph. A third copy, phrased more weakly, reads as a
    // separate problem.
    const { container } = renderGate(null);
    expect(container.textContent).not.toContain("no tiene subtítulos");
  });
});
