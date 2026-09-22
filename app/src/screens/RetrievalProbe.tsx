import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  errorGuidanceKey,
  errorMessage,
  type ProbeCandidate,
  type ProbeLeg,
  type ProbeReport,
} from "../lib/api";
import { ASK_EFFORTS, DEFAULT_ASK_EFFORT, type AskEffort } from "../lib/askEffort";

/**
 * One question through retrieval's gates, with every candidate's legs pulled
 * apart. The retrieval-testing view RAGFlow's docs prescribe as the debugging
 * order — *parsing → chunk → retrieval parameters* — over the probe this
 * product already had and could reach from no screen.
 *
 * **A sandbox, and it says so on screen.** Nothing chosen here changes what a
 * question is answered with, because there is nothing to change: the probe
 * reproduces production's own constants and widths for the level named, on
 * purpose. A debug screen whose knobs were settings would be a settings screen
 * nobody audited.
 *
 * It lives inside Explore rather than on a tab of its own because a chunk id is
 * what a probe is *about*, and Explore is where a person has one in front of
 * them: the chunk whose context is open is the one offered for placement.
 *
 * `busy` is cleared in a `finally`, for the recorded reason: a button latched
 * on a request that can fail is a panel nobody can retry.
 */
export function RetrievalProbe({
  libraryId,
  versionId,
  chunkId,
  onPick,
}: {
  libraryId: string;
  /** The document open in Explore, offered as a narrowing. */
  versionId: string | null;
  /** The chunk whose context is open, offered for placement. */
  chunkId: string | null;
  onPick?: (chunkId: string) => void;
}) {
  const { t } = useTranslation();
  const [question, setQuestion] = useState("");
  const [effort, setEffort] = useState<AskEffort>(DEFAULT_ASK_EFFORT);
  const [narrow, setNarrow] = useState(false);
  const [place, setPlace] = useState(true);
  const [report, setReport] = useState<ProbeReport | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const run = useCallback(async () => {
    const text = question.trim();
    if (!text || !libraryId) return;
    setBusy(true);
    try {
      setReport(
        await api.exploreProbe({
          libraryId,
          question: text,
          effort,
          chunkId: place && chunkId ? chunkId : undefined,
          versionId: narrow && versionId ? versionId : undefined,
        }),
      );
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }, [question, libraryId, effort, place, chunkId, narrow, versionId]);

  const guidance = error ? errorGuidanceKey(error) : undefined;

  return (
    <div className="panel probe">
      <h3>{t("probe.title")}</h3>
      <p className="intro">{t("probe.intro")}</p>
      <p className="notice sandbox">{t("probe.sandbox")}</p>

      <label className="field">
        <span>{t("probe.question")}</span>
        <textarea
          rows={2}
          value={question}
          disabled={busy}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={t("probe.placeholder")}
        />
      </label>

      <fieldset className="effort">
        <legend>{t("ask.effortLegend")}</legend>
        {ASK_EFFORTS.map((level) => (
          <label key={level}>
            <input
              type="radio"
              name="probe-effort"
              value={level}
              checked={effort === level}
              disabled={busy}
              onChange={() => setEffort(level)}
            />
            <span>{t(`ask.effort.${level}`)}</span>
          </label>
        ))}
      </fieldset>

      <div className="probe-options">
        <label className="field-inline">
          <input
            type="checkbox"
            checked={narrow && Boolean(versionId)}
            disabled={busy || !versionId}
            onChange={(e) => setNarrow(e.target.checked)}
          />
          <span>{versionId ? t("probe.narrow") : t("probe.narrowNone")}</span>
        </label>
        <label className="field-inline">
          <input
            type="checkbox"
            checked={place && Boolean(chunkId)}
            disabled={busy || !chunkId}
            onChange={(e) => setPlace(e.target.checked)}
          />
          <span>
            {chunkId ? t("probe.place", { chunk: chunkId }) : t("probe.placeNone")}
          </span>
        </label>
      </div>

      <div className="actions">
        <button onClick={() => void run()} disabled={busy || !question.trim()}>
          {busy ? t("probe.running") : t("probe.run")}
        </button>
      </div>

      {error !== null && (
        <div className="error">
          <strong>{t("probe.failed")}</strong>
          {guidance && <p>{t(guidance)}</p>}
          <p className="detail">{errorMessage(error)}</p>
        </div>
      )}

      {report && <Report report={report} onPick={onPick} />}
    </div>
  );
}

function Report({
  report,
  onPick,
}: {
  report: ProbeReport;
  onPick?: (chunkId: string) => void;
}) {
  const { t } = useTranslation();
  const served = report.servedWith;
  const delivered = report.candidates.filter((c) => c.deliveredRank !== null);

  return (
    <div className="probe-report">
      <p className="summary">
        {report.onTopic
          ? t("probe.onTopic", { dense: report.denseSupported, delivered: delivered.length })
          : t("probe.offTopic")}
        {" · "}
        {t("probe.servedWith", {
          effort: t(`ask.effort.${served.effort}`),
          topK: served.topK,
          candidates: served.candidateLimit,
          prefetch: served.prefetchLimit,
          floor: served.minScore,
        })}
        {" · "}
        {served.reranked
          ? t("probe.reranked", { model: served.rerankModel })
          : served.rerankNote
            ? t("probe.rerankSkipped", { note: served.rerankNote })
            : t("probe.notReranked")}
      </p>
      <p className="model">
        {t("probe.terms")}{" "}
        {report.queryTerms.length ? report.queryTerms.join(", ") : t("probe.noTerms")}
        {report.target?.singleTermQuery && (
          <span className="warn"> {t("probe.singleTerm")}</span>
        )}
      </p>

      {report.target && (
        <div className={report.target.verdict.reached ? "probe-verdict reached" : "probe-verdict lost"}>
          <strong>
            {report.target.verdict.reached
              ? t("probe.reached", { rank: report.target.verdict.deliveredRank, topK: served.topK })
              : t("probe.lostAt", { gate: t(`probe.gate.${report.target.verdict.lostAt}`) })}
          </strong>
          {!report.target.verdict.reached && (
            <>
              <p className="detail">{report.target.verdict.detail}</p>
              <p>{report.target.verdict.remedy}</p>
              {report.target.verdict.note && <p className="model">{report.target.verdict.note}</p>}
            </>
          )}
          {report.target.note && <p className="warn">{report.target.note}</p>}
          <table className="estimate legs">
            <thead>
              <tr>
                <th>{t("probe.leg")}</th>
                <th>{t("probe.rank")}</th>
                <th>{t("probe.score")}</th>
                <th>{t("probe.floor")}</th>
                <th>{t("probe.prefetch")}</th>
              </tr>
            </thead>
            <tbody>
              <LegRow leg={report.target.dense} />
              <LegRow leg={report.target.sparse} />
              <tr>
                <td>{t("probe.legFused")}</td>
                <td>{rank(report.target.fusedRank, served.candidateLimit)}</td>
                <td className="model">
                  {report.target.rerankScore !== null
                    ? t("probe.rerankScore", { score: report.target.rerankScore.toFixed(3) })
                    : "—"}
                </td>
                <td>—</td>
                <td>—</td>
              </tr>
            </tbody>
          </table>
          <p className="model">{t("probe.approximate")}</p>
        </div>
      )}

      <h4>{t("probe.candidates", { count: report.candidates.length })}</h4>
      {report.candidates.length === 0 ? (
        <p className="notice">{t("probe.noCandidates")}</p>
      ) : (
        <table className="estimate candidates">
          <thead>
            <tr>
              <th>#</th>
              <th>{t("probe.delivered")}</th>
              <th>{t("probe.legDense")}</th>
              <th>{t("probe.legSparse")}</th>
              <th>{t("probe.legRerank")}</th>
              <th>{t("probe.passage")}</th>
            </tr>
          </thead>
          <tbody>
            {report.candidates.map((c) => (
              <CandidateRow key={c.chunkId} c={c} onPick={onPick} />
            ))}
          </tbody>
        </table>
      )}

      <p className="model spent">
        {t("probe.spent", {
          tokens: report.spent.embeddingInputTokens,
          hits: report.spent.cacheHits,
          usd: report.spent.rerankUsd.toFixed(4),
        })}
      </p>
    </div>
  );
}

/** "496~ of 500", or "> 500" when the leg never ranked it — the window travels
 *  with the rank because an HNSW rank moves by a place or two with the depth. */
function rank(value: number | null, searched: number): string {
  return value === null ? `> ${searched}` : `${value}~ / ${searched}`;
}

function LegRow({ leg }: { leg: ProbeLeg }) {
  const { t } = useTranslation();
  return (
    <tr>
      <td>{t(leg.leg === "dense" ? "probe.legDense" : "probe.legSparse")}</td>
      <td>{rank(leg.rank, leg.searched)}</td>
      <td className="model">{leg.score === null ? "—" : leg.score.toFixed(4)}</td>
      <td>
        {leg.floor === null
          ? "—"
          : leg.clearsFloor
            ? t("probe.clears", { floor: leg.floor })
            : t("probe.refused", { floor: leg.floor })}
      </td>
      <td>{leg.inPrefetch ? t("probe.inPrefetch") : t("probe.outPrefetch", { width: leg.prefetchLimit })}</td>
    </tr>
  );
}

function CandidateRow({
  c,
  onPick,
}: {
  c: ProbeCandidate;
  onPick?: (chunkId: string) => void;
}) {
  const { t } = useTranslation();
  const num = (v: number | null, digits: number) => (v === null ? "—" : v.toFixed(digits));
  return (
    <tr className={c.deliveredRank !== null ? "delivered" : "dropped"}>
      <td>{c.rank}</td>
      <td>{c.deliveredRank ?? t("probe.dropped")}</td>
      <td className="model">
        {c.denseRank ?? "—"} · {num(c.denseScore, 3)}
      </td>
      <td className="model">
        {c.sparseRank ?? "—"} · {num(c.sparseScore, 2)}
      </td>
      <td className="model">{num(c.rerankScore, 3)}</td>
      <td>
        <button className="link" onClick={() => onPick?.(c.chunkId)} disabled={!onPick}>
          {c.source ?? c.chunkId}
        </button>
        {c.breadcrumb && <span className="model"> · {c.breadcrumb}</span>}
        <p className="chunk-text preview">{c.preview}</p>
      </td>
    </tr>
  );
}
