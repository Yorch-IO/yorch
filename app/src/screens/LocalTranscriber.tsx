/**
 * The Services screen's panel for transcribing on this machine.
 *
 * It answers three questions and nothing else: **can** this machine do it (a
 * binary, a model), **how fast** does it actually go, and **what is it doing
 * now**. The third is the one that could not live on the Bucket screen: a
 * batch takes hours, the person will be elsewhere in the app, and a queue
 * whose only window is the screen that started it is a queue nobody watches.
 *
 * The speed is *measured*, never derived from the model's name or the
 * backend's: a desktop 4060 and a laptop's integrated chip both report `cuda`
 * and are an order of magnitude apart, so the only honest figure is the one
 * the last run produced. Until there is one the panel says so rather than
 * printing an estimate nobody can check.
 */
import { useTranslation } from "react-i18next";

import { errorGuidanceKey, errorMessage } from "../lib/api";
import { useLocalTranscriber } from "../lib/localTranscriber";
import { percent, speedLabel } from "../lib/transcribing";

/** Bytes as a person reads them. A model is gigabytes and a progress line in
 *  bytes is unreadable at that size. */
function gb(bytes: number): string {
  return `${(bytes / 1e9).toFixed(2)} GB`;
}

export function LocalTranscriber() {
  const { t } = useTranslation();
  const {
    status,
    model,
    setModel,
    readiness,
    jobs,
    current,
    attempt,
    history,
    paused,
    setPaused,
    probe,
    probing,
    download,
    downloading,
    hoursLeft,
    error,
  } = useLocalTranscriber();

  // What will actually run the model, and how much that answer is worth.
  // `device` is whisper.cpp's own word after loading a model; `backend` is
  // what the build links against. They differ exactly where it hurts — a
  // binary built with CUDA on a machine whose driver or card it cannot use
  // hints `cuda` and runs on the processor at a fraction of the speed — so
  // the two are rendered differently and never merged into one line.
  const device = status?.device ?? null;
  const running = device?.backend ?? status?.backend ?? "none";

  return (
    <>
      <h3>{t("whisper.title")}</h3>
      <p>{t("whisper.intro")}</p>

      {/* Two questions, answered at a glance and in this order: is the
          software here at all, and will it use the GPU or the processor. Both
          are badges rather than prose because they are the things a person
          checks rather than reads. */}
      {status && (
        <ul className="probes whisper-state">
          <li>
            <span className={status.installed ? "ok" : "bad"}>
              {status.installed ? "✓" : "✕"}
            </span>{" "}
            <strong>
              {t(status.installed ? "whisper.installed" : "whisper.notInstalled")}
            </strong>
            {status.binary && <span className="muted small"> · {status.binary}</span>}
          </li>
          {status.installed && (
            <li>
              <span className={device ? (device.backend === "cpu" ? "warn" : "ok") : "muted"}>
                {device ? (device.backend === "cpu" ? "▲" : "✓") : "?"}
              </span>{" "}
              <strong>
                {t("whisper.runsOn", {
                  backend: t(`whisper.backends.${running}`, { defaultValue: running }),
                })}
              </strong>{" "}
              {device ? (
                <span className="muted small">
                  {t(device.how === "run" ? "whisper.deviceFromRun" : "whisper.deviceFromProbe", {
                    device: device.device,
                    model: device.model,
                  })}
                </span>
              ) : (
                /* Until something has asked the binary, this is a guess read
                   off the build's dependencies — and it is the guess that is
                   wrong on exactly the machines where it matters. */
                <span className="muted small">{t("whisper.deviceGuess")}</span>
              )}
            </li>
          )}
          {status.installed && (
            <li>
              {status.measuredSpeed ? (
                <>
                  <span className="ok">✓</span>{" "}
                  {t("whisper.speed", { speed: speedLabel(status.measuredSpeed) })}
                  {status.measuredOn && <span className="muted"> · {status.measuredOn}</span>}
                </>
              ) : (
                <span className="muted">{t("whisper.speedUnknown")}</span>
              )}
            </li>
          )}
        </ul>
      )}

      {/* The binary is the one thing a person cannot fix from this screen, so
          the sentence with the command in it comes right after the badge. */}
      {status && !status.installed && (
        <p className="warn">{status.error ?? t("whisper.noBinary")}</p>
      )}

      {status?.installed && (
        <div className="actions">
          <button
            type="button"
            onClick={() => void probe()}
            disabled={probing || !status.canProbe}
          >
            {t(probing ? "whisper.probing" : "whisper.probe")}
          </button>
          {!status.canProbe && <span className="muted small">{t("whisper.probeNeedsModel")}</span>}
        </div>
      )}

      {status && status.models.length > 0 && (
        <label className="field">
          <span>{t("whisper.model")}</span>
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            {status.models.map((m) => (
              <option key={m.name} value={m.name}>
                {m.name} · {gb(m.bytes)}
                {m.present ? "" : ` · ${t("whisper.notDownloaded")}`}
              </option>
            ))}
          </select>
        </label>
      )}

      {status?.models
        .filter((m) => m.name === model)
        .map((m) => (
          <p key={m.name} className="muted small">
            {m.note}
          </p>
        ))}

      {readiness === "no_model" && (
        <div className="actions">
          <button
            type="button"
            onClick={() => void download(model)}
            disabled={downloading !== null}
          >
            {t("whisper.download", { model })}
          </button>
        </div>
      )}

      {downloading && (
        <p className="muted small">
          <progress value={downloading.done} max={downloading.total || 1} />{" "}
          {t("whisper.downloading", {
            model: downloading.name,
            done: gb(downloading.done),
            total: gb(downloading.total),
          })}
        </p>
      )}

      {/* The queue itself. Absent rather than empty when there is nothing
          parked: a panel that always shows "0 waiting" trains a reader to skip
          the place the real number appears. */}
      {(jobs.length > 0 || current) && (
        <>
          <p>
            {t("whisper.waiting", { count: jobs.length })}
            {hoursLeft !== null && ` · ${t("whisper.hoursLeft", { hours: hoursLeft.toFixed(1) })}`}
          </p>
          <div className="actions">
            <button type="button" onClick={() => setPaused(!paused)}>
              {t(paused ? "whisper.resume" : "whisper.pause")}
            </button>
          </div>
        </>
      )}

      {current && (
        <p className="muted small">
          <progress value={percent(attempt)} max={100} />{" "}
          {t(`whisper.phase.${attempt?.phase ?? "audio"}`)} — {current.title}
        </p>
      )}

      {paused && jobs.length > 0 && !current && (
        <p className="muted small">{t("whisper.paused")}</p>
      )}

      {history.length > 0 && (
        <ul className="probes">
          {history.slice(0, 5).map((h) => (
            <li key={`${h.workflowId}-${h.at}`}>
              <span className={h.ok ? "ok" : "bad"}>{h.ok ? "✓" : "✕"}</span> {h.title}
              {h.ok && h.result && (
                <span className="muted">
                  {" "}
                  ·{" "}
                  {t("whisper.took", {
                    minutes: (h.result.wallSeconds / 60).toFixed(1),
                    speed: speedLabel(
                      h.result.wallSeconds > 0
                        ? h.result.audioSeconds / h.result.wallSeconds
                        : null,
                    ),
                    segments: h.result.segments,
                  })}
                </span>
              )}
              {!h.ok && <span className="warn"> · {errorMessage(h.error)}</span>}
            </li>
          ))}
        </ul>
      )}

      {error !== null && (
        <p className="warn">
          {errorMessage(error)}
          {errorGuidanceKey(error) && ` — ${t(errorGuidanceKey(error) as string)}`}
        </p>
      )}
    </>
  );
}
