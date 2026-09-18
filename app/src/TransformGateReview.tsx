import { useState } from "react";
import { useTranslation } from "react-i18next";

import type {
  TransformGateReport,
  TransformOptions,
  TransformPlanReport,
} from "./lib/api";
import { EstimateTable } from "./GateReview";

/**
 * A transformation's two gates, as two components.
 *
 * Two, and not one with a branch, because they answer different questions with
 * different numbers and the failure of sharing is on the record: in the ingest
 * path one report object is assigned before the first gate and never cleared,
 * so the second gate serves the first one's preview — a reader was shown
 * "Nothing has been paid for yet" over a run that had already spent $0.58, above
 * a chunk count measured before the correction that changed every offset. Two
 * types, two routes, two components; nothing to reassign.
 */

/**
 * The **first** gate: a quote made from what is knowable for free.
 *
 * The chapter count here is a projection — arithmetic over the source's own
 * chapters — and the panel says so in as many words. A projected figure that is
 * not labelled as one is worse than no figure, because the moment somebody
 * decides is exactly the moment a number is taken literally.
 */
export function TransformQuote({
  report,
  options,
  busy,
  onDecide,
}: {
  report: TransformGateReport;
  options: TransformOptions;
  busy: boolean;
  onDecide: (approved: boolean, options: TransformOptions) => void;
}) {
  const { t } = useTranslation();
  const [reviewPlan, setReviewPlan] = useState(options.reviewPlan);
  const [research, setResearch] = useState(
    options.research && report.researchBudget > 0,
  );

  return (
    <div className="gate">
      <h3>{t("transform.gate.title")}</h3>
      <p className="muted">
        {t("transform.gate.summary", {
          genre: t(`transform.genre.${report.genre}`, {
            defaultValue: report.genre,
          }),
          mode: t(`transform.mode.${report.mode}`, { defaultValue: report.mode }),
          chapters: report.projectedChapters,
          characters: report.characters,
        })}
      </p>
      {report.projection && (
        <p className="caveat">{t("transform.gate.projection")}</p>
      )}

      {/* Zero is a real answer here and is printed as one. A library where
          nothing clears the similarity floor for this document genuinely has
          nothing to add, and saying so beats a research switch that would spend
          money to be told the same thing once per chapter. */}
      <p className="muted small">
        {report.researchBudget > 0
          ? t("transform.gate.research", {
              queries: report.researchBudget,
              supported: report.supported,
            })
          : t("transform.gate.noResearch")}
      </p>

      <fieldset className="stages">
        <label>
          <input
            type="checkbox"
            checked={research}
            disabled={report.researchBudget === 0}
            onChange={(e) => setResearch(e.target.checked)}
          />
          {t("transform.option.research")}
        </label>
        <label>
          <input
            type="checkbox"
            checked={reviewPlan}
            onChange={(e) => setReviewPlan(e.target.checked)}
          />
          {t("transform.option.reviewPlan")}
        </label>
      </fieldset>

      {report.estimate && (
        <>
          <EstimateTable estimate={report.estimate} />
          <p className="caveat">{report.estimate.priceSource}</p>
        </>
      )}

      <div className="actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => onDecide(true, { reviewPlan, research })}
        >
          {t("transform.gate.approve")}
        </button>
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={() => onDecide(false, { reviewPlan, research })}
        >
          {t("transform.gate.cancel")}
        </button>
      </div>
    </div>
  );
}

/**
 * The **second** gate: the outline, and the first quote anybody should act on.
 *
 * What it shows that the first cannot: the real chapter count, the chapters
 * themselves, how much of the source no chapter claims, and what has been spent
 * getting here. `spentSoFar` is the number the ingest path's second gate got
 * wrong by printing the first gate's — so it is rendered plainly and from this
 * report only.
 */
export function TransformPlanReview({
  report,
  options,
  busy,
  onDecide,
}: {
  report: TransformPlanReport;
  options: TransformOptions;
  busy: boolean;
  onDecide: (approved: boolean, options: TransformOptions) => void;
}) {
  const { t } = useTranslation();
  const [research, setResearch] = useState(
    options.research && report.researchBudget > 0,
  );

  return (
    <div className="gate">
      <h3>{t("transform.plan.title")}</h3>
      <p className="muted">
        {t("transform.plan.summary", { chapters: report.chapters.length })}
      </p>

      {/* A fallback outline is not a failure and must not read as one: after
          three refused proposals the source's own chapters are adopted, which
          covers the document by construction. But it *is* a fact about this
          run, and a reader deciding whether to pay for it should have it. */}
      {report.fallback && (
        <p className="caveat">{t("transform.plan.fallback")}</p>
      )}

      {report.uncoveredFraction > 0 && (
        <p className="caveat">
          {t("transform.plan.uncovered", {
            percent: Math.round(report.uncoveredFraction * 100),
          })}
        </p>
      )}

      <ol className="chapters">
        {report.chapters.map((c) => (
          <li key={c.ordinal}>
            <strong>{c.title}</strong>
            {c.intent && <> — {c.intent}</>}
          </li>
        ))}
      </ol>

      <p className="muted small">
        {report.researchBudget > 0
          ? t("transform.plan.research", { queries: report.researchBudget })
          : t("transform.gate.noResearch")}
        {report.spentSoFar !== null && (
          <> {t("transform.plan.spent", { usd: report.spentSoFar.toFixed(4) })}</>
        )}
      </p>

      <fieldset className="stages">
        <label>
          <input
            type="checkbox"
            checked={research}
            disabled={report.researchBudget === 0}
            onChange={(e) => setResearch(e.target.checked)}
          />
          {t("transform.option.research")}
        </label>
      </fieldset>

      {report.estimate && (
        <>
          <EstimateTable estimate={report.estimate} />
          <p className="caveat">{report.estimate.priceSource}</p>
        </>
      )}

      <div className="actions">
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            onDecide(true, { reviewPlan: options.reviewPlan, research })
          }
        >
          {t("transform.plan.approve")}
        </button>
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={() =>
            onDecide(false, { reviewPlan: options.reviewPlan, research })
          }
        >
          {t("transform.gate.cancel")}
        </button>
      </div>
    </div>
  );
}
