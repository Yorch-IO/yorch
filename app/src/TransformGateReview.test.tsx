/**
 * The two gates a transformation stops at.
 *
 * What is under test is the one thing the ingest path got wrong and this
 * feature was shaped around: the second gate must show its **own** report. In
 * `IngestWorkflow` one report object is assigned before the first gate and
 * never cleared, so a reader was shown "Nothing has been paid for yet" over a
 * run that had already spent $0.58, above a chunk count measured before the
 * correction that changed every offset.
 *
 * `cleanup` by hand, as everywhere else here: vitest exposes no global
 * `afterEach`, so RTL never registers its automatic one.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "./i18n";
import type {
  Estimate,
  TransformGateReport,
  TransformOptions,
  TransformPlanReport,
} from "./lib/api";
import { TransformPlanReview, TransformQuote } from "./TransformGateReview";

afterEach(cleanup);
beforeEach(async () => {
  await i18n.changeLanguage("es");
});

const t = (k: string, o?: Record<string, unknown>): string => i18n.t(k, o ?? {});

const estimate: Estimate = {
  stages: [
    {
      stage: "transform-compose",
      model: "gemini-3.6-flash",
      inputTokens: 200_000,
      outputTokens: 600_000,
      usd: 4.5,
      outputTokensHigh: 900_000,
      usdHigh: 6.75,
    },
  ],
  totalUsd: 4.5,
  totalUsdHigh: 6.75,
  priceSource: "Precios de terceros consultados el 2026-08-20",
  unpricedStages: [],
};

const quote = (over: Partial<TransformGateReport> = {}): TransformGateReport => ({
  genre: "essay",
  mode: "faithful",
  purposes: ["context"],
  sourceTitle: "El Documento",
  sourceChapters: 12,
  characters: 420_000,
  projectedChapters: 18,
  researchBudget: 24,
  supported: 96,
  projection: true,
  estimate,
  ...over,
});

const plan = (over: Partial<TransformPlanReport> = {}): TransformPlanReport => ({
  genre: "essay",
  mode: "faithful",
  chapters: [
    { ordinal: 1, title: "Primero", intent: "abre la cuestión", chars: 20_000 },
    { ordinal: 2, title: "Segundo", intent: "la desarrolla", chars: 22_000 },
  ],
  uncoveredFraction: 0,
  researchBudget: 24,
  fallback: false,
  notes: [],
  spentSoFar: 0.0412,
  estimate,
  ...over,
});

const options: TransformOptions = { reviewPlan: true, research: true };

describe("the first gate", () => {
  it("says the chapter count is a projection", () => {
    // A projected figure that is not labelled as one is worse than no figure:
    // the moment somebody decides is exactly the moment a number is taken
    // literally, and the recorded harm of a misleading quote is a 7.1x
    // over-report met with "that is too much for something I already have".
    const { container } = render(
      <TransformQuote report={quote()} options={options} busy={false} onDecide={vi.fn()} />,
    );
    expect(container.textContent).toContain(t("transform.gate.projection"));
  });

  it("prints a measured zero as a sentence rather than as a switch", () => {
    // Zero is a real answer: a library where nothing clears the similarity
    // floor genuinely has nothing to add, and `OffCorpus` read at corpus scale
    // is what says so.
    const { container } = render(
      <TransformQuote
        report={quote({ researchBudget: 0, supported: 0 })}
        options={options}
        busy={false}
        onDecide={vi.fn()}
      />,
    );
    expect(container.textContent).toContain(t("transform.gate.noResearch"));
    const research = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(research?.disabled).toBe(true);
  });

  it("carries the switches the person actually left set", () => {
    // The recorded live defect, from the client's side: the Angular gate sent
    // its options alone while the workflow reads `approval.options`, so every
    // stage a person unticked was silently turned back on.
    const onDecide = vi.fn();
    const { container } = render(
      <TransformQuote report={quote()} options={options} busy={false} onDecide={onDecide} />,
    );
    const boxes = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    fireEvent.click(boxes[0]!); // research off
    fireEvent.click(container.querySelectorAll("button")[0]!);
    expect(onDecide).toHaveBeenCalledWith(true, { reviewPlan: true, research: false });
  });

  it("repeats the price caveat wherever it shows a dollar figure", () => {
    const { container } = render(
      <TransformQuote report={quote()} options={options} busy={false} onDecide={vi.fn()} />,
    );
    expect(container.textContent).toContain("Precios de terceros");
  });

  it("disables both buttons while a decision is in flight, and neither otherwise", () => {
    const { container } = render(
      <TransformQuote report={quote()} options={options} busy onDecide={vi.fn()} />,
    );
    for (const button of container.querySelectorAll("button")) {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    }
  });
});

describe("the second gate", () => {
  it("shows its own outline and its own spend, not the first gate's", () => {
    const { container } = render(
      <TransformPlanReview report={plan()} options={options} busy={false} onDecide={vi.fn()} />,
    );
    expect(container.textContent).toContain("Primero");
    expect(container.textContent).toContain("Segundo");
    expect(container.textContent).toContain("0.0412");
    // And nothing that would only be true of the first gate.
    expect(container.textContent).not.toContain(t("transform.gate.projection"));
  });

  it("says when the outline is the document's own, without calling it a failure", () => {
    // After three refused proposals the source's own chapters are adopted,
    // which covers the document by construction — the argument rule learning
    // already makes for falling back rather than refusing to publish a
    // document whose only fault is being ordinary.
    const { container } = render(
      <TransformPlanReview
        report={plan({ fallback: true })}
        options={options}
        busy={false}
        onDecide={vi.fn()}
      />,
    );
    expect(container.textContent).toContain(t("transform.plan.fallback"));
  });

  it("reports uncovered material when there is some, and says nothing when there is none", () => {
    const { container } = render(
      <TransformPlanReview
        report={plan({ uncoveredFraction: 0.18 })}
        options={options}
        busy={false}
        onDecide={vi.fn()}
      />,
    );
    expect(container.textContent).toContain("18%");

    cleanup();
    const clean = render(
      <TransformPlanReview report={plan()} options={options} busy={false} onDecide={vi.fn()} />,
    );
    expect(clean.container.textContent).not.toContain("%");
  });

  it("keeps the plan-review switch it was given rather than inventing one", () => {
    // This gate offers only research; `reviewPlan` has already happened by the
    // time anybody reads it, and answering with a defaulted object would be the
    // silent-reset defect in a third place.
    const onDecide = vi.fn();
    const { container } = render(
      <TransformPlanReview
        report={plan()}
        options={{ reviewPlan: false, research: true }}
        busy={false}
        onDecide={onDecide}
      />,
    );
    fireEvent.click(container.querySelectorAll("button")[0]!);
    expect(onDecide).toHaveBeenCalledWith(true, { reviewPlan: false, research: true });
  });
});
