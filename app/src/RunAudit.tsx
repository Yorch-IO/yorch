/**
 * What a run actually did — every stage, its duration, its charges, its outputs.
 *
 * This exists because the product could bill $10 across sixteen stages and
 * afterwards answer exactly one question about it: the aggregate. `run.stage` is
 * a single column each transition overwrites, so the free half of the pipeline
 * left no trace at all once a run ended, and the run that charged semantic
 * extraction twice was diagnosed by reading worker logs and `docker inspect`,
 * because there was nowhere else to look.
 *
 * Two sources, and the split is the point:
 *
 * - **The ledger** comes from the catalog and always answers, including for a
 *   run whose Temporal history has aged out — which is every run past the
 *   retention period, and which is exactly when somebody goes looking.
 * - **The raw history** comes from Temporal and shows what no application code
 *   recorded: retries, heartbeat timeouts, the attempt that was closed while it
 *   kept working. It is fetched only when a person expands it, because it is the
 *   one call here that can genuinely be slow, and it is allowed to be absent.
 *
 * One component, two entry points: a version row in the Library, and a finished
 * item in the Import queue.
 */
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorGuidanceKey, errorMessage } from "./lib/api";
import type { AuditStage, RunAudit as Audit, RunEventPage } from "./lib/api";
import { duration, runDot } from "./lib/runState";
import { Money } from "./Money";

/** A stage name, localised, falling through to the identifier.
 *
 *  `defaultValue` rather than a throw: a control plane newer than this bundle
 *  can name a stage the bundle has never heard of, and rendering the raw
 *  identifier is a worse label but a working screen. */
function StageName({ stage }: { stage: string | null }) {
  const { t } = useTranslation();
  if (stage === null)
    // The trailing row, which collects charges no pipeline stage claims — the
    // three a question makes. Named rather than left blank, because a blank
    // stage with money against it reads as a bug.
    return <em className="muted">{t("audit.unattributed")}</em>;
  return <>{t(`audit.stage.${stage}`, { defaultValue: stage })}</>;
}

