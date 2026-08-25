import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { Tab } from "../App";
import {
  api,
  errorGuidanceKey,
  errorMessage,
  type ProjectSummary,
  type RunSummary,
} from "../lib/api";

/**
 * The landing screen: what this installation holds, and what it did recently.
 *
 * Two rules shape everything below.
 *
 * **A figure that could not be read is never a zero.** The summary reports its
 * catalog and graph legs separately, each with its own availability, and a card
 * whose leg is down renders an em dash carrying the failure in its tooltip. `0
 * conceptos` would be a claim about the corpus, and a user reading it would go
 * looking for a broken extractor rather than a stopped container.
 *
 * **A stopped stack is a state, not an error.** This is the first screen the app
 * opens on and the containers are normally down at that moment, so the red
 * failure panel every other screen shows would be the product's first
 * impression. The stack is probed first — a local, free call — and the control
 * API is not touched until something is listening, which also keeps the landing
 * screen from opening with a fifteen-second timeout.
 */

/** The one service the summary needs. The others may be down without this
 *  screen having anything to say about it — that is the Services screen's job. */
const API_SERVICE = "api";

type Stack = "checking" | "down" | "up";

/** Which metrics are shown, in the order the plan asks for them.
 *
 *  `read` returns null for "the leg that would know is unavailable", which is
 *  what the dash renders from. Returning 0 from any of these would be the bug
 *  this whole screen is written around.
 */
const METRICS: {
  key: string;
  read: (s: ProjectSummary) => number | null;
  detail: (s: ProjectSummary) => string | null;
}[] = [
  {
    key: "libraries",
    read: (s) => s.catalog.libraries,
    detail: (s) => s.catalog.detail,
  },
  {
    key: "documents",
    read: (s) => s.catalog.documents,
    detail: (s) => s.catalog.detail,
  },
  {
    key: "indexed",
    read: (s) => s.catalog.indexedVersions,
    detail: (s) => s.catalog.detail,
  },
  {
    key: "chunks",
    read: (s) => s.graph.nodes?.Chunk ?? null,
    detail: (s) => s.graph.detail,
  },
  {
    key: "concepts",
    read: (s) => s.graph.nodes?.Concept ?? null,
    detail: (s) => s.graph.detail,
  },
  {
    key: "claims",
    read: (s) => s.graph.nodes?.Claim ?? null,
    detail: (s) => s.graph.detail,
  },
  {
    key: "relations",
    read: (s) =>
      s.graph.semanticEdges === null
        ? null
        : Object.values(s.graph.semanticEdges).reduce((a, b) => a + b, 0),
    detail: (s) => s.graph.detail,
  },
];

/** Which state dot a run gets.
 *
 *  Not the Services screen's container states: a stopped container is neither
 *  good nor bad, and reusing its grey for a *failed run* made the one row a
 *  reader needs to notice the one that looked least like anything. A run that
 *  is waiting for a person is `--warn`, because it is the row that will do
 *  nothing until somebody acts — and its gate expires as a rejection. */
function dot(state: string): string {
  if (state === "succeeded") return "dot-ok";
  if (state === "running") return "dot-running";
  if (state === "awaiting_approval") return "dot-warn";
  if (state === "failed" || state === "cancelled") return "dot-bad";
  return "dot-absent";
}

function Metric({
  label,
  value,
  detail,
  locale,
}: {
  label: string;
  value: number | null;
  detail: string | null;
  locale: string;
}) {
  const { t } = useTranslation();
  return (
    <div className="metric" title={value === null ? detail ?? undefined : undefined}>
      <span className="metric-value">
        {value === null ? "—" : value.toLocaleString(locale)}
      </span>
      <span className="metric-label">{label}</span>
      {value === null && <span className="metric-absent">{t("home.unavailable")}</span>}
    </div>
  );
}

function Pages({ summary, locale }: { summary: ProjectSummary; locale: string }) {
  const { t } = useTranslation();
  const { pages } = summary;
  if (pages.available && pages.pages !== null) {
    return (
      <Metric
        label={t("home.metric.pages")}
        value={pages.pages}
        detail={null}
        locale={locale}
      />
    );
  }
  // Not "0 pages" and not a blank: nothing writes the column yet, and saying so
  // with the count is what makes it obvious the day that changes.
  return (
    <div className="metric" title={t("home.pagesHelp")}>
      <span className="metric-value">—</span>
      <span className="metric-label">{t("home.metric.pages")}</span>
      <span className="metric-absent">
        {t("home.pagesUnrecorded", {
          recorded: pages.recorded ?? 0,
          of: pages.of ?? 0,
        })}
      </span>
    </div>
  );
}

