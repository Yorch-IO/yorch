/**
 * What one indexed version holds, what it cost, and what still agrees.
 *
 * `document_detail` says which versions a document has. This says how big each
 * one is, what the extractor found in it, whether the three stores still agree
 * about it, and what it cost across *every* run that touched it — which until
 * now was answerable only through `scripts/audit_version.py`, a CLI.
 *
 * **Every leg can be unavailable on its own, and one that is carries no
 * figures.** That is the contract the whole component is written around: a
 * stopped Memgraph renders as "could not ask", naming the URL it tried, and
 * never as a version with no concepts. So nothing here reaches for a value
 * without first asking whether the leg answered, and nothing substitutes a zero
 * for an absence — the same rule Home's `Pages` applies to `page_count` and the
 * run audit applies to an aged-out history.
 *
 * Fetched on demand rather than with the document detail, because it reads
 * three stores and two artifacts. Measured at **0.52 s** on the biggest version
 * here; free either way, since nothing behind it writes or spends.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Money } from "../Money";
import {
  api,
  errorGuidanceKey,
  errorMessage,
  type RunScores,
  type StatLeg,
  type VersionStaleDiff,
  type VersionStatistics as Stats,
} from "../lib/api";

/** A figure, or a dash. Never a zero standing in for "we could not say". */
function Figure({ label, value }: { label: string; value: string | number | null }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value === null ? "—" : value}</dd>
    </div>
  );
}

/** What a leg says when it could not answer: the reason, and the URL it tried.
 *
 * Rendered instead of the figures rather than beside them. A panel showing
 * "0 concepts · could not reach the graph" invites the first half to be read
 * and the second to be skipped.
 */
function Unavailable({ leg }: { leg: StatLeg }) {
  const { t } = useTranslation();
  return (
    <p className="muted">
      {t("library.stats.unavailable")} <span className="model">{leg.detail}</span>
    </p>
  );
}

function Section({
  title,
  leg,
  children,
}: {
  title: string;
  leg: StatLeg;
  children: React.ReactNode;
}) {
  return (
    <section className="stats-block">
      <h5>{title}</h5>
      {leg.available ? children : <Unavailable leg={leg} />}
    </section>
  );
}

/** A histogram as a line of `label n` pairs, in the order the store gave them. */
function Counts({ counts }: { counts: Record<string, number> | undefined }) {
  const { t } = useTranslation();
  const entries = Object.entries(counts ?? {});
  if (entries.length === 0) return <span className="muted">—</span>;
  return (
    <span className="stats-counts">
      {entries.map(([key, n]) => (
        <span className="badge" key={key}>
          {/* The stored value is the fallback, so a kind or a status added in
              the worker renders as itself rather than as a missing key. */}
          {t(`library.stats.kind.${key}`, { defaultValue: key })} {n}
        </span>
      ))}
    </span>
  );
}

/** One three-way split between what a run made and what the graph holds.
 *
 * `leftBehind` is the defect and `missing` is its mirror — a projection that
 * did not finish — so both are shown even when they are zero, which is the one
 * place a zero is a real measurement rather than an absence.
 */
function Diff({ label, diff }: { label: string; diff: VersionStaleDiff | null | undefined }) {
  const { t } = useTranslation();
  if (!diff) return null;
  const clean = diff.leftBehind === 0 && diff.missing === 0;
  return (
    <tr>
      <th scope="row">{label}</th>
      <td>{diff.produced.toLocaleString()}</td>
      <td>{diff.inStore.toLocaleString()}</td>
      {/* `.warn` is a block rule — border-left, padding, margin — so on a
          cell it drew a stray bar beside the number. A cell wants the colour
          and nothing else. */}
      <td className={diff.leftBehind > 0 ? "off" : ""}>
        {diff.leftBehind.toLocaleString()}
      </td>
      <td className={diff.missing > 0 ? "off" : ""}>{diff.missing.toLocaleString()}</td>
      <td>
        {clean ? (
          <span className="dot dot-ok" aria-label={t("library.stats.converged")} />
        ) : (
          <span className="dot dot-warn" aria-label={t("library.stats.diverged")} />
        )}
      </td>
    </tr>
  );
}

/**
 * What this version's index can actually be asked.
 *
 * `recall_at_5_dense_only` and the noise floor are not extras: the eval
 * questions are written *from* the chunks they must find, so they leak
 * vocabulary to the lexical leg and the hybrid figure alone flatters the index;
 * the gap between the two is that leakage. And recall says how often the right
 * chunk came back while the floor says what a *wrong* one scores.
 */