function StageRow({ row, locale }: { row: AuditStage; locale: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const took = duration(row.seconds);
  const charges = row.cost?.entries ?? [];
  return (
    <>
      <tr>
        <td>
          <StageName stage={row.stage} />
          {row.outcome && (
            <>
              {" "}
              <span className={`dot ${runDot(row.outcome)}`} aria-hidden="true" />{" "}
              <strong>
                {t(`audit.outcome.${row.outcome}`, { defaultValue: row.outcome })}
              </strong>
            </>
          )}
          {row.detail && <div className="muted small">{row.detail}</div>}
        </td>
        <td className="small">
          {row.at
            ? new Date(row.at).toLocaleTimeString(locale, {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
              })
            : "—"}
        </td>
        {/* Nothing rather than `0s` for a stage with no end: it is either still
            running or it is the terminal instant, and both are said in the row
            already. */}
        <td className="small">{took ?? "—"}</td>
        <td>
          {/* `null` is not zero. A stage that does not spend and a stage whose
              charge was not recorded are different claims, and `$0.00` renders
              as the first while sometimes meaning the second. */}
          {row.cost ? <Money usd={row.cost.usd} /> : <span className="muted">—</span>}
          {/* Only when *some* of the charges were priced. With `usd === null`
              `Money` already renders "sin precio", and adding "1 cargo(s) sin
              precio conocido" under it said the same thing twice — which is
              what a 1440px screenshot showed and what no assertion would. */}
          {row.cost && row.cost.usd !== null && row.cost.unpricedEntries > 0 && (
            <div className="unpriced small">
              {t("audit.unpriced", { count: row.cost.unpricedEntries })}
            </div>
          )}
          {/* The charges belong here, not under "Produced": a charge is not
              something the stage produced, and putting the toggle in that column
              made `semantics 1 cargo(s)` read as two artifacts. */}
          {charges.length > 0 && (
            <div>
              <button type="button" className="link" onClick={() => setOpen(!open)}>
                {open
                  ? t("audit.hideCharges")
                  : t("audit.showCharges", { count: charges.length })}
              </button>
            </div>
          )}
        </td>
        <td>
          {row.artifacts.length === 0 ? (
            <span className="muted">—</span>
          ) : (
            row.artifacts.map((a) => (
              <code className="locator" key={a.name} title={a.relPath}>
                {a.name}
              </code>
            ))
          )}
        </td>
      </tr>
      {open &&
        charges.map((c, i) => (
          <tr className="audit-charge" key={`${c.stage}-${i}`}>
            <td colSpan={2}>
              <code className="locator">{c.stage}</code>{" "}
              <span className="model">{c.model}</span>
            </td>
            <td className="small">
              {t("audit.tokens", { input: c.inputTokens, output: c.outputTokens })}
            </td>
            <td colSpan={2}>
              <Money usd={c.usd} />
            </td>
          </tr>
        ))}
    </>
  );
}

/** The raw history, fetched on first expand and never before.
 *
 *  `available: false` renders as a sentence, not as an empty table: "Temporal
 *  has forgotten this run" and "this run did nothing" must not look the same.
 *  The same distinction `/project-summary` makes for a leg it could not read. */
function RawLog({ runId, locale }: { runId: string; locale: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<RunEventPage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  /** Whether this run's history has already been asked for.
   *
   *  A ref rather than the `loading` flag, and the difference is not cosmetic:
   *  with `loading` in the dependency list, setting it re-ran the effect, which
   *  first ran the *previous* run's cleanup — flipping the `alive` flag the
   *  in-flight request was holding, so the response arrived and was discarded
   *  and the panel waited forever on a fetch that had already succeeded. The
   *  effect now depends only on what it actually keys off. */
  const asked = useRef<string | null>(null);

  useEffect(() => {
    if (!open || asked.current === runId) return;
    asked.current = runId;
    let alive = true;
    setLoading(true);
    api
      .runEvents(runId)
      .then((p) => alive && setPage(p))
      .catch((e) => alive && setError(e))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [open, runId]);

  return (
    <div className="audit-log">
      <button type="button" className="link" onClick={() => setOpen(!open)}>
        {open ? t("audit.hideLog") : t("audit.showLog")}
      </button>
      {open && loading && <p className="waiting">{t("audit.loadingLog")}</p>}
      {open && error !== null && (
        <p className="warn">
          {errorGuidanceKey(error) ? t(errorGuidanceKey(error)!) : errorMessage(error)}
        </p>
      )}
      {open && page !== null && !page.available && (
        <p className="muted">{t("audit.logExpired")}</p>
      )}
      {open && page !== null && page.available && (
        <>
          <ol className="audit-events">
            {page.events.map((e) => (
              <li key={e.id}>
                <span className="small">
                  {new Date(e.at).toLocaleTimeString(locale, {
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                  })}
                </span>{" "}
                <code className="locator">{e.kind}</code>{" "}
                {e.activity && <span className="model">{e.activity}</span>}
                {/* The reason this panel exists: a retry is invisible in every
                    other view of a run. */}
                {e.attempt !== null && e.attempt > 1 && (
                  <strong> · {t("audit.attempt", { n: e.attempt })}</strong>
                )}
                {e.detail && <div className="muted small">{e.detail}</div>}
              </li>
            ))}
          </ol>
          {page.truncated && <p className="muted">{t("audit.logTruncated")}</p>}
          {page.events.length === 0 && <p className="muted">{t("audit.logEmpty")}</p>}
        </>
      )}
    </div>
  );
}

export function RunAudit({ runId }: { runId: string }) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const [audit, setAudit] = useState<Audit | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let alive = true;
    setAudit(null);
    setError(null);
    api
      .runAudit(runId)
      .then((a) => alive && setAudit(a))
      .catch((e) => alive && setError(e));
    return () => {
      alive = false;
    };
  }, [runId]);

  if (error !== null)
    return (
      <div className="error">
        {errorGuidanceKey(error) && <p>{t(errorGuidanceKey(error)!)}</p>}
        <pre className="detail">{errorMessage(error)}</pre>
      </div>
    );
  if (audit === null) return <p className="waiting">{t("audit.loading")}</p>;

  const { run, stages, totals, warnings } = audit;
  return (
    <div className="audit">
      <dl className="audit-head">
        <div>
          <dt>{t("audit.kind")}</dt>
          <dd>
            <span className={`dot ${runDot(run.state)}`} aria-hidden="true" />{" "}
            {t(`home.run.kind.${run.kind}`, { defaultValue: run.kind })} ·{" "}
            {t(`home.run.state.${run.state}`, { defaultValue: run.state })}
          </dd>
        </div>
        <div>
          <dt>{t("audit.started")}</dt>
          <dd>{new Date(run.startedAt).toLocaleString(locale)}</dd>
        </div>
        <div>
          <dt>{t("audit.finished")}</dt>
          <dd>
            {run.finishedAt
              ? new Date(run.finishedAt).toLocaleString(locale)
              : t("audit.stillGoing")}
          </dd>
        </div>
        <div>
          <dt>{t("audit.total")}</dt>
          <dd>
            <Money usd={totals.usd} />
          </dd>
        </div>
      </dl>

      {run.errorDetail && <pre className="detail">{run.errorDetail}</pre>}

      {warnings.length > 0 && (
        <ul className="audit-warnings">
          {warnings.map((w, i) => (
            <li className="warn" key={i}>
              {t("audit.warning", {
                profile: w.profileId ?? "—",
                other: w.collidesWith ?? "—",
              })}
              {w.detail && <div className="muted small">{w.detail}</div>}
            </li>
          ))}
        </ul>
      )}

      {stages.length === 0 ? (
        // A run that predates the trail. Said plainly rather than shown as an
        // empty table, because "nothing was recorded" and "nothing happened" are
        // different facts and only one of them is true here.
        <p className="muted">{t("audit.noTrail")}</p>
      ) : (
        <table className="audit-table">
          <thead>
            <tr>
              <th>{t("audit.col.stage")}</th>
              <th>{t("audit.col.at")}</th>
              <th>{t("audit.col.took")}</th>
              <th>{t("audit.col.cost")}</th>
              <th>{t("audit.col.produced")}</th>
            </tr>
          </thead>
          <tbody>
            {stages.map((row, i) => (
              <StageRow row={row} locale={locale} key={row.seq ?? `loose-${i}`} />
            ))}
          </tbody>
        </table>
      )}

      <p className="caveat">{t("audit.caveat")}</p>
      <RawLog runId={run.workflowId} locale={locale} />
    </div>
  );
}

