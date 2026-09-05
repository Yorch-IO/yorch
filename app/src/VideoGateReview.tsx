import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { StageOptions, VideoGateReport } from "./lib/api";
import { EstimateTable } from "./GateReview";

/** `12:34` under an hour, `1:02:34` over it. Mirrors `videosource.hhmmss`. */
export function duration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/**
 * A video's approval gate.
 *
 * Its own component rather than a branch inside `GateReview`, because the two
 * reports differ in the one place that matters: a video with no captions has
 * **no preview at all**. There is no text to chunk until Amazon has been paid,
 * and saying so is the whole point of stopping here — a fabricated chunk count
 * would be a number nobody measured, presented at the moment somebody decides
 * whether to spend.
 *
 * Two switches, not seven. A video run has no profile, semantics, eval-set or
 * tuning stage at all, so offering those would quote work that cannot happen.
 * They open on what the probe recommends: correction is worth it for automatic
 * captions, which arrive with no punctuation, and not for a manual track or for
 * Amazon's own output, which are already punctuated.
 */
export function VideoGateReview({
  report,
  stages,
  busy,
  onDecide,
}: {
  report: VideoGateReport;
  stages: StageOptions;
  busy: boolean;
  onDecide: (approved: boolean, options: StageOptions) => void;
}) {
  const { t } = useTranslation();
  const [correct, setCorrect] = useState(report.recommended?.correct ?? false);
  const [embed, setEmbed] = useState(report.recommended?.embed ?? true);

  const { probe } = report;
  const source = probe.chosen
    ? t(`gate.video.captions.${probe.chosen.kind}`, {
        defaultValue: probe.chosen.kind,
        language: probe.chosen.language,
      })
    : t("gate.video.transcribe");

  const decide = (approved: boolean) =>
    // Everything this workflow has no stage for stays off, whatever the
    // document defaults say: the gate must not quote work that cannot run.
    onDecide(approved, {
      ...stages,
      correct,
      embed,
      extractSemantics: false,
      learnProfile: false,
      generateEvalset: false,
      tune: false,
    });

  return (
    <div className="gate">
      <h3>{t("gate.video.title")}</h3>
      <p className="intro">{t("gate.video.intro")}</p>

      <dl className="preview">
        <dt>{t("gate.video.video")}</dt>
        <dd>
          {probe.title}
          {probe.channel ? ` · ${probe.channel}` : ""}
        </dd>
        <dt>{t("gate.video.duration")}</dt>
        <dd>{duration(probe.durationS)}</dd>
        <dt>{t("gate.video.source")}</dt>
        <dd>{source}</dd>
        {report.preview && (
          <>
            <dt>{t("gate.chunks")}</dt>
            <dd>{report.preview.chunkCount}</dd>
            <dt>{t("gate.characters")}</dt>
            <dd>{report.preview.characters.toLocaleString()}</dd>
          </>
        )}
      </dl>

      {/* Not a footnote, and not a zero. With no captions there is nothing to
          chunk until the transcription is paid for, so the figures above are
          projected from the video's duration through a speech rate that has
          not been measured yet. */}
      {!report.preview && <p className="warn">{t("gate.video.noPreview")}</p>}
      {report.preview && !report.preview.chunksAreFinal && (
        <p className="warn">{t("gate.notFinal")}</p>
      )}
      {report.warnings.map((w) => (
        <p className="warn" key={w}>
          {w}
        </p>
      ))}

      <fieldset className="stages">
        <legend>{t("import.stages")}</legend>
        <label>
          <input
            type="checkbox"
            checked={correct}
            onChange={() => setCorrect((c) => !c)}
          />
          <span>{t("import.stageCorrect")}</span>
        </label>
        <label>
          <input
            type="checkbox"
            checked={embed}
            onChange={() => setEmbed((c) => !c)}
          />
          <span>{t("import.stageEmbed")}</span>
        </label>
      </fieldset>
      {/* Why the box starts where it does. Three cases, not two: at this gate
          an Amazon transcript does not exist yet, so saying it "already comes
          punctuated" would be present tense about something that has not
          happened. Correction's value on a transcript is unmeasured either way,
          and it is the stage that dominates the bill. */}
      <p className="caveat">
        {t(
          probe.chosen === null
            ? "gate.video.correctTranscribe"
            : probe.chosen.kind === "auto"
              ? "gate.video.correctAuto"
              : "gate.video.correctManual",
        )}
      </p>

      <EstimateTable estimate={report.estimate} />

      <div className="actions">
        <button type="button" onClick={() => decide(true)} disabled={busy}>
          {t("gate.approve")}
        </button>
        <button type="button" onClick={() => decide(false)} disabled={busy}>
          {t("gate.reject")}
        </button>
      </div>
    </div>
  );
}
