/**
 * The radial layout, as arithmetic.
 *
 * `GraphScreen` draws two rings around a centre and nothing about that needs a
 * force simulation — positions are a pure function of the data, which is what
 * keeps the picture from wandering between renders. Pulling the arithmetic out
 * here is what lets it be *proved*: jsdom computes no SVG geometry, so a
 * rendered test can only see attributes. Collisions are a property of these
 * numbers, and `radial.test.ts` asserts them directly.
 *
 * Same reasoning as `askSession.ts`: the part with rules in it becomes a plain
 * function, and the component keeps only the rendering.
 */

/** The canvas, and every fixed size drawn on it. One place, because the label
 *  gutter, the ring radii and the card widths are only correct relative to each
 *  other — changing one in isolation is how labels end up under cards. */
export const GEOM = {
  W: 880,
  H: 620,
  CX: 440,
  CY: 310,
  /** Inner ring. Staggered by ±`STAGGER` above `STAGGER_ABOVE` items. */
  R_CONCEPT: 150,
  /** Outer ring. Far enough out that a concept label pointing outwards ends
   *  before a document card begins on the same ray. */
  R_DOC: 300,
  CENTRE_W: 240,
  CENTRE_H: 64,
  DOC_W: 140,
  DOC_H: 40,
  /** Smallest pointer/touch target on the inner ring, as a radius. Concept
   *  circles reach r≈20 at the top of the mention scale and 9 at the bottom,
   *  which is a 18px target — below any comfortable one. */
  HIT_R: 22,
} as const;

/** Radial offset applied to alternating items, so two neighbouring labels sit
 *  on different circles rather than side by side in one horizontal band. */
export const STAGGER = 14;

/** Below this many items the ring is sparse enough that staggering only makes
 *  it look accidental. */
export const STAGGER_ABOVE = 8;

export interface Placed<T> {
  item: T;
  x: number;
  y: number;
  /** Radians, zero at three o'clock, as `Math.cos`/`Math.sin` take it. Kept so
   *  the caller can ask for a label anchor without recomputing it. */
  angle: number;
  /** The radius actually used, which is not `radius` when staggered. */
  radius: number;
}

export interface LabelAnchor {
  dx: number;
  dy: number;
  anchor: "start" | "middle" | "end";
  baseline: "auto" | "middle" | "hanging";
}

export interface RingOptions {
  /** Rotates the whole ring. Two rings sharing twelve o'clock put their first
   *  item on one ray, which is the collision this exists to avoid. */
  offset?: number;
  /** Pass `STAGGER` to enable it; omitted means a true circle. */
  stagger?: number;
}

/**
 * Evenly spaced from twelve o'clock, clockwise.
 *
 * `Math.max(items.length, 1)` guards the empty case: dividing by zero would
 * make every coordinate `NaN` and SVG silently drops an element with a `NaN`
 * attribute, so an empty ring would fail as a blank canvas rather than as an
 * error.
 */
export function ring<T>(
  items: T[],
  radius: number,
  opts: RingOptions = {},
): Placed<T>[] {
  const { offset = 0, stagger = 0 } = opts;
  const step = (Math.PI * 2) / Math.max(items.length, 1);
  const staggering = stagger !== 0 && items.length > STAGGER_ABOVE;

  return items.map((item, i) => {
    const angle = i * step - Math.PI / 2 + offset;
    const r = staggering ? radius + (i % 2 === 0 ? -stagger : stagger) : radius;
    return {
      item,
      x: GEOM.CX + Math.cos(angle) * r,
      y: GEOM.CY + Math.sin(angle) * r,
      angle,
      radius: r,
    };
  });
}

/** Half a step of a ring of `count` items. The offset that puts a ring's items
 *  between another's rather than on top of them. */
export function halfStep(count: number): number {
  return Math.PI / Math.max(count, 1);
}

/**
 * Where a node's label goes, by quadrant.
 *
 * The bug this replaces: every concept label was placed at `y = -14`, centred,
 * wherever on the ring its node sat. Near twelve o'clock the labels stacked on
 * each other; at three and nine o'clock they ran back over their own node. A
 * label belongs *outside* the node, pushed away from the centre, and which way
 * "outside" is depends on where on the circle you are.
 *
 * The 0.35 threshold, rather than 0, is what keeps a label near the top from
 * being flung sideways by a barely-positive cosine: within roughly 20° of
 * vertical the label goes above or below, where there is room, and only past
 * that does it swing out to the side.
 */
export function labelAnchor(angle: number, nodeRadius: number): LabelAnchor {
  const cos = Math.cos(angle);
  const gutter = nodeRadius + 7;

  if (cos > 0.35) {
    return { dx: gutter, dy: 0, anchor: "start", baseline: "middle" };
  }
  if (cos < -0.35) {
    return { dx: -gutter, dy: 0, anchor: "end", baseline: "middle" };
  }
  return Math.sin(angle) < 0
    ? { dx: 0, dy: -(nodeRadius + 9), anchor: "middle", baseline: "auto" }
    : { dx: 0, dy: nodeRadius + 9, anchor: "middle", baseline: "hanging" };
}

/** A node's radius from its share of the largest value on the ring. Both rings
 *  normalise against their own maximum, so a document whose neighbours all
 *  share two concepts does not look isolated. */
export function scale(value: number, max: number, min: number, span: number): number {
  return min + (value / Math.max(max, 1)) * span;
}

export function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

/* -- collision, as arithmetic rather than as hope ---------------------------
 *
 * "Documents must not cover concepts" is enforced two ways. The cheap and
 * robust one is paint order plus a halo behind every label, in the component.
 * The one that can be *tested* is this: SVG gives no text metrics outside a
 * browser, so a label's footprint is estimated from its character count at the
 * font size the stylesheet uses, and `radial.test.ts` asserts that no concept
 * label's box meets any document card's box at the real ring counts. An
 * estimate is enough — the question is whether two boxes are nowhere near each
 * other, not whether they miss by a pixel.
 */

export const LABEL = {
  /** Must match `.screen.graph .graph-canvas .node text` in `styles.css`. */
  FONT: 11,
  /** Advance width of a digit in the system UI stack at 11px, rounded up. */
  CHAR_W: 6.2,
  HEIGHT: 13,
  CONCEPT_CHARS: 18,
  DOC_CHARS: 18,
  CENTRE_CHARS: 30,
} as const;

export interface Box {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

/** The footprint of a label placed by `labelAnchor`, in canvas coordinates. */
export function labelBox(x: number, y: number, a: LabelAnchor, chars: number): Box {
  const w = chars * LABEL.CHAR_W;
  const h = LABEL.HEIGHT;
  const ax = x + a.dx;
  const ay = y + a.dy;
  const x1 = a.anchor === "start" ? ax : a.anchor === "end" ? ax - w : ax - w / 2;
  const y1 =
    a.baseline === "middle" ? ay - h / 2 : a.baseline === "auto" ? ay - h : ay;
  return { x1, y1, x2: x1 + w, y2: y1 + h };
}

/** A card centred on a placed point. Also what the component feeds `<rect>`. */
export function cardBox(x: number, y: number, w: number, h: number): Box {
  return { x1: x - w / 2, y1: y - h / 2, x2: x + w / 2, y2: y + h / 2 };
}

export function overlaps(a: Box, b: Box): boolean {
  return a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;
}