/**
 * The Library's way in: which runs produced this version, and what each did.
 *
 * A version legitimately has more than one — a re-index makes a second run
 * against the same version id — so the newest is opened by default and the rest
 * are offered rather than hidden. Picking the newest silently would bury the
 * run a reader is most often looking for, which is the one *before* the last:
 * the question is usually "what changed", and that needs both.
 */
export function VersionAudit({ versionId }: { versionId: string }) {
  const { t, i18n } = useTranslation();
  const [runs, setRuns] = useState<{ id: string; kind: string; startedAt: string }[] | null>(
    null,
  );
  const [chosen, setChosen] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let alive = true;
    api
      .runsList({ versionId, limit: 10 })
      .then((page) => {
        if (!alive) return;
        setRuns(page.runs.map((r) => ({ id: r.id, kind: r.kind, startedAt: r.startedAt })));
        setChosen(page.runs[0]?.id ?? null);
      })
      .catch((e) => alive && setError(e));
    return () => {
      alive = false;
    };
  }, [versionId]);

  if (error !== null)
    return (
      <div className="error">
        {errorGuidanceKey(error) && <p>{t(errorGuidanceKey(error)!)}</p>}
        <pre className="detail">{errorMessage(error)}</pre>
      </div>
    );
  if (runs === null) return <p className="waiting">{t("audit.loading")}</p>;
  if (runs.length === 0)
    // Every version indexed before the run table carried this, and every version
    // whose run row was removed with its document. Said rather than shown blank.
    return <p className="muted">{t("audit.noRuns")}</p>;

  return (
    <div className="audit-runs">
      {runs.length > 1 && (
        <label className="field-inline">
          {t("audit.whichRun")}
          <select value={chosen ?? ""} onChange={(e) => setChosen(e.target.value)}>
            {runs.map((r) => (
              <option value={r.id} key={r.id}>
                {t(`home.run.kind.${r.kind}`, { defaultValue: r.kind })} ·{" "}
                {new Date(r.startedAt).toLocaleString(i18n.language)}
              </option>
            ))}
          </select>
        </label>
      )}
      {chosen && <RunAudit runId={chosen} />}
    </div>
  );
}