function Scores({ scores }: { scores: RunScores }) {
  const { t } = useTranslation();
  const pct = (n: number) => `${(n * 100).toFixed(0)}%`;
  return (
    <dl className="scores">
      <Figure label={t("library.scores.recall5")} value={pct(scores.recallAt5)} />
      <Figure label={t("library.scores.recall1")} value={pct(scores.recallAt1)} />
      <Figure label={t("library.scores.mrr")} value={scores.mrrAt10.toFixed(3)} />
      <Figure
        label={t("library.scores.denseOnly")}
        value={pct(scores.recallAt5DenseOnly)}
      />
      <Figure
        label={t("library.scores.noiseFloor")}
        value={scores.noiseFloor.toFixed(3)}
      />
      <p className="muted basis">
        {t("library.scores.basis", {
          questions: scores.evalQuestions,
          chunks: scores.chunks,
          misses: scores.misses,
        })}
      </p>
    </dl>
  );
}

export function VersionStatistics({
  libraryId,
  versionId,
}: {
  libraryId: string;
  versionId: string;
}) {
  const { t, i18n } = useTranslation();
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let alive = true;
    setStats(null);
    setError(null);
    api
      .versionStatistics(libraryId, versionId)
      .then((s) => alive && setStats(s))
      .catch((e) => alive && setError(e));
    return () => {
      alive = false;
    };
  }, [libraryId, versionId]);

  if (error !== null)
    return (
      <div className="error">
        {errorGuidanceKey(error) && <p>{t(errorGuidanceKey(error)!)}</p>}
        <pre className="detail">{errorMessage(error)}</pre>
      </div>
    );
  if (stats === null) return <p className="waiting">{t("library.stats.loading")}</p>;

  const { catalog, structure, semantics, retrieval, ledger } = stats;
  const { graph, qdrant, artifacts } = structure;
  const date = (iso: string | null | undefined) =>
    iso ? new Date(iso).toLocaleString(i18n.language) : null;

  return (
    <div className="stats">
      <Section title={t("library.stats.structure")} leg={structure}>
        <dl className="scores">
          <Figure label={t("library.stats.chunks")} value={graph.chunks ?? null} />
          <Figure label={t("library.stats.sections")} value={graph.sections ?? null} />
          <Figure label={t("library.stats.citations")} value={graph.citations ?? null} />
          <Figure label={t("library.stats.points")} value={qdrant.points ?? null} />
          <Figure
            label={t("library.stats.bytes")}
            value={
              catalog.byteSize == null
                ? null
                : `${(catalog.byteSize / 1024 / 1024).toFixed(2)} MB`
            }
          />
          {/* A column nothing writes: `register_version` runs before
              extraction. Rendered as "not recorded" rather than as 0, so the
              figure starts working by itself the day something fills it. */}
          <Figure
            label={t("library.stats.pages")}
            value={catalog.pageCount ?? t("library.stats.unrecorded")}
          />
        </dl>
        <p className="small">
          {t("library.stats.kinds")} <Counts counts={graph.kinds} />
        </p>
        <p className="small">
          {t("library.stats.levels")} <Counts counts={graph.sectionLevels} />
        </p>
        {!graph.available && <Unavailable leg={graph} />}
        {!qdrant.available && <Unavailable leg={qdrant} />}
        {/* null is "could not compare" and only false is the claim that the
            stores disagree — so the reassuring line is shown only when all
            three actually answered. */}
        {structure.countsAgree === true && (
          <p className="small">
            <span className="dot dot-ok" /> {t("library.stats.agree")}
          </p>
        )}
        {structure.countsAgree === false && (
          <p className="warn">{t("library.stats.disagree")}</p>
        )}
        {artifacts.available && artifacts.spans && (
          <p className="small">
            {t("library.stats.spans", {
              verified: artifacts.spans.spansVerified,
              chunks: artifacts.spans.chunks,
              stream: artifacts.stream,
            })}{" "}
            {artifacts.sequence && (
              <>
                {t("library.stats.coverage", {
                  percent: (artifacts.sequence.coverage * 100).toFixed(1),
                })}
              </>
            )}
          </p>
        )}
        {!artifacts.available && <Unavailable leg={artifacts} />}
      </Section>

      <Section title={t("library.stats.semantics")} leg={semantics}>
        <dl className="scores">
          <Figure
            label={t("library.stats.concepts")}
            value={semantics.inStore?.concepts ?? null}
          />
          <Figure
            label={t("library.stats.claims")}
            value={semantics.inStore?.claims ?? null}
          />
          {/* A claim nobody can check must not look like one that can. */}
          <Figure
            label={t("library.stats.withQuote")}
            value={semantics.inStore?.withAQuote ?? null}
          />
        </dl>
        <p className="small">
          {t("library.stats.status")} <Counts counts={semantics.inStore?.byStatus} />
        </p>
        {semantics.diff?.available ? (
          <table className="audit-table stats-diff">
            <thead>
              <tr>
                <th scope="col">{t("library.stats.diff")}</th>
                <th scope="col">{t("library.stats.produced")}</th>
                <th scope="col">{t("library.stats.inStore")}</th>
                <th scope="col">{t("library.stats.leftBehind")}</th>
                <th scope="col">{t("library.stats.missing")}</th>
                <th scope="col" />
              </tr>
            </thead>
            <tbody>
              <Diff label={t("library.stats.claims")} diff={semantics.diff.claims} />
              <Diff label={t("library.stats.concepts")} diff={semantics.diff.concepts} />
              <Diff label={t("library.stats.mentions")} diff={semantics.diff.mentions} />
            </tbody>
          </table>
        ) : (
          semantics.diff && <Unavailable leg={semantics.diff} />
        )}
        {semantics.diff?.claims != null &&
          semantics.diff.concepts?.namesExtracted != null && (
            <p className="caveat">
              {t("library.stats.namesFolded", {
                names: semantics.diff.concepts.namesExtracted,
                concepts: semantics.diff.concepts.produced,
              })}
            </p>
          )}
        {(semantics.diff?.staleClaimQuotes?.quoteNoLongerLocates ?? 0) > 0 && (
          <p className="warn">
            {t("library.stats.staleQuotes", {
              count: semantics.diff!.staleClaimQuotes!.quoteNoLongerLocates,
            })}
          </p>
        )}
      </Section>

      <Section title={t("library.stats.retrieval")} leg={retrieval}>
        {retrieval.scores && <Scores scores={retrieval.scores} />}
        {retrieval.floor && (
          <p className={retrieval.floor.honest ? "small" : "warn"}>
            {t(
              retrieval.floor.honest
                ? "library.stats.floorHonest"
                : "library.stats.floorBelowNoise",
              {
                min: retrieval.floor.minScore.toFixed(3),
                noise: retrieval.floor.noiseFloor.toFixed(3),
              },
            )}
          </p>
        )}
      </Section>

      <Section title={t("library.stats.cost")} leg={ledger}>
        <table className="audit-table">
          <thead>
            <tr>
              <th scope="col">{t("library.stats.stage")}</th>
              <th scope="col">{t("library.stats.usd")}</th>
              <th scope="col">{t("library.stats.runsCharged")}</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(ledger.byStage ?? {}).map(([stage, cost]) => (
              <tr key={stage}>
                {/* The **charge** vocabulary, not the workflow's. They
                    overlap in three words out of ten — `brainworker/stages.py`
                    exists for exactly that — and `audit.stage.*` holds the
                    workflow's, which are present-continuous *progress* labels.
                    Reusing them printed "extrayendo semántica" as a line item
                    on a bill and left `correction` and `evalset` as raw ids,
                    because no workflow stage is spelled either way. */}
                <th scope="row">
                  {t(`library.stats.charge.${stage}`, { defaultValue: stage })}
                </th>
                <td>
                  <Money usd={cost.unpricedEntries > 0 && cost.usd === 0 ? null : cost.usd} />
                </td>
                <td>{cost.runs.length}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row">{t("library.stats.total")}</th>
              <td>
                <Money usd={ledger.totalUsd ?? null} />
              </td>
              <td />
            </tr>
          </tfoot>
        </table>
        {/* The finding this leg exists for. A version's bill is not one run's
            bill, and a stage charged twice is money that was spent doing
            something over. */}
        {(ledger.chargedInMoreThanOneRun?.length ?? 0) > 0 && (
          <p className="warn">
            {t("library.stats.chargedTwice", {
              // The same labels the table above uses. Joining the raw stage ids
              // here printed "embedding, semantics" beside rows reading
              // "incrustación" and "extracción semántica", so the sentence
              // named stages the reader could not find.
              stages: ledger
                .chargedInMoreThanOneRun!.map((stage) =>
                  t(`library.stats.charge.${stage}`, { defaultValue: stage }),
                )
                .join(", "),
            })}
          </p>
        )}
        <p className="small">
          {t("library.stats.byRunState")}{" "}
          <span className="stats-counts">
            {Object.entries(ledger.usdByRunState ?? {}).map(([state, usd]) => (
              <span className="badge" key={state}>
                {t(`home.run.state.${state}`, { defaultValue: state })} $
                {usd.toFixed(4)}
              </span>
            ))}
          </span>
        </p>
        <p className="caveat">{t("library.stats.priceCaveat")}</p>
      </Section>

      <Section title={t("library.stats.provenance")} leg={catalog}>
        <dl className="scores">
          <Figure label={t("library.stats.state")} value={catalog.state ?? null} />
          <Figure label={t("library.stats.runs")} value={catalog.runs ?? null} />
          <Figure label={t("library.stats.created")} value={date(catalog.createdAt)} />
          <Figure
            label={t("library.stats.activated")}
            value={date(catalog.activatedAt)}
          />
        </dl>
        <p className="small model">{catalog.contentSha256}</p>
        {(catalog.profileWarnings ?? []).map((w, i) => (
          <p className="warn" key={i}>
            {t("library.stats.profileCollision", { profile: w.collidesWith })}{" "}
            {/* `_topical_overlap` returns 0 by construction for a plain-text
                document, and 0 is the *most dangerous* case. A figure nobody can
                act on must not be printed as though it were measured. */}
            {w.comparable
              ? t("library.stats.overlap", {
                  percent: ((w.similarity ?? 0) * 100).toFixed(0),
                })
              : t("library.stats.overlapUnknown")}
          </p>
        ))}
      </Section>
    </div>
  );
}
