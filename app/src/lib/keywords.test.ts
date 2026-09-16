/**
 * The title keywords, as strings and numbers.
 *
 * What is asserted is the derivation — which words survive, how they are
 * counted, what a chip is called — because that is what a person's filter is
 * built from, and a rendered `<button>` says nothing about any of it.
 */
import { describe, expect, it } from "vitest";

import {
  BOILERPLATE_MIN_TITLES,
  KEYWORDS_MAX,
  titleKeywords,
  titleMatches,
  tokens,
  topicOf,
} from "./keywords";

describe("tokens", () => {
  it("lowercases, folds accents, and keeps the spelling beside the key", () => {
    expect(tokens("La Justicia de Dios")).toEqual([
      ["justicia", "Justicia"],
      ["dios", "Dios"],
    ]);
    expect(tokens("Oración y Perdón")).toEqual([
      ["oracion", "Oración"],
      ["perdon", "Perdón"],
    ]);
  });

  it("does not fold ñ, so año and ano stay apart", () => {
    expect(tokens("Año nuevo")[0]).toEqual(["año", "Año"]);
  });

  it("drops stopwords, short tokens and bare numbers", () => {
    // `bm25.py`'s rules, plus one: a year or a chapter number is not a topic.
    expect(tokens("Culto del domingo 2024 - parte 12 con los jóvenes")).toEqual([
      ["culto", "Culto"],
      ["domingo", "domingo"],
      ["parte", "parte"],
      ["jovenes", "jóvenes"],
    ]);
  });

  it("splits on punctuation and keeps non-Latin letters", () => {
    expect(tokens("¿Qué es el ágape? | Predicación").map(([k]) => k)).toEqual([
      "agape",
      "predicacion",
    ]);
  });
});

describe("titleKeywords", () => {
  it("counts videos, not occurrences", () => {
    // One title that repeats a word three times is still one video about it.
    const out = titleKeywords(["Fe, fe y más fe", "La fe de Abraham", "El perdón"]);
    const fe = out.find((k) => k.key === "fe");
    expect(fe).toBeUndefined(); // two letters: under MIN_TOKEN_LEN
    const abraham = out.find((k) => k.key === "abraham");
    expect(abraham?.videos).toBe(1);
  });

  it("orders by how many videos carry the word, then by label", () => {
    const out = titleKeywords([
      "La justicia de Dios",
      "Justicia y misericordia",
      "Misericordia hoy",
      "Amor y perdón",
    ]);
    expect(out.map((k) => [k.key, k.videos])).toEqual([
      ["justicia", 2],
      ["misericordia", 2],
      ["amor", 1],
      ["dios", 1],
      ["hoy", 1],
      ["perdon", 1],
    ]);
  });

  it("labels a chip with the spelling the channel uses most", () => {
    // The key is folded for matching; the chip must not read as a typo.
    const out = titleKeywords(["El Espíritu Santo", "espíritu de verdad", "Espíritu y vida"]);
    expect(out.find((k) => k.key === "espiritu")?.label).toBe("Espíritu");
  });

  it("drops words present in more than half the titles once there are enough titles", () => {
    // The channel's own name is on every title and joins nothing.
    const titles = Array.from({ length: 12 }, (_, i) =>
      i % 2 === 0 ? `Iglesia Central · Sermón ${i} sobre la gracia` : `Iglesia Central · Culto ${i}`,
    );
    const keys = titleKeywords(titles).map((k) => k.key);
    expect(keys).not.toContain("iglesia");
    expect(keys).not.toContain("central");
    // Exactly half is not "more than half": kept.
    expect(keys).toContain("sermon");
    expect(keys).toContain("gracia");
  });

  it("drops nothing on a small channel, where a share means nothing", () => {
    const titles = Array.from(
      { length: BOILERPLATE_MIN_TITLES - 1 },
      (_, i) => `Iglesia Central ${i} tema${i}`,
    );
    expect(titleKeywords(titles).map((k) => k.key)).toContain("iglesia");
  });

  it("returns at most the cap", () => {
    const titles = Array.from({ length: 60 }, (_, i) => `Palabra${i} distinta`);
    const out = titleKeywords(titles);
    expect(out).toHaveLength(KEYWORDS_MAX);
    // "distinta" is on every title and therefore dropped; the cap is filled
    // with the singletons in label order.
    expect(out.map((k) => k.key)).not.toContain("distinta");
  });

  it("is empty for an empty catalogue rather than throwing", () => {
    expect(titleKeywords([])).toEqual([]);
  });
});

describe("titleMatches", () => {
  it("is any-of over the selected keys, through the same tokenisation", () => {
    const keys = new Set(["justicia", "perdon"]);
    expect(titleMatches("La JUSTICIA de Dios", keys)).toBe(true);
    expect(titleMatches("Sobre el perdón", keys)).toBe(true);
    expect(titleMatches("La oración", keys)).toBe(false);
  });

  it("matches everything when nothing is selected", () => {
    expect(titleMatches("cualquier cosa", new Set())).toBe(true);
  });
});

describe("topicOf", () => {
  it("joins the labels with single spaces", () => {
    expect(topicOf(["Justicia", "Perdón"])).toBe("Justicia Perdón");
    expect(topicOf([])).toBe("");
  });
});