function Run({ run, locale }: { run: RunSummary; locale: string }) {
  const { t } = useTranslation();
  const when = new Date(run.startedAt);
  const stamp = Number.isNaN(when.getTime())
    ? run.startedAt
    : new Intl.DateTimeFormat(locale, {
        dateStyle: "short",
        timeStyle: "short",
      }).format(when);
  return (
    <li className="run-row">
      <span className={`dot ${dot(run.state)}`} aria-hidden="true" />
      <span className="run-title">
        {/* A run outlives the document it was spent on, so the title can be
            gone. The workflow id is what is left to name it by. */}
        {run.title ?? <code className="locator">{run.workflowId}</code>}
      </span>
      <span className="badge">{t(`home.run.kind.${run.kind}`, { defaultValue: run.kind })}</span>
      <span className="muted small">{t(`home.run.state.${run.state}`, { defaultValue: run.state })}</span>
      <span className="muted small">{stamp}</span>
    </li>
  );
}

/** `go` is optional so the screen can be rendered on its own in a test without
 *  a shell around it. Type-only import of `Tab`, so the cycle back to `App` is
 *  erased at compile time and the prop still cannot name a tab that does not
 *  exist. */
export function HomeScreen({ go }: { go?: (tab: Tab) => void }) {
  const { t, i18n } = useTranslation();
  const locale = i18n.resolvedLanguage ?? "es";

  const [stack, setStack] = useState<Stack>("checking");
  const [summary, setSummary] = useState<ProjectSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      // The stack first, and the control API only if it answers. Probing
      // compose is local and free; reaching a control API that is not there
      // costs the full request timeout before it says anything.
      const status = await api.stackStatus();
      const up = status.services.some(
        (s) => s.name === API_SERVICE && s.state === "running",
      );
      setStack(up ? "up" : "down");
      if (!up) {
        setSummary(null);
        return;
      }
      setSummary(await api.projectSummary());
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const guidance = error === null ? null : errorGuidanceKey(error);

  return (
    <section className="screen home">
      <h2>{t("home.title")}</h2>
      <p className="intro">{t("home.intro")}</p>

      <div className="actions">
        <button type="button" onClick={() => void load()} disabled={busy}>
          {t("home.refresh")}
        </button>
      </div>

      {error !== null && (
        <div className="error">
          <strong>{t("error.title")}</strong>
          {guidance && <p>{t(guidance)}</p>}
          <pre className="detail">{errorMessage(error)}</pre>
        </div>
      )}

      {stack === "checking" && <p className="muted">{t("home.loading")}</p>}

      {stack === "down" && (
        // Deliberately a notice and not an error: the containers being down is
        // the ordinary state of a freshly opened app.
        <div className="notice home-offline">
          <p>{t("home.offline")}</p>
          {go && (
            <button type="button" onClick={() => go("stack")}>
              {t("home.goToServices")}
            </button>
          )}
        </div>
      )}

      {stack === "up" && summary === null && busy && (
        <p className="muted">{t("home.loading")}</p>
      )}

      {summary !== null && (
        <>
          <div className="home-metrics">
            {METRICS.map((m) => (
              <Metric
                key={m.key}
                label={t(`home.metric.${m.key}`)}
                value={m.read(summary)}
                detail={m.detail(summary)}
                locale={locale}
              />
            ))}
            <Pages summary={summary} locale={locale} />
          </div>

          {summary.catalog.available && summary.catalog.libraries === 0 && (
            <p className="muted">{t("home.emptyProject")}</p>
          )}

          {!summary.graph.available && (
            <p className="warn">{t("home.graphUnavailable")}</p>
          )}

          <section className="panel">
            <h3>{t("home.activity")}</h3>
            {summary.recentRuns === null ? (
              <p className="muted">{t("home.activityUnavailable")}</p>
            ) : summary.recentRuns.length === 0 ? (
              <p className="muted">{t("home.activityEmpty")}</p>
            ) : (
              <ul className="run-list">
                {summary.recentRuns.map((r) => (
                  <Run key={r.id} run={r} locale={locale} />
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </section>
  );
}
