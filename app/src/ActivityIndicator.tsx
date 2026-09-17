/**
 * What is happening right now, visible from every screen.
 *
 * It lives in the shell rather than on a screen because the failure it exists to
 * stop is *not being on that screen*. An import was started, the person moved on,
 * and for the next hour nothing anywhere said it was still going — while it made
 * one generation call every five seconds. A panel you have to navigate to cannot
 * fix that; a line that is always there can.
 *
 * **It renders nothing when nothing is running.** An indicator that is always
 * present is furniture, and furniture stops being read.
 *
 * Cancelling confirms inline rather than through the dialog plugin, the same
 * decision `LibraryScreen` made and for the same reasons — no widening of the
 * capability surface, and the confirmation can name what it is about to stop.
 * It matters more here: this control is one click away on every screen, and what
 * it destroys is work already paid for.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "./lib/api";
import { useActiveRuns, type ActiveRun } from "./lib/activeRuns";
import { useChannels } from "./lib/channels";
import { useLibraries } from "./lib/libraries";
import { runDestination } from "./lib/runOpen";
import type { Tab } from "./App";

/** Two decimals is the wrong precision for these numbers: a run's spend is
 *  often $0.0250, which renders as "$0.03" — a 20% lie on a figure somebody is
 *  reading to decide whether to stop. Four is what the ledger stores. */
function money(usd: number): string {
  return `$${usd.toFixed(4)}`;
}

function RunLine({
  run,
  go,
  open,
}: {
  run: ActiveRun;
  go?: (tab: Tab) => void;
  /** Select whatever the destination screen needs, then show it. */
  open?: (run: ActiveRun) => void;
}) {
  const { t } = useTranslation();
  const [confirming, setConfirming] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [failed, setFailed] = useState(false);

  const cancel = async () => {
    setCancelling(true);
    setFailed(false);
    try {
      await api.cancelRun(run.workflowId);
      setConfirming(false);
    } catch {
      // Named rather than silent: a cancel that did nothing, on a run that is
      // spending, is the one failure here that must not be quiet.
      setFailed(true);
    } finally {
      setCancelling(false);
    }
  };

  const name = run.title ?? run.workflowId;
  const stage = run.stage
    ? t(`activity.stage.${run.stage}`, { defaultValue: run.stage })
    : null;

  return (
    <li className="activity-run">
      <span className="activity-name" title={name}>
        {name}
      </span>
      <span className="badge">{t(`home.run.kind.${run.kind}`, { defaultValue: run.kind })}</span>
      {stage && <span className="muted small">{stage}</span>}

      {run.progress && (
        <>
          {/* A native progress element, so the value is announced rather than
              only drawn: this is the one number on screen a person is waiting
              on. */}
          <progress value={run.progress.done} max={run.progress.total} />
          <span className="muted small">
            {t("activity.progress", {
              done: run.progress.done,
              total: run.progress.total,
            })}
          </span>
        </>
      )}

      {run.usdSoFar !== null && (
        <span className="muted small" title={t("activity.spentLags")}>
          {t("activity.spent", { usd: money(run.usdSoFar) })}
        </span>
      )}

      <span className="activity-actions">
        {/* To the screen that *owns* this run, with its library or its channel
            selected — not simply to the import tab, which is filtered by a
            library the person is almost certainly not on. */}
        {go && open && (
          <button type="button" className="link" onClick={() => open(run)}>
            {t("activity.view")}
          </button>
        )}
        {confirming ? (
          <>
            <button type="button" onClick={() => void cancel()} disabled={cancelling}>
              {cancelling ? t("activity.cancelling") : t("activity.confirmCancel")}
            </button>
            <button type="button" className="link" onClick={() => setConfirming(false)}>
              {t("activity.keepGoing")}
            </button>
          </>
        ) : (
          <button type="button" className="link" onClick={() => setConfirming(true)}>
            {t("activity.cancel")}
          </button>
        )}
      </span>

      {failed && <span className="warn small">{t("activity.cancelFailed")}</span>}
    </li>
  );
}

export function ActivityIndicator({ go }: { go?: (tab: Tab) => void }) {
  const { t } = useTranslation();
  const runs = useActiveRuns();
  const libraries = useLibraries();
  const channels = useChannels();

  const open = (run: ActiveRun) => {
    if (!go) return;
    const known = new Set(channels.rows.map((c) => c.channel.channelId));
    const to = runDestination(run.libraryId, known);
    // Selected before the tab changes, so the screen mounts already looking at
    // the right thing rather than fetching the old one first.
    if (to.channelId !== null) channels.select(to.channelId);
    if (to.libraryId !== null) libraries.select(to.libraryId);
    go(to.tab);
  };

  if (runs.length === 0) return null;

  return (
    <section className="activity" aria-label={t("activity.title")}>
      <h2 className="activity-title">{t("activity.title")}</h2>
      <ul className="activity-list">
        {runs.map((run) => (
          <RunLine key={run.workflowId} run={run} go={go} open={open} />
        ))}
      </ul>
    </section>
  );
}
