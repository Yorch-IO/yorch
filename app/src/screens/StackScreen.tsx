import { openUrl } from "@tauri-apps/plugin-opener";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { AnswerStyles } from "./AnswerStyles";

import { useBackend } from "../lib/backend";
import {
  api,
  errorGuidanceKey,
  errorMessage,
  type BackendMode,
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
  // Held above this screen, not in it. Which plane the app talks to decides
  // what every other screen may read, so the four writes below — the initial
  // read, a saved mode, a sign-in and a sign-out — are what invalidates the
  // library list and remounts the rest of the app. See `lib/backend.tsx`.
  const { info: backend, publish: setBackend, reload: reloadBackend } = useBackend();
  /** No organisation here on purpose. This release supports an account with a
   *  single membership, and the server resolves that one from the token — so
   *  offering a field would be offering a choice with one option and a way to
   *  get it wrong. The header and its plumbing stay in Rust, because that is
   *  what a picker would use the day several memberships are provisioned. */
  const [backendDraft, setBackendDraft] = useState<{
    mode: BackendMode;
    baseUrl: string;
  } | null>(null);
  const [signingIn, setSigningIn] = useState(false);
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
    // First, and deliberately before the stack is asked about: this is the one
    // read that must work when there is no local stack at all. In cloud mode
    // Docker is irrelevant, and a user whose stack cannot start still has to be
    // able to see — and change — which backend they are pointed at. Putting it
    // after `stackStatus`'s early return hid the whole section on exactly the
    // machine that needed it.
    // Read through the provider rather than calling Rust here. It is the one
    // owner of this value now, because the rest of the app is invalidated by
    // it; two reads would be two chances for this screen and everything else to
    // disagree about which plane is selected.
    const chosen = await reloadBackend();
    const mode: BackendMode = chosen?.mode ?? "local";
    if (chosen) {
      setBackendDraft(
        (draft) => draft ?? { mode: chosen.mode, baseUrl: chosen.baseUrl },
      );
    }

    // The stack and the provider project belong to the local backend and to
    // nothing else, so in cloud mode they are not asked about at all.
    //
    // Not merely tidier — the commands *refuse* in cloud mode now, and the
    // `return` below treats a refusal as fatal, so asking anyway skipped the
    // health read underneath it. That is how the paid plane ended up never
    // being contacted from a signed-in window: no request, no `last_login_at`,
    // no health table, and a red panel saying Docker was unavailable to a user
    // for whom Docker is irrelevant. Docker's absence is already explicitly not
    // an error further down this file; this is the same rule, one call earlier.
    if (mode === "local") {
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
    } else {
      setStatus(null);
      setProvider(null);
      setError(null);
    }
    // Health comes from the control API, which is only up once the stack is.
    // A failure here is expected before that and must not surface as an error.
    try {
      setHealth(await api.controlHealth());
    } catch {
      setHealth(null);
    }
  }, [reloadBackend]);

  /**
   * Save the chosen plane.
   *
   * The address is validated in Rust, not here: it has to be refused by the
   * request that set it rather than by every screen afterwards, and a check in
   * the webview would only be the first of two.
   */
  /**
   * The button stays disabled for as long as the browser has the user, which
   * can be minutes — a password and possibly a one-time code. Without the flag
   * a second press binds a second listener to the same port and fails with an
   * error about an address in use, which says nothing about what happened.
   */
  const startSession = useCallback(async () => {
    setError(null);
    setSigningIn(true);
    try {
      setBackend(await api.signIn());
    } catch (e) {
      setError(e);
    } finally {
      setSigningIn(false);
    }
  }, []);

  const endSession = useCallback(async () => {
    setError(null);
    try {
      setBackend(await api.signOut());
    } catch (e) {
      setError(e);
    }
  }, []);

  const saveBackend = useCallback(async () => {
    if (!backendDraft) return;
    setError(null);
    try {
      setBackend(
        // Empty: an absent `X-Tenant-Id`, which is what tells the server to
        // use the account's sole membership. Not an empty header.
        await api.setBackendMode(backendDraft.mode, backendDraft.baseUrl, ""),
      );
      // Everything on this screen describes whichever plane is now selected,
      // so it is all stale.
      await refresh();
    } catch (e) {
      setError(e);
    }
  }, [backendDraft, refresh]);

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

  /**
   * The backend chooser, rendered by *both* returns below.
   *
   * A machine with no Docker used to get a screen that talked only about
   * installing Docker — with no way off it. That is precisely the user the paid
   * service exists for: Docker is a requirement of the local backend and of
   * nothing else, and stranding them on an install prompt made the switch
   * unreachable exactly where it mattered.
   */
  const backendChooser = backendDraft && (
    <>
  {/* Which plane, before anything about the local one. The two speak the
            same requests, so this is the only place in the app that knows there
            is more than one. */}
        <h3>{t("backend.title")}</h3>
        <p>{t("backend.intro")}</p>
        {backendDraft && (
          <>
            <label className="field">
              <span>{t("backend.mode")}</span>
              {/* `aria-label` as well as the visible span: a `<select>` inside a
                  `<label>` folds its own option text into the label's accessible
                  name, so "Which backend" becomes "Which backendLocal…Paid
                  service" — unmatchable, and wrong for a screen reader too. The
                  inputs beside it need no such thing: they have no text content. */}
              <select
                aria-label={t("backend.mode")}
                value={backendDraft.mode}
                onChange={(e) =>
                  setBackendDraft({
                    ...backendDraft,
                    mode: e.target.value as BackendMode,
                  })
                }
              >
                <option value="local">{t("backend.local")}</option>
                <option value="cloud">{t("backend.cloud")}</option>
              </select>
            </label>
            {backendDraft.mode === "cloud" ? (
              <>
                <label className="field">
                  <span>{t("backend.baseUrl")}</span>
                  <input
                    value={backendDraft.baseUrl}
                    placeholder={t("backend.baseUrlHint")}
                    onChange={(e) =>
                      setBackendDraft({ ...backendDraft, baseUrl: e.target.value })
                    }
                  />
                </label>
              </>
            ) : (
              <p className="caveat">{t("backend.localNote")}</p>
            )}
            <div className="actions">
              <button
                type="button"
                onClick={() => void saveBackend()}
                disabled={busy !== "idle"}
              >
                {t("backend.save")}
              </button>
            </div>
            {backend?.mode === "cloud" && (
              <>
                <ul className="probes">
                  <li>
                    <span className={backend.signedIn ? "ok" : "bad"}>
                      {backend.signedIn ? "✓" : "✕"}
                    </span>{" "}
                    {backend.signedIn
                      ? backend.email
                        ? t("backend.signedInAs", { email: backend.email })
                        : t("backend.signedIn")
                      : t("backend.signedOut")}
                  </li>
                </ul>
                <div className="actions">
                  {backend.signedIn ? (
                    <button type="button" onClick={() => void endSession()}>
                      {t("backend.signOut")}
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => void startSession()}
                      disabled={signingIn}
                    >
                      {signingIn ? t("backend.signingIn") : t("backend.signIn")}
                    </button>
                  )}
                </div>
                {backend.signedIn && (
                  <>
                    <p className="caveat">
                      {t(`backend.secretStore.${backend.secretStore}`)}
                    </p>
                    <p className="caveat">{t("backend.signOutNote")}</p>
                  </>
                )}
              </>
            )}
          </>
        )}
    </>
  );

  // Docker is a requirement of the local backend and of nothing else, so its
  // absence is not an error at all for someone using the paid service.
  if (dockerError !== null && backend?.mode !== "cloud") {
    return (
      <section className="panel">
        <h2>{t("docker.missingTitle")}</h2>
        <p>{t("docker.missingBody")}</p>
        <pre className="detail">{dockerError}</pre>
        {backendChooser}
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
      {backendChooser}

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

      <AnswerStyles />

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
