import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  DEFAULT_STAGES,
  MANIFEST_FIELDS,
  api,
  errorGuidanceKey,
  errorMessage,
  type BucketDetail,
  type BucketSource,
  type StageOptions,
  type Transcriber,
  type VideoGateReport,
} from "../lib/api";
import {
  NO_FILTERS,
  PROBE_POLL_MS,
  approvalOptions,
  awaiting,
  buildRows,
  cost,
  duration,
  filterRows,
  quotable,
  size,
  sources,
  toApprove,
  toProbe,
  undecodable,
  toggle,
  totals,
  type Filters,
  type Row,
} from "../lib/bucket";
import { useBuckets } from "../lib/buckets";
import { useLocalTranscriber } from "../lib/localTranscriber";
import { hoursFor, speedLabel } from "../lib/transcribing";
import { Cost } from "../Money";

/** A bucket run has no profile, eval-set or tuning stage, and correction is
 *  decided by the gate (off: Transcribe punctuates its own output). What is
 *  left for a person to tick is semantics, which nearly doubles the bill and
 *  is what puts a recording on the Graph screen. Ticked *before* the probes,
 *  never after, so each gate quotes the choice rather than being approved
 *  past it. */
const AUDIO_STAGES: Partial<StageOptions> = {
  learnProfile: false,
  generateEvalset: false,
  tune: false,
  ignoreProfile: false,
  // Off unless a person ticks it, and **sent with the probe** rather than
  // decided at the gate: the estimate is built from the switches the run was
  // started with, so correction ticked afterwards would be a stage nobody was
  // shown a price for. A machine transcript arrives punctuated, which is why
  // off is the default rather than the only option — on a 1993 cassette the
  // repair is worth more than on a studio recording, and nobody has measured
  // which.
  correct: false,
};

/** Who transcribes. Decided **per batch, before quoting**, because the quote
 *  is per run: a gate that said $1.90 and a run that was then transcribed for
 *  nothing would be a receipt for work nobody did, and the reverse — quoting
 *  $0 and billing Amazon — is the failure this product refuses outright. */
const TRANSCRIBERS: Transcriber[] = ["transcribe", "local"];

const EMPTY_SOURCE: BucketSource = {
  bucket: "",
  prefix: "",
  role_arn: "",
  region: "",
  archive_prefix: "transcripciones/",
  correction_prefix: "",
  manifest_key: "",
  manifest_map: {},
  language: "es-US",
};

/**
 * Audio out of a customer's own S3 bucket: catalogue it, quote it, approve it.
 *
 * The bucket is picked — and re-synced — in the top bar, because a bucket *is*
 * a library and that is where the library is picked on every other tab. What
 * is left here is the register form for a bucket the plane has not seen, and
 * three acts over the objects of one it has: tick, quote, approve. Each button
 * is a decision somebody makes with a figure in front of them.
 *
 * **Quoting is free and approving is not.** A quote starts one ordinary
 * `audio` run per ticked object, each parked at its own seven-day gate with
 * its own `estimate.json`; this screen adds the figures up and answers the
 * ticked gates through `ingestApprove`, unchanged. The sum is the number the
 * person approves, and it is exact: the plane read every object's duration
 * from its own headers, and the rate is published.
 *
 * **Paid plane only.** Local mode renders the explanation and nothing else.
 */
