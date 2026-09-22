/**
 * The second gate: what correction did, before paying to embed it.
 *
 * It used to render `GateReview` — the *first* gate's report, which
 * `IngestWorkflow._report` assigns once and never clears. So the panel whose
 * whole purpose is "look at what correction did" showed a chunk count measured
 * before correction moved every offset, an *estimate* for correction on a run
 * already billed for it, and the line "nothing has been paid for yet" with the
 * real figure on the queue row directly above. Measured on a real run: it
 * offered "50 chunks, correction $0.161821" where the truth was 210
 * paragraphs, 30 corrected, $0.133921 spent.
 *
 * Counts and spend, not a diff. What changed is in `correction-report.json`,
 * which is not downloadable — and a true count is worth more than a false
 * preview, which is the whole defect.
 */
import { useTranslation } from "react-i18next";

import type { CorrectionReport, StageOptions } from "./lib/api";

export function CorrectionReview({
  report,
  stages,
  busy,
  onDecide,
}: {
  report: CorrectionReport;
  stages: StageOptions;
  busy: boolean;
  onDecide: (approved: boolean, options: StageOptions) => void;
}) {
  const { t } = useTranslation();
  const usd = report.spend?.usd ?? null;
  const kept = report.paragraphs - report.changed;

  return (
    <section className="gate correction-gate">
      <h3>{t("correction.title")}</h3>
      <p className="muted">{t("correction.intro")}</p>

      <dl className="correction-counts">
        <div>
          <dt>{t("correction.paragraphs")}</dt>
          <dd>{report.paragraphs}</dd>
        </div>
        <div>
          <dt>{t("correction.changed")}</dt>
          <dd>{report.changed}</dd>
        </div>
        <div>
          <dt>{t("correction.unchanged")}</dt>
          <dd>{kept}</dd>
        </div>
        {/* Shown whenever it is non-zero, and explained rather than counted:
            a refused correction is an improvement declined, never damage done
            — the paragraph keeps its original text. Reading it as breakage is
            the available misreading, so the wording denies it. */}
        {report.rejected > 0 && (
          <div>
            <dt>{t("correction.rejected")}</dt>
            <dd>{report.rejected}</dd>
          </div>
        )}
        {report.missing > 0 && (
          <div>
            <dt>{t("correction.missing")}</dt>
            <dd>{report.missing}</dd>
          </div>
        )}
        {report.cacheHits > 0 && (
          <div>
            <dt>{t("correction.cached")}</dt>
            <dd>{report.cacheHits}</dd>
          </div>
        )}
      </dl>

      {/* The line the old panel got exactly backwards. `null` is not zero: a
          missing price renders as "sin precio" everywhere else in this product
          and must not render as free here, at the moment somebody decides
          whether to spend more. */}
      <p className="correction-spent">
        {usd === null
          ? t("correction.spentUnknown")
          : t("correction.spent", { usd: usd.toFixed(6) })}
      </p>
      {report.rejected > 0 && (
        <p className="muted">{t("correction.rejectedNote")}</p>
      )}

      <div className="actions">
        {/* Both plain, like `GateReview`'s pair. A `className="primary"` here
            matched no rule in the stylesheet, so it styled nothing and would
            have rotted unnoticed — the screenshot is what said so. */}
        <button type="button" disabled={busy} onClick={() => onDecide(true, stages)}>
          {t("correction.approve")}
        </button>
        <button type="button" disabled={busy} onClick={() => onDecide(false, stages)}>
          {t("correction.cancel")}
        </button>
      </div>
    </section>
  );
}
