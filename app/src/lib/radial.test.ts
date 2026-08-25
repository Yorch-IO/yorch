import { describe, expect, it } from "vitest";

import {
  cardBox,
  GEOM,
  halfStep,
  labelAnchor,
  labelBox,
  LABEL,
  overlaps,
  ring,
  scale,
  STAGGER,
  STAGGER_ABOVE,
  truncate,
} from "./radial";

/**
 * The geometry, asserted where it can be asserted.
 *
 * jsdom computes no SVG layout — no `getBBox`, no resolved `transform` — so a
 * rendered test can only check that a node exists and carries the attributes it
 * was given. Whether two labels collide is a property of these numbers, and
 * this is the only place it can be caught. `GraphScreen.test.tsx` covers the
 * behaviour; this covers the picture.
 */

/** The counts the screen actually draws with. A property proved at 3 and 4
 *  items says nothing about the configuration that ships. */
const CONCEPTS = 12;
const RELATED = 8;

const items = (n: number): number[] => Array.from({ length: n }, (_, i) => i);

/** Smallest circular distance between two angles, in radians. */
function gap(a: number, b: number): number {
  const d = Math.abs(a - b) % (Math.PI * 2);
  return Math.min(d, Math.PI * 2 - d);
}

const DEG = Math.PI / 180;

describe("ring", () => {
  it("is a pure function of its input", () => {
    const once = ring(items(CONCEPTS), GEOM.R_CONCEPT, { stagger: STAGGER });
    const twice = ring(items(CONCEPTS), GEOM.R_CONCEPT, { stagger: STAGGER });
    expect(once).toEqual(twice);
  });

  it("starts at twelve o'clock", () => {
    const [first] = ring(items(4), 100);
    expect(first?.x).toBeCloseTo(GEOM.CX);
    expect(first?.y).toBeCloseTo(GEOM.CY - 100);
  });

  it("goes clockwise", () => {
    const placed = ring(items(4), 100);
    // Second of four is three o'clock: right of centre, level with it.
    expect(placed[1]?.x).toBeCloseTo(GEOM.CX + 100);
    expect(placed[1]?.y).toBeCloseTo(GEOM.CY);
  });

  it("survives an empty ring without NaN", () => {
    // Dividing by a zero length would make every coordinate NaN, and SVG drops
    // an element with a NaN attribute silently — the failure would look like a
    // blank canvas rather than an error.
    expect(ring([], 100)).toEqual([]);
    const [only] = ring(items(1), 100);
    expect(Number.isFinite(only?.x)).toBe(true);
    expect(Number.isFinite(only?.y)).toBe(true);
  });

  it("staggers only above the threshold, and alternates", () => {
    const sparse = ring(items(STAGGER_ABOVE), GEOM.R_CONCEPT, { stagger: STAGGER });
    expect(sparse.map((p) => p.radius)).toEqual(
      sparse.map(() => GEOM.R_CONCEPT),
    );

    const dense = ring(items(STAGGER_ABOVE + 1), GEOM.R_CONCEPT, { stagger: STAGGER });
    expect(dense.map((p) => p.radius).slice(0, 4)).toEqual([
      GEOM.R_CONCEPT - STAGGER,
      GEOM.R_CONCEPT + STAGGER,
      GEOM.R_CONCEPT - STAGGER,
      GEOM.R_CONCEPT + STAGGER,
    ]);
  });

  it("does not stagger when no stagger is asked for", () => {
    const plain = ring(items(CONCEPTS), GEOM.R_CONCEPT);
    expect(new Set(plain.map((p) => p.radius))).toEqual(new Set([GEOM.R_CONCEPT]));
  });
});

describe("the offset between the two rings", () => {
  it("puts no document on any concept's ray", () => {
    const concepts = ring(items(CONCEPTS), GEOM.R_CONCEPT, { stagger: STAGGER });
    const docs = ring(items(RELATED), GEOM.R_DOC, { offset: halfStep(RELATED) });

    let smallest = Infinity;
    for (const c of concepts) {
      for (const d of docs) {
        smallest = Math.min(smallest, gap(c.angle, d.angle));
      }
    }
    // Half of the outer ring's step against a 30° inner step leaves 7.5°.
    // Without the offset it would be 0 — ring 0 and concept 0 both at noon.
    expect(smallest / DEG).toBeCloseTo(7.5, 5);
  });

  it("is zero degrees without the offset, which is the bug", () => {
    const concepts = ring(items(CONCEPTS), GEOM.R_CONCEPT);
    const docs = ring(items(RELATED), GEOM.R_DOC);
    expect(gap(concepts[0]!.angle, docs[0]!.angle)).toBeCloseTo(0);
  });
});

