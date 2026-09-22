/**
 * The second gate's panel, and the one thing it must never do again.
 *
 * It rendered `GateReview` — the first gate's report, frozen before any money
 * was spent — so it quoted an *estimate* for correction on a run already billed
 * for it and said "nothing has been paid for yet" with the real figure on the
 * row directly above. The numbers below are from a real run: the gate offered
 * 50 chunks and $0.161821 estimated where the truth was 210 paragraphs, 30
 * corrected and $0.133921 spent.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import i18n from "./i18n";
import { CorrectionReview } from "./CorrectionReview";
import { DEFAULT_STAGES, type CorrectionReport, type StageOptions } from "./lib/api";

await i18n.changeLanguage("es");

afterEach(cleanup);

// The real defaults, not a hand-built object: an `as StageOptions` would hide
// a field added later, which is the recorded `StageOptions` parity hazard.
const STAGES: StageOptions = DEFAULT_STAGES;

const report = (over: Partial<CorrectionReport> = {}): CorrectionReport => ({
  paragraphs: 210,
  changed: 30,
  rejected: 0,
  missing: 0,
  cacheHits: 0,
  spend: {
    stage: "correction",
    model: "gemini-3.6-flash",
    inputTokens: 1000,
    outputTokens: 2000,
    usd: 0.133921,
  },
  ...over,
});

const draw = (r: CorrectionReport) =>
  render(
    <CorrectionReview report={r} stages={STAGES} busy={false} onDecide={() => {}} />,
  );

it("reports what correction spent, never what it was quoted", () => {
  const { container } = draw(report());
  expect(container.textContent).toContain("0.133921");
  // The figure the old panel showed instead. A quote for money already spent
  // is worse than no figure: it presents the decision as still ahead.
  expect(container.textContent).not.toContain("0.161821");
});

it("counts paragraphs and never chunks", () => {
  /** The stale panel's "50 chunks" was measured before correction, which moves
   *  every byte offset — so it described a cutting that no longer exists. */
  const { container } = draw(report());
  expect(container.textContent).toContain("210");
  expect(container.textContent).toContain("30");
  expect(container.textContent).not.toContain("50 ");
});

it("says a declined correction is an improvement refused and not damage", () => {
  /** The available misreading, and the expensive one: a person who reads
   *  "22 rejected" as "22 paragraphs broken" cancels a run whose text is
   *  intact. `verify` refusing a correction keeps the original paragraph. */
  const { container } = draw(report({ rejected: 22 }));
  expect(container.textContent).toContain("22");
  expect(container.textContent).toMatch(/nunca un daño|never damage/);
});

it("hides the counts that are zero rather than printing them", () => {
  /** `rejected: 0` and `missing: 0` are the ordinary case; four zeroes on a
   *  panel read as a list of problems rather than as their absence. */
  const { container } = draw(report());
  expect(container.textContent).not.toMatch(/rechazadas|declined/i);
});

it("an unpriced model reads as paid-for-without-a-price, never as free", () => {
  /** `null` is not zero — the rule `ledger.py` states and every other surface
   *  in this product honours as "sin precio". Rendering it as $0.000000 at the
   *  moment somebody decides whether to spend more would be the same lie in a
   *  smaller font. */
  const { container } = draw(report({ spend: { ...report().spend, usd: null } }));
  expect(container.textContent).not.toContain("$0.000000");
  expect(container.textContent).toMatch(/no tiene precio|no price/);
});

it("offers both ways out", () => {
  draw(report());
  expect(screen.getAllByRole("button")).toHaveLength(2);
});
