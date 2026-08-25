/** The gate's cost cell.
 *
 *  Extracted and tested on its own for the reason `lib/radial.ts` gives about
 *  the graph's arithmetic: the property that matters is a decision about
 *  numbers, and asserting it directly beats rendering the whole approval screen
 *  to find out whether two figures were joined by a dash.
 *
 *  Assertions go through each render's own container rather than `screen`.
 *  Nothing in this project configures Testing Library's `cleanup`, so `screen`
 *  queries the accumulated body and a second render sees the first one's output.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Cost } from "./ImportScreen";

const text = (usd: number | null, high: number | null): string =>
  render(<Cost usd={usd} high={high} />).container.textContent ?? "";

describe("the cost a person approves", () => {
  it("shows a range when the estimate has a measured spread", () => {
    // Semantics is a mean over documents that vary by more than 2x. One number
    // could not both cover the worst and stay in reach of the smallest, and the
    // gate under-reported a real run by 22% because it had to pick one.
    // The dash arrives glued to the low figure by a non-breaking space, so the
    // pair can only break after it — see `range`.
    expect(text(0.0993, 0.1199)).toBe("$0.099300\u00a0\u2013 $0.119900");
  });

  it("shows a single figure when both ends agree", () => {
    // Every stage but semantics. A range with nothing behind it would claim a
    // precision the estimate does not have.
    expect(text(0.000861, 0.000861)).toBe("$0.000861");
  });

  it("never renders an unpriced model as zero", () => {
    // "We have token counts but no price" and "this is free" are different
    // answers, and this is the figure a user approves spend against.
    expect(text(null, null)).not.toMatch(/\$/);
  });

  it("ignores a high end that is not above the low one", () => {
    expect(text(0.5, 0.4)).toBe("$0.500000");
  });

  it("never lets the dash break onto a line of its own", () => {
    // At 520px it did. `white-space: nowrap` was the wrong fix — it pushed the
    // estimate table past the viewport instead — so the pair is glued in the
    // markup and the only break opportunity sits after the dash.
    const rendered = text(0.0993, 0.1199);
    expect(rendered).toContain("\u00a0\u2013 ");
    expect(rendered).not.toContain(" \u2013 ");
  });
});