describe("labelAnchor", () => {
  const at = (deg: number) => labelAnchor(deg * DEG, 10);

  it("pushes the label to the right on the right of the circle", () => {
    const a = at(0);
    expect(a.anchor).toBe("start");
    expect(a.dx).toBeGreaterThan(0);
    expect(a.dy).toBe(0);
  });

  it("pushes it to the left on the left", () => {
    const a = at(180);
    expect(a.anchor).toBe("end");
    expect(a.dx).toBeLessThan(0);
  });

  it("puts it above at the top and below at the bottom", () => {
    expect(at(-90).anchor).toBe("middle");
    expect(at(-90).dy).toBeLessThan(0);
    expect(at(90).anchor).toBe("middle");
    expect(at(90).dy).toBeGreaterThan(0);
  });

  it("keeps a near-vertical label vertical rather than flinging it sideways", () => {
    // 15° off noon: the cosine is positive but small. Placing this label to the
    // side is what made the top of the ring unreadable.
    expect(at(-75).anchor).toBe("middle");
    expect(at(-105).anchor).toBe("middle");
    // Past the threshold it does swing out.
    expect(at(-45).anchor).toBe("start");
  });

  it("clears the node it belongs to", () => {
    for (const r of [9, 14, 20]) {
      const a = labelAnchor(0, r);
      expect(a.dx).toBeGreaterThan(r);
    }
  });
});

describe("the drawn configuration", () => {
  const concepts = ring(items(CONCEPTS), GEOM.R_CONCEPT, { stagger: STAGGER });
  const docs = ring(items(RELATED), GEOM.R_DOC, { offset: halfStep(RELATED) });
  // The top of the mention scale, which is the widest a concept node gets.
  const conceptLabels = concepts.map((p) =>
    labelBox(p.x, p.y, labelAnchor(p.angle, 20), LABEL.CONCEPT_CHARS),
  );
  const cards = docs.map((p) => cardBox(p.x, p.y, GEOM.DOC_W, GEOM.DOC_H));

  it("keeps every document card inside the viewBox", () => {
    for (const b of cards) {
      expect(b.x1).toBeGreaterThan(0);
      expect(b.y1).toBeGreaterThan(0);
      expect(b.x2).toBeLessThan(GEOM.W);
      expect(b.y2).toBeLessThan(GEOM.H);
    }
  });

  it("keeps every concept label inside the viewBox", () => {
    for (const b of conceptLabels) {
      expect(b.x1).toBeGreaterThan(0);
      expect(b.y1).toBeGreaterThan(0);
      expect(b.x2).toBeLessThan(GEOM.W);
      expect(b.y2).toBeLessThan(GEOM.H);
    }
  });

  it("puts no concept label under a document card", () => {
    const hits: string[] = [];
    conceptLabels.forEach((label, i) => {
      cards.forEach((card, j) => {
        if (overlaps(label, card)) hits.push(`concept ${i} × document ${j}`);
      });
    });
    expect(hits).toEqual([]);
  });

  it("puts no concept label over another concept's label", () => {
    const hits: string[] = [];
    for (let i = 0; i < conceptLabels.length; i += 1) {
      for (let j = i + 1; j < conceptLabels.length; j += 1) {
        if (overlaps(conceptLabels[i]!, conceptLabels[j]!)) {
          hits.push(`${i} × ${j}`);
        }
      }
    }
    expect(hits).toEqual([]);
  });

  it("keeps concept labels clear of the centre card", () => {
    const centre = cardBox(GEOM.CX, GEOM.CY, GEOM.CENTRE_W, GEOM.CENTRE_H);
    for (const b of conceptLabels) {
      expect(overlaps(b, centre)).toBe(false);
    }
  });

  it("puts no document card on top of another", () => {
    for (let i = 0; i < cards.length; i += 1) {
      for (let j = i + 1; j < cards.length; j += 1) {
        expect(overlaps(cards[i]!, cards[j]!)).toBe(false);
      }
    }
  });
});

describe("scale", () => {
  it("spans min to min+span", () => {
    expect(scale(0, 10, 9, 11)).toBe(9);
    expect(scale(10, 10, 9, 11)).toBe(20);
  });

  it("treats a zero maximum as one rather than dividing by it", () => {
    expect(Number.isFinite(scale(0, 0, 9, 11))).toBe(true);
  });
});

describe("truncate", () => {
  it("leaves a short string alone", () => {
    expect(truncate("corto", 10)).toBe("corto");
  });

  it("spends the last character on the ellipsis", () => {
    expect(truncate("abcdefghij", 5)).toBe("abcd…");
    expect(truncate("abcdefghij", 5)).toHaveLength(5);
  });
});
