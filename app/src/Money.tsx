/**
 * How money is written on screen, in one place.
 *
 * Lifted out of `ImportScreen` when the audit ledger started rendering the same
 * figures: the gate quotes a cost and the ledger reports the one that was
 * actually billed, and the two have to be comparable at a glance. Two figures
 * formatted differently invite a reader to think they measure different things.
 */
import { useTranslation } from "react-i18next";

/** One figure, or an honest statement that there is no price.
 *
 * `null` means the model has no recorded price, which renders as "not priced".
 * Formatting it as $0.00 would tell the user a paid stage is free.
 */
export function Money({ usd }: { usd: number | null }) {
  const { t } = useTranslation();
  if (usd === null) return <span className="unpriced">{t("gate.unpriced")}</span>;
  return <span>${usd.toFixed(6)}</span>;
}

/** Two figures that have to read as one value.
 *
 *  The non-breaking space glues the dash to the low figure, so the only break
 *  opportunity is after the dash. Measured at 520px: without this the dash sat
 *  alone on its own line, and forcing `nowrap` instead pushed the table past the
 *  viewport with the high figure clipped. */
export function range(low: string, high: string): string {
  return `${low} – ${high}`;
}

/** A cost that may be a range.
 *
 *  Semantic extraction is a mean over a corpus whose documents vary by more than
 *  2x, so one number could not both cover the worst document and stay within
 *  reach of the smallest — the gate under-reported a real run by 22% because it
 *  had to pick one. Two figures let it stop picking.
 *
 *  Renders a single figure when the ends agree, which is every stage but that
 *  one, so nothing grows a range it has no measurement for. */
export function Cost({ usd, high }: { usd: number | null; high: number | null }) {
  if (usd === null || high === null || high <= usd) return <Money usd={usd} />;
  return (
    <span className="range">
      {range(`$${usd.toFixed(6)}`, `$${high.toFixed(6)}`)}
    </span>
  );
}
