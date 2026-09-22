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

import { CorrectionReview } from "./CorrectionReview";
import { GateReview } from "./GateReview";
import { VideoGateReview } from "./VideoGateReview";
import { RunAudit } from "./RunAudit";
import { api, errorGuidanceKey, errorMessage, type StageOptions } from "./lib/api";
import { unfinished, waiting, type QueueItem } from "./lib/importQueue";
import { runDot } from "./lib/runState";
import { useLocalTranscriber } from "./lib/localTranscriber";
import { AWAITING, percent } from "./lib/transcribing";

/** What to call a library on a queue row: its name, or its id when it has none
 *  — and the id alone for one this installation cannot see, which is a real
 *  state rather than a gap. Shorter than `libraryLabel`'s "Name (id)" on
 *  purpose: the row already carries the document's title, the kind and the
 *  state, and this is the fourth thing on it. */
export interface QueueLibrary {
  id: string;
  name: string;
}

function libraryName(id: string | null, rows: QueueLibrary[]): string | null {
  if (!id) return null;
  const found = rows.find((l) => l.id === id);
  const named = found?.name.trim() ?? "";
  return named === "" || named === id ? id : named;
}

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
  showLibrary,
  stages,
  onDecide,
  onChanged,
  locale,
}: {
  item: QueueItem;
  /** The library this run went to, or null to leave it off — which is what the
   *  screen does when the queue is already narrowed to one. */
  showLibrary: string | null;
  stages: StageOptions;
  /** May return a promise; the row re-enables its buttons when it settles,
   *  whichever way it settles. */
  onDecide: (
    workflowId: string,
    approved: boolean,
    options: StageOptions,
  ) => void | Promise<void>;
  onChanged: () => void;
  locale: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const local = useLocalTranscriber();
  const [switching, setSwitching] = useState(false);
  const [switched, setSwitched] = useState(false);
  const { run } = item;
  const live = unfinished(run);
  const state = item.state ?? run.state;
  // `title` is the *document's*, joined, and a run that failed before it
  // registered one has none — which is now every run that dies in its first
  // activity, since the row is opened before it. `label` is what the run knew
  // about itself: the URL somebody pasted, or the file they picked.
  const name = run.title ?? run.label ?? run.workflowId;

  return (
    <li className="queue-item">
      <div className={`queue-row${showLibrary !== null ? " has-library" : ""}`}>
        <span className={`dot ${runDot(item.state ?? run.state)}`} aria-hidden="true" />
        <span className="queue-name">{name}</span>
        <span className="badge">
          {t(`home.run.kind.${run.kind}`, { defaultValue: run.kind })}
        </span>
        {/* Which shelf this went to. The queue spans every library now, and
            without this a row is a title with no answer to "where". */}
        {showLibrary !== null && (
          <span className="muted small queue-library">{showLibrary}</span>
        )}
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
              properly; the row is not the place.

              And a run parked for this machine sits in the `transcribing`
              stage while transcribing is exactly what is *not* happening —
              the stage is where the workflow is, the state is what it is
              waiting for. Printing both would read as a contradiction. */}
          {live && item.stage && item.stage !== state && state !== AWAITING && (
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

      {/* A run parked for a transcript this machine has not made yet.
          Rendered here as well as on Services because this is the screen a
          person is on when they wonder why an approved run is not moving — and
          because the way out, handing it to Amazon, belongs beside the run it
          is about rather than in a settings panel. */}
      {state === AWAITING && (
        <p className="muted small">
          {t(local.paused ? "queue.transcribeHerePaused" : "queue.transcribeHere")}
          {local.current?.workflowId === run.workflowId && (
            <>
              {" "}
              <progress value={percent(local.attempt)} max={100} />{" "}
              {t(`whisper.phase.${local.attempt?.phase ?? "audio"}`)}
            </>
          )}{" "}
          <button
            type="button"
            className="link"
            disabled={switching || switched}
            onClick={() => {
              setSwitching(true);
              void local
                .giveUp(run.workflowId)
                .then((ok) => setSwitched(ok))
                // Always, whichever way it went: a refused switch that left the
                // button disabled would be the `deciding` latch again, one
                // screen over.
                .finally(() => {
                  setSwitching(false);
                  onChanged();
                });
            }}
          >
            {t(switching ? "queue.transcribeSwitching" : "queue.transcribeOnAmazon")}
          </button>
          {switched && <> {t("queue.transcribeSwitched")}</>}
        </p>
      )}

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
          {/* The kind is translated, not printed raw. A person reading a red row
              needs to know what to do, and `youtube_refused_this_host` and
              `video_unavailable` call for opposite things — one is the video,
              the other is this machine. `defaultValue` keeps every kind that has
              no wording rendering as itself rather than as a bare key. */}
          {t("queue.ended", {
            state: t(`home.run.state.${run.state}`, { defaultValue: run.state }),
            kind: t(`queue.errorKind.${run.errorKind}`, {
              defaultValue: run.errorKind,
            }),
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

      {/* The second gate gets its own panel, fed by its own route. Rendering
          `GateReview` here showed the *first* gate's report — frozen before
          any money was spent — so it quoted an estimate for correction on a
          run already billed for it and said nothing had been paid for, with
          the real figure on the row directly above. */}
      {item.correction && (
        <CorrectionReview
          report={item.correction}
          stages={stages}
          busy={deciding}
          onDecide={(approved, options) => {
            setDeciding(true);
            // The same `finally` as the panels below, for the same recorded
            // incident: a refused approval used to leave both buttons disabled
            // with no way to retry.
            void Promise.resolve(
              onDecide(run.workflowId, approved, options),
            ).finally(() => setDeciding(false));
          }}
        />
      )}
      {waiting(run) && item.gate && !item.correction && (
        <GateReview
          report={item.gate}
          stages={stages}
          busy={deciding}
          onDecide={(approved, options) => {
            setDeciding(true);
            // **`finally`, not `then`.** This used to set the latch and never
            // clear it, so a refused approval left both buttons disabled with
            // the panel still on screen and no way to retry — seen in the real
            // window when the paid plane answered 422 and the only remedy was
            // relaunching the app, which nobody guesses. A remount is what
            // cleared it, which is why the happy path never showed the bug: the
            // row moves on and the component goes with it.
            void Promise.resolve(
              onDecide(run.workflowId, approved, options),
            ).finally(() => setDeciding(false));
          }}
        />
      )}
      {waiting(run) && item.videoGate && (
        <VideoGateReview
          report={item.videoGate}
          stages={stages}
          busy={deciding}
          onDecide={(approved, options) => {
            setDeciding(true);
            // **`finally`, not `then`.** This used to set the latch and never
            // clear it, so a refused approval left both buttons disabled with
            // the panel still on screen and no way to retry — seen in the real
            // window when the paid plane answered 422 and the only remedy was
            // relaunching the app, which nobody guesses. A remount is what
            // cleared it, which is why the happy path never showed the bug: the
            // row moves on and the component goes with it.
            void Promise.resolve(
              onDecide(run.workflowId, approved, options),
            ).finally(() => setDeciding(false));
          }}
        />
      )}
      {waiting(run) && !item.gate && !item.videoGate && (
        <p className="waiting">{t("queue.gateComing")}</p>
      )}

      {open && !live && <RunAudit runId={run.workflowId} />}
    </li>
  );
}

export function ImportQueue({
  items,
  loaded,
  error,
  filter,
  onFilter,
  libraries,
  stages,
  onDecide,
  onChanged,
  locale,
}: {
  items: QueueItem[];
  loaded: boolean;
  error: unknown;
  /** The library the queue is narrowed to, and `null` for every one of them. */
  filter: string | null;
  onFilter: (libraryId: string | null) => void;
  /** What the narrowing control offers and what names a row's library. A prop
   *  rather than a `useLibraries()` call, because everything else this
   *  component needs is one too: reading a context here would make every test
   *  that renders the Import screen mount a provider to see a file chooser. */
  libraries: QueueLibrary[];
  stages: StageOptions;
  /** May return a promise; the row re-enables its buttons when it settles,
   *  whichever way it settles. */
  onDecide: (
    workflowId: string,
    approved: boolean,
    options: StageOptions,
  ) => void | Promise<void>;
  onChanged: () => void;
  locale: string;
}) {
  const { t } = useTranslation();
  const rows = libraries;

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
      <div className="queue-head">
        <h3>{t("queue.title")}</h3>
        {/* The queue spans every library, so this narrows rather than scopes.
            It is a request parameter, not a filter over what arrived — see
            `useImportQueue`, where the 30-run limit is the reason. */}
        <label className="field field-inline queue-filter">
          <span>{t("queue.filter")}</span>
          <select
            value={filter ?? ""}
            onChange={(e) => onFilter(e.target.value === "" ? null : e.target.value)}
          >
            <option value="">{t("queue.filterAll")}</option>
            {rows.map((l) => (
              <option key={l.id} value={l.id}>
                {libraryName(l.id, rows)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {!loaded ? (
        <p className="waiting">{t("queue.loading")}</p>
      ) : items.length === 0 ? (
        <p className="muted">{filter === null ? t("queue.empty") : t("queue.emptyHere")}</p>
      ) : (
        <ul className="queue">
          {items.map((item) => (
            <Row
              key={item.run.id}
              item={item}
              // Only when the list spans more than one: repeating the library
              // somebody just filtered to on every row is noise.
              showLibrary={filter === null ? libraryName(item.run.libraryId, rows) : null}
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
