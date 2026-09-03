/**
 * The queue: what has been imported, what is running, and what is waiting.
 *
 * The Import screen used to forget everything the moment a gate was answered —
 * one run in `useState`, no feedback after approval, and a second import
 * silently replacing the first. This is the other half of the fix: the list is
 * read from the catalog, so it survives a relaunch, a plane switch and a crash,
 * and it keeps completed runs rather than discarding them.
 *
 * Each row shows one of three things, chosen by where the run is:
 *
 * - **waiting** → its own approval gate, with its own profile choice.
 * - **running** → the stage, the progress and a two-click cancel.
 * - **finished** → a way into the ledger, which is what ends the amnesia: an
 *   approved import stays on screen and becomes readable.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { GateReview } from "./GateReview";
import { RunAudit } from "./RunAudit";
import { api, errorGuidanceKey, errorMessage, type StageOptions } from "./lib/api";
import { unfinished, waiting, type QueueItem } from "./lib/importQueue";
import { runDot } from "./lib/runState";

/** Stopping a run destroys work already paid for, so it cannot happen on one
 *  click. The same two-step `ActivityIndicator` uses, for the same reason. */
function Cancel({ workflowId, onDone }: { workflowId: string; onDone: () => void }) {
  const { t } = useTranslation();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  if (!confirming)
    return (
      <button type="button" className="link" onClick={() => setConfirming(true)}>
        {t("activity.cancel")}
      </button>
    );
  return (
    <span className="confirm">
      <button
        type="button"
        className="danger"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          api
            .cancelRun(workflowId)
            .then(onDone)
            .catch(() => setFailed(true))
            .finally(() => setBusy(false));
        }}
      >
        {busy ? t("activity.cancelling") : t("activity.confirmCancel")}
      </button>
      <button type="button" className="link" onClick={() => setConfirming(false)}>
        {t("activity.keepGoing")}
      </button>
      {failed && <span className="warn">{t("activity.cancelFailed")}</span>}
    </span>
  );
}

/** Publish an index the pipeline built and deliberately did not promote.
 *
 *  A structural collision withholds exactly one step. Everything else ran: the
 *  chunks are in Qdrant, the graph is projected, the bill is already paid, and
 *  the *previous* version stays answerable — so the document is one press away
 *  from being findable, and until it is pressed the run's whole cost bought
 *  something nobody can reach. Measured on the run that prompted this button:
 *  $4.1219, 444 points indexed, 2,466 claims projected, and no route to any of
 *  it from the screen that reported the outcome.
 *
 *  **Offered for `structural_mismatch` and for nothing else.** A withheld
 *  activation is the one blocked outcome that leaves a complete index behind;
 *  `pending` alone does not mean that — four versions in this catalog are
 *  `pending` because their run was *cancelled*, and their indexes are partial.
 *  Keying on the reason rather than on the version's state is what keeps this
 *  from publishing one of those. That is also why it lives here rather than in
 *  the Library's version list, which has the state and not the reason.
 *
 *  One press, no confirm, like the Library's own promote button: it is free, it
 *  is idempotent — `catalog.activate` upserts — and it is undone by promoting
 *  the other version. */
function Activate({
  libraryId,
  versionId,
  onDone,
}: {
  libraryId: string;
  versionId: string;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [documents, setDocuments] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);

  // The run stays `blocked` for ever — it *was* withheld, and rewriting that
  // would lose the fact. So what hides the button afterwards is this, and the
  // count is worth saying: a version can be held by more than one document.
  if (documents !== null)
    return <p className="notice">{t("queue.activateDone", { count: documents })}</p>;

  return (
    <div className="queue-activate">
      <p className="muted small">{t("queue.activateWhy")}</p>
      <span>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            setError(null);
            api
              .versionActivate(libraryId, versionId)
              .then((a) => {
                setDocuments(a.documents.length);
                onDone();
              })
              .catch((e) => setError(e))
              .finally(() => setBusy(false));
          }}
        >
          {busy ? t("queue.activateBusy") : t("queue.activate")}
        </button>
      </span>
      {error !== null && (
        <p className="warn">
          {errorGuidanceKey(error) ? t(errorGuidanceKey(error)!) : errorMessage(error)}
        </p>
      )}
    </div>
  );
}

