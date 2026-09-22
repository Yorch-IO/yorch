import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import i18n from "./i18n";
import type { Estimate, Prepaid } from "./lib/api";
import { EstimateTable } from "./GateReview";

/**
 * The three states a cache measurement can be in have to read differently.
 * A re-import was once quoted as a first import — $0.2257767 against a bill of
 * $0.031821 — and the fix is only worth having if the gate says so; but a
 * *measured* zero and an absent measurement are different facts too, and
 * printing "0 of 107 are already corrected" over a first import is noise while
 * printing it over an unmeasured one is a claim nobody checked.
 */
const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});

function estimate(prepaid: Prepaid | null): Estimate {
  return {
    stages: [
      {
        stage: "correction",
        model: "gemini-3.6-flash",
        inputTokens: 3377,
        outputTokens: 4165,
        usd: 0.0363,
        outputTokensHigh: 4165,
        usdHigh: 0.0363,
      },
    ],
    totalUsd: 0.0363,
    totalUsdHigh: 0.0363,
    priceSource: "precios de terceros",
    unpricedStages: [],
    prepaid,
  };
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
});
afterEach(cleanup);

describe("EstimateTable", () => {
  it("says how much of a re-import is already paid for", () => {
    render(
      <EstimateTable
        estimate={estimate({
          correctionHits: 85,
          correctionTotal: 107,
          correctionCharacters: 4000,
          correctionCharactersTotal: 20000,
        })}
      />,
    );
    expect(
      screen.getByText(t("gate.prepaid", { hits: 85, total: 107 })),
    ).toBeTruthy();
  });

  it("prints nothing over a first import, whose measurement is a real zero", () => {
    const { container } = render(
      <EstimateTable
        estimate={estimate({
          correctionHits: 0,
          correctionTotal: 107,
          correctionCharacters: 20000,
          correctionCharactersTotal: 20000,
        })}
      />,
    );
    expect(container.querySelector(".prepaid")).toBeNull();
  });

  it("survives a control plane that has never heard of the field", () => {
    // An older plane sends no key at all, and `undefined !== null` is true —
    // which read `undefined.correctionHits` and took the gate down.
    const older = estimate(null);
    delete (older as { prepaid?: unknown }).prepaid;
    const { container } = render(<EstimateTable estimate={older} />);
    expect(container.querySelector(".prepaid")).toBeNull();
    expect(screen.getByText("precios de terceros")).toBeTruthy();
  });

  it("prints nothing when nothing measured, and never invents a zero", () => {
    const { container } = render(<EstimateTable estimate={estimate(null)} />);
    expect(container.querySelector(".prepaid")).toBeNull();
    // The price caveat still travels with the figure, always.
    expect(screen.getByText("precios de terceros")).toBeTruthy();
  });
});
