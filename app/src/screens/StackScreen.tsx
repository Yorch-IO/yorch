import { openUrl } from "@tauri-apps/plugin-opener";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  errorGuidanceKey,
  errorMessage,
  type DockerInfo,
  type Health,
  type ProviderSettings,
  type PingResult,
  type StackStatus,
} from "../lib/api";

type Busy = "idle" | "up" | "down" | "ping";

/** Docker's own install matrix, which covers all three target platforms. */
const DOCKER_INSTALL_URL = "https://docs.docker.com/engine/install/";

/**
 * Shows what failed and, where the error's kind implies a fix, what to do about
 * it. The technical message is always kept: guidance without the underlying
 * error is unactionable when the guidance turns out not to apply.
 */
function ErrorPanel({
  error,
  onDismiss,
}: {
  error: unknown;
  onDismiss: () => void;
}) {
  const { t } = useTranslation();
  const guidance = errorGuidanceKey(error);
  return (
    <div className="error">
      <strong>{t("error.title")}</strong>
      {guidance && <p>{t(guidance)}</p>}
      <pre className="detail">{errorMessage(error)}</pre>
      <button type="button" onClick={onDismiss}>
        {t("error.dismiss")}
      </button>
    </div>
  );
}

export function StackScreen() {
  const { t } = useTranslation();

  const [docker, setDocker] = useState<DockerInfo | null>(null);
  const [dockerError, setDockerError] = useState<string | null>(null);
  const [status, setStatus] = useState<StackStatus | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [provider, setProvider] = useState<ProviderSettings | null>(null);
  const [projectDraft, setProjectDraft] = useState("");
  /** Set once a project has been saved in this session. The containers keep the
   *  old value until they are recreated, and silently accepting a setting that
   *  has not taken effect is how a user concludes the feature is broken. */
  const [savedProject, setSavedProject] = useState(false);
  const [ping, setPing] = useState<PingResult | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  // Held as the raw thrown value, not a string: ErrorPanel needs the `kind` tag
  // to decide whether there is actionable guidance to offer.
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<Busy>("idle");
  const [logsFor, setLogsFor] = useState<string | null>(null);
  const [logs, setLogs] = useState<string>("");

  const checkDocker = useCallback(async () => {
    try {
      setDocker(await api.dockerProbe());
      setDockerError(null);
    } catch (e) {
      setDocker(null);
      setDockerError(errorMessage(e));
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.stackStatus());
      setError(null);
    } catch (e) {
      setError(e);
      return;
    }
    // The provider is read from `.env` by Rust, so unlike health it answers
    // whether or not the stack is running — which is the point: a user should
    // be able to name their project *before* bringing anything up.
    try {
      const settings = await api.providerSettings();
      setProvider(settings);
      setProjectDraft((draft) => (draft === "" ? settings.projectId : draft));
    } catch {
      setProvider(null);
    }
    // Health comes from the control API, which is only up once the stack is.
    // A failure here is expected before that and must not surface as an error.
    try {
      setHealth(await api.controlHealth());
    } catch {
      setHealth(null);
    }
  }, []);

  const saveProject = useCallback(async () => {
    setError(null);
    try {
      setProvider(await api.setProviderProject(projectDraft));
      setSavedProject(true);
    } catch (e) {
      setError(e);
    }
  }, [projectDraft]);

  useEffect(() => {
    void (async () => {
      await checkDocker();
      await refresh();
    })();
  }, [checkDocker, refresh]);

  async function start() {
    setBusy("up");
    setError(null);
    setProgress(null);
    try {
      const next = await api.stackUp((e) => setProgress(e.detail));
      setStatus(next);
      setHealth(await api.controlHealth().catch(() => null));
    } catch (e) {
      setError(e);
      // The stack may be half-up; show whatever compose actually created.
      void refresh();
    } finally {
      setBusy("idle");
      setProgress(null);
    }
  }

  async function stop() {
    setBusy("down");
    setError(null);
    try {
      setStatus(await api.stackDown());
      setHealth(null);
      setPing(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy("idle");
    }
  }

  async function runCheck() {
    setBusy("ping");
    setError(null);
    try {
      setPing(await api.controlPing());
    } catch (e) {
      setPing(null);
      setError(e);
    } finally {
      setBusy("idle");
    }
  }

  async function toggleLogs(service: string) {
    if (logsFor === service) {
      setLogsFor(null);
      return;
    }
    setLogsFor(service);
    try {
      setLogs(await api.stackLogs(service));
    } catch (e) {
      setLogs(errorMessage(e));
    }
  }

  if (dockerError !== null) {
    return (
      <section className="panel">
        <h2>{t("docker.missingTitle")}</h2>
        <p>{t("docker.missingBody")}</p>
        <pre className="detail">{dockerError}</pre>
        <div className="actions">
          <button type="button" onClick={() => void checkDocker()}>
            {t("docker.retry")}
          </button>
          <button
            type="button"
            onClick={() => void openUrl(DOCKER_INSTALL_URL)}
          >
            {t("docker.install")}
          </button>
        </div>
      </section>
    );
  }

  const running = status?.services.some((s) => s.state === "running") ?? false;

  return (
    <section className="panel">
      <h2>{t("stack.title")}</h2>
      <p>{t("stack.intro")}</p>

      <p className="muted">
        {docker
          ? t("docker.found", {
              docker: docker.dockerVersion,
              compose: docker.composeVersion,
            })
          : t("docker.checking")}
      </p>

      {status?.devMode && <p className="badge">{t("stack.devMode")}</p>}

      <div className="actions">
        <button
          type="button"
          onClick={() => void start()}
          disabled={busy !== "idle"}
        >
          {busy === "up" ? t("stack.starting") : t("stack.start")}
        </button>
        <button
          type="button"
          onClick={() => void stop()}
          disabled={busy !== "idle" || !running}
        >
          {busy === "down" ? t("stack.stopping") : t("stack.stop")}
        </button>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={busy !== "idle"}
        >
          {t("stack.refresh")}
        </button>
        <button
          type="button"
          onClick={() => void runCheck()}
          disabled={busy !== "idle" || !running}
        >
          {busy === "ping" ? t("stack.checking") : t("stack.runCheck")}
        </button>
      </div>

      {progress && <p className="progress">{progress}</p>}

      {error != null && (
        <ErrorPanel error={error} onDismiss={() => setError(null)} />
      )}

      {status && (
        <>
          <table className="services">
            <thead>
              <tr>
                <th>{t("stack.columns.service")}</th>
                <th>{t("stack.columns.state")}</th>
                <th>{t("stack.columns.health")}</th>
                <th>{t("stack.columns.ports")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {status.services.map((s) => (
                <tr key={s.name}>
                  <td>{s.name}</td>
                  <td>
                    <span className={`dot dot-${s.state}`} />
                    {t(`stack.state.${s.state}`, { defaultValue: s.state })}
                  </td>
                  <td>
                    {t(`stack.health.${s.health}`, { defaultValue: s.health })}
                  </td>
                  <td>{s.publishers.join(", ") || "—"}</td>
                  <td>
                    <button
                      type="button"
                      className="link"
                      onClick={() => void toggleLogs(s.name)}
                    >
                      {logsFor === s.name
                        ? t("stack.hideLogs")
                        : t("stack.logs")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {logsFor && <pre className="logs">{logs || t("stack.noLogs")}</pre>}

          <dl className="meta">
            <dt>{t("stack.workspace")}</dt>
            <dd>{status.workspace}</dd>
            <dt>{t("stack.composeDir")}</dt>
            <dd>
              {status.project} · {status.composeDir}
            </dd>
          </dl>
          <p className="muted">{t("stack.stopNotice")}</p>
        </>
      )}

      {health && (
        <>
          <h3>{t("health.title")}</h3>
          <p>{health.ok ? t("health.ok") : t("health.degraded")}</p>
          <ul className="probes">
            {Object.entries(health.services).map(([name, s]) => (
              <li key={name}>
                <span className={s.ok ? "ok" : "bad"}>{s.ok ? "✓" : "✕"}</span>{" "}
                {name} — {s.detail}
              </li>
            ))}
          </ul>
        </>
      )}

      {/* Two conditions, shown separately because they have different fixes:
          naming a project is a text field, and getting credentials is a
          command the user runs outside this app. Gemini Enterprise refuses API
          keys, so there is no third path — a project id without ADC buys
          nothing, and saying "not configured" for both would hide which. */}
      <h3>{t("provider.title")}</h3>
      <p>{t("provider.intro")}</p>
      <label className="field">
        <span>{t("provider.project")}</span>
        <input
          value={projectDraft}
          placeholder={t("provider.projectHint")}
          onChange={(e) => setProjectDraft(e.target.value)}
        />
      </label>
      <div className="actions">
        <button
          type="button"
          onClick={() => void saveProject()}
          disabled={
            busy !== "idle" ||
            projectDraft.trim() === (provider?.projectId ?? "")
          }
        >
          {t("provider.save")}
        </button>
      </div>
      {provider && (
        <>
          <ul className="probes">
            <li>
              <span className={provider.configured ? "ok" : "bad"}>
                {provider.configured ? "✓" : "✕"}
              </span>{" "}
              {provider.configured
                ? t("provider.projectSet", { id: provider.projectId })
                : t("provider.projectUnset")}
            </li>
            <li>
              <span className={provider.adcFound ? "ok" : "bad"}>
                {provider.adcFound ? "✓" : "✕"}
              </span>{" "}
              {provider.adcFound
                ? t("provider.adcFound", { path: provider.adcPath })
                : t("provider.adcMissing")}
            </li>
          </ul>
          {!provider.adcFound && (
            <p className="caveat">{t("provider.adcHow")}</p>
          )}
          {savedProject && <p className="warn">{t("provider.needsRestart")}</p>}
        </>
      )}

      <h3>{t("ping.title")}</h3>
      <p>{t("ping.intro")}</p>
      {ping && (
        <>
          <p className="muted">{t("ping.ranAs", { id: ping.workflowId })}</p>
          <ul className="probes">
            {ping.probes.map((p) => (
              <li key={p.service}>
                <span className={p.ok ? "ok" : "bad"}>{p.ok ? "✓" : "✕"}</span>{" "}
                {p.service} — {p.detail}
              </li>
            ))}
          </ul>
          <p>
            {ping.probes.every((p) => p.ok) ? t("ping.ok") : t("ping.failed")}
          </p>
        </>
      )}
    </section>
  );
}
