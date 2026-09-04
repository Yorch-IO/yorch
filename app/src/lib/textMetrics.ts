import { LABEL } from "./radial";

/**
 * How wide a string renders, measured where a browser will say and estimated
 * where it will not.
 *
 * **Why this is not in `regionLabels.ts`.** That module decides geometry and
 * touches no DOM, for the reason `force.ts` and `radial.ts` both record about
 * themselves. This is the half that has to ask the platform, and it is written
 * so that the platform refusing is ordinary rather than fatal — the same
 * decision `graphPainter.ts` makes by taking its 2D context as an argument, and
 * `useEdgeCanvas.ts` by treating a `null` context as "draw nothing".
 *
 * **Why measuring matters at all.** A cluster name has to be placed by its
 * rendered footprint, not by its anchor point: `Tomás de Aquino · Platón` needs
 * more clearance than `Roma`, and a placement that reserved the same box for
 * both would be wrong for one of them. `radial.ts` estimates a footprint from a
 * character count, which is enough for the question it asks — whether two boxes
 * are nowhere near each other — and is not enough when the box is the thing
 * being fitted into a gap.
 *
 * The estimate is still the floor under this, because jsdom's canvas has no 2D
 * context and neither does a webview built without one. `radial.ts` measured
 * 6.2px of advance per character at 11px in the system stack; that ratio is
 * what stands in.
 *
 * **How good the estimate is, measured rather than hoped.** Twenty real
 * concept names from the corpus, rendered at `600 24px` in this stack and
 * measured in Chrome against the ratio: **mean real/estimate 0.983**, so the
 * estimate over-reserves by about 1.7% on average, which is the safe
 * direction. The worst case is the other way and is short: `Roma` measures
 * 68.8 against an estimated 54.1 — 1.27x — because four wide glyphs beat the
 * average. That is 15 units of a canvas whose clearances are 10 and 8, so a
 * placement decided on the estimate alone is close but not covered. Nothing in
 * the product decides on it: a window has a 2D context, so a window measures.
 */

/** The stack `styles.css` sets on `body`. Named here because a canvas measures
 *  whatever font string it is handed and silently falls back to its default
 *  when the string does not parse — so a wrong family is a wrong number, not an
 *  error. */
export const UI_FONT_STACK = 'system-ui, -apple-system, "Segoe UI", sans-serif';

/** Advance width per point of font size, from `LABEL.CHAR_W` at `LABEL.FONT`. */
const CHAR_RATIO = LABEL.CHAR_W / LABEL.FONT;

/** Enough for every label on screen at every zoom the solve is re-run at.
 *  Bounded because the key includes the text, and a search box could otherwise
 *  feed it strings for ever. */
const CACHE_CAP = 512;

export interface FontSpec {
  size: number;
  weight?: number | string;
  /** In em, as the stylesheet writes it. Added by hand: `measureText` ignores
   *  letter spacing in the engines this has to run in. */
  letterSpacing?: number;
  family?: string;
}

let resolved: CanvasRenderingContext2D | null | undefined;

function context(): CanvasRenderingContext2D | null {
  if (resolved !== undefined) return resolved;
  resolved = null;
  try {
    if (typeof document === "undefined") return resolved;
    resolved = document.createElement("canvas").getContext("2d");
  } catch {
    resolved = null;
  }
  return resolved;
}

const cache = new Map<string, number>();

export function measureText(text: string, font: FontSpec): number {
  const { size, weight = 400, letterSpacing = 0, family = UI_FONT_STACK } = font;
  const key = `${weight}|${size}|${letterSpacing}|${family}|${text}`;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;

  const tracking = text.length > 1 ? letterSpacing * size * (text.length - 1) : 0;
  let width = text.length * CHAR_RATIO * size + tracking;

  const ctx = context();
  if (ctx !== null) {
    try {
      ctx.font = `${weight} ${size}px ${family}`;
      const measured = ctx.measureText(text).width;
      // A stub context answers 0 for everything, and so does one whose font
      // string did not parse. Zero is not a width, so the estimate stands.
      if (Number.isFinite(measured) && measured > 0) width = measured + tracking;
    } catch {
      // The estimate stands.
    }
  }

  if (cache.size >= CACHE_CAP) cache.clear();
  cache.set(key, width);
  return width;
}

/** Test seam: drops both the memo and the resolved context, so a suite can
 *  exercise the estimate and the measurement in one run. */
export function resetTextMetrics(): void {
  cache.clear();
  resolved = undefined;
}