function Row({
  item,
  stages,
  onDecide,
  onChanged,
  locale,
}: {
  item: QueueItem;
  stages: StageOptions;
  onDecide: (workflowId: string, approved: boolean, options: StageOptions) => void;
  onChanged: () => void;
  locale: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const { run } = item;
  const live = unfinished(run);
  const state = item.state ?? run.state;
  const name = run.title ?? run.workflowId;

  return (
    <li className="queue-item">
      <div className="queue-row">
        <span className={`dot ${runDot(item.state ?? run.state)}`} aria-hidden="true" />
        <span className="queue-name">{name}</span>
        <span className="badge">
          {t(`home.run.kind.${run.kind}`, { defaultValue: run.kind })}
        </span>
        <span className="muted small">
          {/* The live state when Temporal answered, the catalog's otherwise.
              Never blank: a row with no state reads as a row that is broken. */}
          {t(`home.run.state.${state}`, { defaultValue: state })}
          {/* The stage only while the run is going, and only when it says
              something the state did not.

              Both halves were found by looking. A run parked at its gate read
              "esperando aprobación · esperando aprobación", because the state
              and the stage are the same word there. And a finished one read
              "completada · terminando" or "fallida · extrayendo conceptos" —
              `activity.stage.*` is a present-continuous progress label, so a run
              that ended half an hour ago claimed to still be doing something.
              Where it died is a real question, and the ledger below answers it
              properly; the row is not the place. */}
          {live && item.stage && item.stage !== state && (
            <> · {t(`activity.stage.${item.stage}`, { defaultValue: item.stage })}</>
          )}
        </span>
        <span className="muted small">
          {new Date(run.startedAt).toLocaleString(locale)}
        </span>
        <span className="queue-spend">
          {/* Four decimals, because two would round a lie: a stage can bill less
              than a cent and reporting it as $0.00 says it was free. */}
          {run.usdSoFar === null ? (
            <span className="muted">—</span>
          ) : (
            <span>${run.usdSoFar.toFixed(4)}</span>
          )}
        </span>
        <span className="queue-actions">
          {live && <Cancel workflowId={run.workflowId} onDone={onChanged} />}
          {!live && (
            <button type="button" className="link" onClick={() => setOpen(!open)}>
              {open ? t("queue.hideDetail") : t("queue.showDetail")}
            </button>
          )}
        </span>
      </div>

      {item.progress && (
        <p className="muted small">
          <progress value={item.progress.done} max={item.progress.total} />{" "}
          {t("activity.progress", {
            done: item.progress.done,
            total: item.progress.total,
          })}
        </p>
      )}

      {run.errorKind && (
        <p className="warn">
          {t("queue.ended", {
            state: t(`home.run.state.${run.state}`, { defaultValue: run.state }),
            kind: run.errorKind,
          })}
        </p>
      )}

      {/* The way out of a withheld activation, beside the line that reports it.
          `libraryId` and `versionId` are on the run row already; a run whose
          document has since been removed carries neither, and then there is
          nothing to promote. */}
      {!live &&
        run.state === "blocked" &&
        run.errorKind === "structural_mismatch" &&
        run.libraryId !== null &&
        run.versionId !== null && (
          <Activate
            libraryId={run.libraryId}
            versionId={run.versionId}
            onDone={onChanged}
          />
        )}

      {waiting(run) && item.gate && (
        <GateReview
          report={item.gate}
          stages={stages}
          busy={deciding}
          onDecide={(approved, options) => {
            setDeciding(true);
            onDecide(run.workflowId, approved, options);
          }}
        />
      )}
      {waiting(run) && !item.gate && <p className="waiting">{t("queue.gateComing")}</p>}

      {open && !live && <RunAudit runId={run.workflowId} />}
    </li>
  );
}

export function ImportQueue({
  items,
  loaded,
  error,
  stages,
  onDecide,
  onChanged,
  locale,
}: {
  items: QueueItem[];
  loaded: boolean;
  error: unknown;
  stages: StageOptions;
  onDecide: (workflowId: string, approved: boolean, options: StageOptions) => void;
  onChanged: () => void;
  locale: string;
}) {
  const { t } = useTranslation();

  if (error !== null)
    // A notice, not the red panel: a control plane that is not up yet is the
    // ordinary state of a freshly opened app, and the queue is not the thing
    // the person came here to do.
    return (
      <section className="panel">
        <h3>{t("queue.title")}</h3>
        <p className="notice">
          {errorGuidanceKey(error) ? t(errorGuidanceKey(error)!) : errorMessage(error)}
        </p>
      </section>
    );

  return (
    <section className="panel">
      <h3>{t("queue.title")}</h3>
      {!loaded ? (
        <p className="waiting">{t("queue.loading")}</p>
      ) : items.length === 0 ? (
        <p className="muted">{t("queue.empty")}</p>
      ) : (
        <ul className="queue">
          {items.map((item) => (
            <Row
              key={item.run.id}
              item={item}
              stages={stages}
              onDecide={onDecide}
              onChanged={onChanged}
              locale={locale}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