export function BucketScreen() {
  const { t } = useTranslation();
  const { rows: buckets, selected: bucketId, register, forget, syncing, unavailable, news } =
    useBuckets();

  const [detail, setDetail] = useState<BucketDetail | null>(null);
  const [gates, setGates] = useState<Record<string, VideoGateReport>>({});
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [semantics, setSemantics] = useState(false);
  const [correct, setCorrect] = useState(false);
  const [reindex, setReindex] = useState(false);
  const [transcriber, setTranscriber] = useState<Transcriber>("transcribe");
  const local = useLocalTranscriber();
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [outcome, setOutcome] = useState<{ started: number; failed: string[] } | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Changing bucket throws away everything derived from the previous one.
  useEffect(() => {
    setDetail(null);
    setGates({});
    setPicked(new Set());
    setFilters(NO_FILTERS);
    setOutcome(null);
  }, [bucketId]);

  const load = useCallback(async () => {
    if (!bucketId) {
      setDetail(null);
      return;
    }
    try {
      const d = await api.bucketDetail(bucketId);
      if (alive.current) setDetail(d);
    } catch (e) {
      if (alive.current) setError(e);
    }
  }, [bucketId]);

  // The detail, on a bucket change and again after each sync of it.
  useEffect(() => {
    void load();
  }, [load, news]);

  const rows: Row[] = useMemo(() => buildRows(detail?.objects ?? [], gates), [detail, gates]);
  const visible = useMemo(() => filterRows(rows, filters), [rows, filters]);
  const sourceNames = useMemo(() => sources(rows), [rows]);

  /** Poll the parked probes until each has published its gate. A gate
   *  already read is not re-asked — a run parked for seven days cannot change
   *  its report — and a failure to read one is swallowed so the rest arrive;
   *  the import queue is where a failed run is chased. */
  useEffect(() => {
    const pending = rows
      .filter((r) => r.object.runId !== null && r.object.state === "pending" && r.gate === null)
      .map((r) => r.object.runId!);
    if (pending.length === 0) return;
    let stop = false;
    const tick = async () => {
      for (const workflowId of pending) {
        try {
          const report = await api.audioGate(workflowId);
          if (stop || report === null) continue;
          setGates((current) => ({ ...current, [workflowId]: report }));
        } catch {
          /* chased in the import queue */
        }
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), PROBE_POLL_MS);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [rows]);

  const guard = async (label: string, fn: () => Promise<void>) => {
    setBusy(true);
    setStep(label);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
      setStep("");
    }
  };

  const stages = (): StageOptions => ({
    ...DEFAULT_STAGES,
    ...AUDIO_STAGES,
    extractSemantics: semantics,
    correct,
  });

  const probe = () =>
    guard("probe", async () => {
      if (!bucketId) return;
      const keys = toProbe(visible, picked, reindex);
      if (keys.length === 0) return;
      const result = await api.bucketProbe(bucketId, keys, stages(), reindex, transcriber);
      setOutcome({
        started: result.started.length,
        failed: result.failed.map((f) => `${f.key}: ${f.kind}`),
      });
      await load();
    });

  const approve = () =>
    guard("approve", async () => {
      const chosen = stages();
      // Sequentially, with per-row failure collection — the `ImportScreen`
      // loop. A half-finished batch leaves every remaining run parked rather
      // than lost.
      const failed: string[] = [];
      for (const row of toApprove(visible, picked)) {
        try {
          await api.ingestApprove(row.object.runId!, {
            approved: true,
            options: approvalOptions(row, chosen),
            reason: "",
          });
        } catch (e) {
          failed.push(`${row.object.key}: ${errorMessage(e)}`);
        }
      }
      setOutcome({ started: 0, failed });
      setGates({});
      setPicked(new Set());
      await load();
    });

  const reject = () =>
    guard("reject", async () => {
      for (const row of toApprove(visible, picked)) {
        try {
          await api.ingestApprove(row.object.runId!, {
            approved: false,
            options: stages(),
            reason: t("bucket.notChosen"),
          });
        } catch {
          /* the queue shows what stayed parked */
        }
      }
      setGates({});
      setPicked(new Set());
      await load();
    });

  const summary = totals(visible, picked);
  // What the plane will send to Amazon whatever is ticked here, said before
  // the button rather than afterwards in a per-key line of the answer.
  const amazonAnyway = undecodable(visible, picked, local.status?.decodable ?? []);
  const hoursHere = hoursFor(summary.seconds, local.status?.measuredSpeed ?? null);
  // The fact if the binary has been asked, the build's own hint until then.
  const localDevice = local.status?.device?.backend ?? local.status?.backend ?? "none";
  const runsOnGpu = localDevice !== "cpu" && localDevice !== "blas" && localDevice !== "none";
  const probable = toProbe(visible, picked, reindex).length;
  const approvable = toApprove(visible, picked).length;
  const pickable = visible.filter((r) => quotable(r, reindex) || awaiting(r));

  if (unavailable) {
    return (
      <section className="screen">
        <h2>{t("bucket.title")}</h2>
        <p className="notice">{t("bucket.paidOnly")}</p>
      </section>
    );
  }

  return (
    <section className="screen">
      <h2>{t("bucket.title")}</h2>
      <p className="intro">{t("bucket.intro")}</p>

      {error !== null && (
        <div className="error">
          <strong>{t("error.title")}</strong>
          {errorGuidanceKey(error) && <p>{t(errorGuidanceKey(error)!)}</p>}
          <pre className="detail">{errorMessage(error)}</pre>
          <button type="button" onClick={() => setError(null)}>
            {t("error.dismiss")}
          </button>
        </div>
      )}

      {(buckets.length === 0 || adding) && (
        <RegisterForm
          busy={syncing}
          onCancel={buckets.length > 0 ? () => setAdding(false) : undefined}
          onSubmit={async (source, name, libraryId) => {
            const synced = await register(source, name, libraryId);
            if (synced) setAdding(false);
          }}
        />
      )}

      {buckets.length > 0 && !adding && (
        <div className="actions">
          <button type="button" className="link" onClick={() => setAdding(true)}>
            {t("bucket.add")}
          </button>
          {bucketId && (
            <button
              type="button"
              className="link"
              disabled={busy}
              onClick={() => {
                if (window.confirm(t("bucket.forgetConfirm"))) void forget(bucketId);
              }}
            >
              {t("bucket.forget")}
            </button>
          )}
        </div>
      )}

      {detail && (
        <>
          <BucketSummary detail={detail} />

          <div className="panel">
            <BucketFilters
              filters={filters}
              sourceNames={sourceNames}
              onChange={setFilters}
              shown={visible.length}
              total={rows.length}
            />
            <div className="channel-bulk">
              <button
                type="button"
                disabled={busy || pickable.length === 0}
                onClick={() => setPicked(new Set(pickable.map((r) => r.object.key)))}
              >
                {t("bucket.pickAll", { count: pickable.length })}
              </button>
              <button
                type="button"
                disabled={busy || picked.size === 0}
                onClick={() => setPicked(new Set())}
              >
                {t("bucket.pickNone")}
              </button>
            </div>
            <ObjectsTable
              rows={visible}
              picked={picked}
              busy={busy}
              reindex={reindex}
              onToggle={(key) => setPicked((p) => toggle(p, key))}
            />
            <dl className="preview channel-total">
              <dt>{t("bucket.selected")}</dt>
              <dd>{summary.count}</dd>
              <dt>{t("bucket.selectedMinutes")}</dt>
              <dd>
                {t("bucket.minutes", { count: Math.round(summary.seconds / 60) })}
                {summary.estimated > 0 && (
                  <span className="warn-inline">
                    {" "}
                    {t("bucket.minutesEstimated", { count: summary.estimated })}
                  </span>
                )}
              </dd>
              <dt>{t("bucket.totalCost")}</dt>
              <dd>
                <Cost usd={summary.usd} high={summary.usdHigh} />
              </dd>
              {summary.transcription !== null && (
                <>
                  <dt>{t("bucket.transcriptionCost")}</dt>
                  <dd>
                    <Cost usd={summary.transcription} high={summary.transcription} />
                  </dd>
                </>
              )}
            </dl>
            {summary.unpriced > 0 && (
              <p className="warn">{t("bucket.unpriced", { count: summary.unpriced })}</p>
            )}
          </div>

          <div className="panel">
            <h3>{t("bucket.quote")}</h3>
            <p className="caveat">{t("bucket.quoteCaveat")}</p>
            <fieldset className="stages">
              <legend>{t("bucket.stages")}</legend>
              <label htmlFor="bucket-semantics">
                <input
                  id="bucket-semantics"
                  type="checkbox"
                  checked={semantics}
                  disabled={busy}
                  onChange={() => setSemantics((s) => !s)}
                />
                <span>{t("bucket.stageSemantics")}</span>
              </label>
              <label htmlFor="bucket-correct">
                <input
                  id="bucket-correct"
                  type="checkbox"
                  checked={correct}
                  disabled={busy}
                  onChange={() => setCorrect((c) => !c)}
                />
                <span>{t("bucket.stageCorrect")}</span>
              </label>
              <label htmlFor="bucket-reindex">
                <input
                  id="bucket-reindex"
                  type="checkbox"
                  checked={reindex}
                  disabled={busy}
                  onChange={() => setReindex((r) => !r)}
                />
                <span>{t("bucket.reindex")}</span>
              </label>
            </fieldset>
            <p className="caveat">{t("bucket.stageSemanticsCaveat")}</p>
            {correct && <p className="caveat">{t("bucket.stageCorrectCaveat")}</p>}

            {/* The engine, beside the switches and above the button, because
                it is decided for the batch and cannot be changed at the gate:
                each run is quoted for the engine it is on. */}
            <fieldset className="stages">
              <legend>{t("bucket.transcriber")}</legend>
              {TRANSCRIBERS.map((engine) => (
                <label key={engine} htmlFor={`bucket-engine-${engine}`}>
                  <input
                    id={`bucket-engine-${engine}`}
                    type="radio"
                    name="bucket-engine"
                    checked={transcriber === engine}
                    disabled={busy || (engine === "local" && local.readiness !== "ready")}
                    onChange={() => setTranscriber(engine)}
                  />
                  <span>{t(`bucket.transcribers.${engine}`)}</span>
                  {/* Which device, on the line where the choice is made. The
                      panel on Services explains it; here it is one word,
                      because "this machine" means something very different at
                      1.6x and at 15x, and the difference is what somebody is
                      weighing against Amazon's price. */}
                  {engine === "local" && local.status?.installed && (
                    <span className={runsOnGpu ? "badge badge-ok" : "badge"}>
                      {t(`whisper.backends.${localDevice}`, { defaultValue: localDevice })}
                      {local.status.device === null && ` · ${t("bucket.deviceGuess")}`}
                    </span>
                  )}
                </label>
              ))}
            </fieldset>
            {transcriber === "local" ? (
              <p className="caveat">
                {t("bucket.localCaveat")}
                {hoursHere !== null
                  ? ` ${t("bucket.localHours", {
                      hours: hoursHere.toFixed(1),
                      speed: speedLabel(local.status?.measuredSpeed ?? null),
                    })}`
                  : ` ${t("bucket.localUnmeasured")}`}
              </p>
            ) : (
              <p className="caveat">{t("bucket.amazonCaveat")}</p>
            )}
            {local.readiness !== "ready" && (
              <p className="caveat">{t(`bucket.local.${local.readiness}`)}</p>
            )}
            {transcriber === "local" && amazonAnyway > 0 && (
              <p className="warn">{t("bucket.localUndecodable", { count: amazonAnyway })}</p>
            )}

            <div className="actions">
              <button type="button" disabled={busy || probable === 0} onClick={probe}>
                {busy && step === "probe"
                  ? t("bucket.quoting")
                  : t("bucket.quoteStart", { count: probable })}
              </button>
              <button type="button" disabled={busy || approvable === 0} onClick={approve}>
                {busy && step === "approve"
                  ? t("bucket.approving")
                  : t("bucket.approveStart", { count: approvable })}
              </button>
              <button
                type="button"
                className="link"
                disabled={busy || approvable === 0}
                onClick={reject}
              >
                {t("bucket.rejectStart", { count: approvable })}
              </button>
            </div>
            <p className="caveat">{t("bucket.approveCaveat")}</p>
            {outcome !== null && (
              <p className={outcome.failed.length > 0 ? "warn" : "muted"}>
                {outcome.started > 0 && t("bucket.quoted", { count: outcome.started })}
                {outcome.failed.length > 0 && (
                  <>
                    {" "}
                    {t("bucket.failed", { count: outcome.failed.length })}: {outcome.failed.join("; ")}
                  </>
                )}
              </p>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function BucketSummary({ detail }: { detail: BucketDetail }) {
  const { t } = useTranslation();
  const { bucket, totals: counts } = detail;
  return (
    <div className="panel">
      <dl className="preview">
        <dt>{t("bucket.source")}</dt>
        <dd>
          <code>{bucket.libraryName}</code>
        </dd>
        <dt>{t("bucket.objects")}</dt>
        <dd>
          {counts.objects} · {t("bucket.minutes", { count: Math.round(counts.seconds / 60) })}
        </dd>
        <dt>{t("bucket.states")}</dt>
        <dd>
          {t("bucket.stateIndexed", { count: counts.indexed })} ·{" "}
          {t("bucket.statePending", { count: counts.pending })} ·{" "}
          {t("bucket.stateUnindexed", { count: counts.unindexed })}
        </dd>
        {bucket.source.manifest_key && (
          <>
            <dt>{t("bucket.manifest")}</dt>
            <dd>
              <code>{bucket.source.manifest_key}</code> ·{" "}
              {t("bucket.manifestJoined", {
                rows: bucket.manifestRows,
                unmatched: bucket.unmatchedObjects,
              })}
            </dd>
          </>
        )}
        {bucket.source.archive_prefix ? (
          <>
            <dt>{t("bucket.archive")}</dt>
            <dd>
              <code>{bucket.source.archive_prefix}</code>
            </dd>
          </>
        ) : null}
      </dl>
      {bucket.estimated > 0 && (
        <p className="warn">{t("bucket.estimatedCaveat", { count: bucket.estimated })}</p>
      )}
      {!bucket.complete && <p className="notice">{t("bucket.partialCaveat")}</p>}
      {bucket.warnings.map((w) => (
        <p key={w} className="warn">
          {w}
        </p>
      ))}
    </div>
  );
}

function BucketFilters({
  filters,
  sourceNames,
  onChange,
  shown,
  total,
}: {
  filters: Filters;
  sourceNames: string[];
  onChange: (next: Filters) => void;
  shown: number;
  total: number;
}) {
  const { t } = useTranslation();
  const set = (patch: Partial<Filters>) => onChange({ ...filters, ...patch });
  return (
    <div className="bucket-filters">
      <input
        type="search"
        value={filters.text}
        placeholder={t("bucket.filterText")}
        aria-label={t("bucket.filterText")}
        onChange={(e) => set({ text: e.target.value })}
      />
      {sourceNames.length > 1 && (
        <select
          value={filters.source}
          aria-label={t("bucket.filterSource")}
          onChange={(e) => set({ source: e.target.value })}
        >
          <option value="">{t("bucket.everySource")}</option>
          {sourceNames.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      )}
      <select
        value={filters.state}
        aria-label={t("bucket.filterState")}
        onChange={(e) => set({ state: e.target.value })}
      >
        {["all", "unindexed", "awaiting", "pending", "indexed"].map((s) => (
          <option key={s} value={s}>
            {t(`bucket.filter.${s}`)}
          </option>
        ))}
      </select>
      <input
        type="text"
        inputMode="numeric"
        value={filters.yearFrom}
        placeholder={t("bucket.yearFrom")}
        aria-label={t("bucket.yearFrom")}
        size={6}
        onChange={(e) => set({ yearFrom: e.target.value.trim() })}
      />
      <input
        type="text"
        inputMode="numeric"
        value={filters.yearTo}
        placeholder={t("bucket.yearTo")}
        aria-label={t("bucket.yearTo")}
        size={6}
        onChange={(e) => set({ yearTo: e.target.value.trim() })}
      />
      {shown !== total && (
        <span className="muted">{t("bucket.filtered", { shown, total })}</span>
      )}
    </div>
  );
}

function ObjectsTable({
  rows,
  picked,
  busy,
  reindex,
  onToggle,
}: {
  rows: Row[];
  picked: Set<string>;
  busy: boolean;
  reindex: boolean;
  onToggle: (key: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <table className="candidates">
      <thead>
        <tr>
          <th />
          <th>{t("bucket.recording")}</th>
          <th>{t("bucket.state")}</th>
          <th>{t("bucket.cost")}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <ObjectRow
            key={row.object.key}
            row={row}
            checked={picked.has(row.object.key)}
            busy={busy}
            reindex={reindex}
            onToggle={() => onToggle(row.object.key)}
          />
        ))}
      </tbody>
    </table>
  );
}

function ObjectRow({
  row,
  checked,
  busy,
  reindex,
  onToggle,
}: {
  row: Row;
  checked: boolean;
  busy: boolean;
  reindex: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const o = row.object;
  const quoted = cost(row);
  const selectable = quotable(row, reindex) || awaiting(row);
  const flags = o.warnings.filter(
    (w) => w.startsWith("extension_lies") || w === "container_unknown" ||
      w.startsWith("too_") || w === "object_changed_since_sync",
  );
  return (
    <tr className={!o.available || (o.state === "indexed" && !reindex) ? "muted" : ""}>
      <td>
        <input
          type="checkbox"
          checked={checked}
          disabled={busy || !selectable}
          aria-label={o.title}
          onChange={onToggle}
        />
      </td>
      <td>
        <span className="candidate-title">{o.title}</span>
        <span className="candidate-meta">
          {duration(o.durationS)}
          {o.durationEstimated && (
            <span className="warn-inline"> {t("bucket.estimated")}</span>
          )}
          {o.recordedAt ? ` · ${o.recordedAt}` : ""}
          {o.source ? ` · ${o.source}` : ""}
          {` · ${size(o.size)}`}
          {o.container ? ` · ${o.container}` : ""}
        </span>
        <span className="candidate-meta">
          <code>{o.key}</code>
        </span>
        {flags.length > 0 && (
          <span className="candidate-state warn-inline">{flags.join(" · ")}</span>
        )}
        {!o.available && (
          <span className="candidate-state warn-inline">{t("bucket.unavailable")}</span>
        )}
      </td>
      <td>
        <span className={`verdict state-${o.state}`}>{t(`bucket.filter.${o.state}`)}</span>
        {row.gate !== null && (
          <span className="candidate-meta">{t("bucket.awaitingApproval")}</span>
        )}
        {o.runId !== null && row.gate === null && o.state === "pending" && (
          <span className="candidate-meta">{o.runState ?? t("bucket.probing")}</span>
        )}
      </td>
      <td>
        {row.gate === null ? (
          <span className="muted">—</span>
        ) : (
          <Cost usd={quoted.usd} high={quoted.high} />
        )}
      </td>
    </tr>
  );
}

/**
 * The register form. Everything the plane needs to read a bucket, and
 * nothing secret: the role is the customer's and its trust policy is the gate.
 * The manifest mapping is seven optional boxes, each naming a column of the
 * customer's own CSV — the whole point of the mapping is that nothing in the
 * product knows what those columns are called.
 */
export function RegisterForm({
  busy,
  onSubmit,
  onCancel,
}: {
  busy: boolean;
  onSubmit: (source: BucketSource, libraryName: string, libraryId: string) => Promise<void>;
  onCancel?: () => void;
}) {
  const { t } = useTranslation();
  const [source, setSource] = useState<BucketSource>(EMPTY_SOURCE);
  const [name, setName] = useState("");
  // Empty derives `lib_s3_<hash>`, which is the default and the right one for
  // a corpus that stands on its own. Naming an existing library puts the
  // recordings on that shelf instead: one graph and one retrieval scope with
  // whatever is already there, at the cost of no longer being able to ask the
  // recordings by themselves.
  const [libraryId, setLibraryId] = useState("");
  const set = (patch: Partial<BucketSource>) => setSource((s) => ({ ...s, ...patch }));
  const setMap = (field: string, column: string) =>
    setSource((s) => {
      const manifest_map = { ...s.manifest_map };
      if (column.trim()) manifest_map[field] = column.trim();
      else delete manifest_map[field];
      return { ...s, manifest_map };
    });
  const ready = source.bucket.trim().length >= 3;

  return (
    <form
      className="panel bucket-register"
      onSubmit={(e) => {
        e.preventDefault();
        if (!ready) return;
        void onSubmit(
          { ...source, bucket: source.bucket.trim() },
          name.trim(),
          libraryId.trim(),
        );
      }}
    >
      <h3>{t("bucket.registerTitle")}</h3>
      <p className="caveat">{t("bucket.registerCaveat")}</p>
      <label className="field">
        <span>{t("bucket.bucket")}</span>
        <input
          type="text"
          value={source.bucket}
          placeholder="my-archive-bucket"
          onChange={(e) => set({ bucket: e.target.value })}
        />
      </label>
      <label className="field">
        <span>{t("bucket.prefix")}</span>
        <input
          type="text"
          value={source.prefix}
          placeholder="audios/"
          onChange={(e) => set({ prefix: e.target.value })}
        />
      </label>
      <label className="field">
        <span>{t("bucket.roleArn")}</span>
        <input
          type="text"
          value={source.role_arn}
          placeholder="arn:aws:iam::123456789012:role/company-brain-reader"
          onChange={(e) => set({ role_arn: e.target.value })}
        />
      </label>
      <p className="caveat">{t("bucket.roleCaveat")}</p>
      <label className="field">
        <span>{t("bucket.libraryName")}</span>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">
        <span>{t("bucket.libraryId")}</span>
        <input
          type="text"
          value={libraryId}
          placeholder={t("bucket.libraryIdPlaceholder")}
          onChange={(e) => setLibraryId(e.target.value)}
        />
      </label>
      <p className="caveat">{t("bucket.libraryIdCaveat")}</p>
      <label className="field">
        <span>{t("bucket.correctionPrefix")}</span>
        <input
          type="text"
          value={source.correction_prefix}
          placeholder="correcciones/"
          onChange={(e) => set({ correction_prefix: e.target.value })}
        />
      </label>
      <p className="caveat">{t("bucket.correctionCaveat")}</p>
      <label className="field">
        <span>{t("bucket.language")}</span>
        <select value={source.language} onChange={(e) => set({ language: e.target.value })}>
          {["es-US", "es-ES", "en-US", "pt-BR"].map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>{t("bucket.archivePrefix")}</span>
        <input
          type="text"
          value={source.archive_prefix}
          onChange={(e) => set({ archive_prefix: e.target.value })}
        />
      </label>
      <p className="caveat">{t("bucket.archiveCaveat")}</p>
      <fieldset className="stages">
        <legend>{t("bucket.manifest")}</legend>
        <label className="field">
          <span>{t("bucket.manifestKey")}</span>
          <input
            type="text"
            value={source.manifest_key}
            placeholder="metadatos/manifiesto.csv"
            onChange={(e) => set({ manifest_key: e.target.value })}
          />
        </label>
        <p className="caveat">{t("bucket.manifestCaveat")}</p>
        {MANIFEST_FIELDS.map((field) => (
          <label key={field} className="field field-inline">
            <span>{t(`bucket.manifestField.${field}`)}</span>
            <input
              type="text"
              value={source.manifest_map[field] ?? ""}
              onChange={(e) => setMap(field, e.target.value)}
            />
          </label>
        ))}
      </fieldset>
      <div className="actions">
        <button type="submit" disabled={busy || !ready}>
          {busy ? t("bucket.syncing") : t("bucket.register")}
        </button>
        {onCancel && (
          <button type="button" className="link" disabled={busy} onClick={onCancel}>
            {t("bucket.cancel")}
          </button>
        )}
      </div>
    </form>
  );
}
