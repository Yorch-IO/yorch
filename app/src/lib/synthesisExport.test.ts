/**
 * What a synthesis looks like as a file, and the one property that matters:
 * every factual row carries the link that proves it, and an inference is never
 * in a row that looks like one.
 */
import { describe, expect, it } from "vitest";

import type { Synthesis } from "./api";
import { exportName, rowsFor, splitLocator, toCsv, toJson } from "./synthesisExport";

const synthesis = (over: Partial<Synthesis> = {}): Synthesis => ({
  state: "answered",
  topic: "justicia social y pobreza",
  model: "gemini-3.6-flash",
  promptVersion: "synthesis/1",
  hallazgos: [
    { afirmacion: "Se predica la justicia", chunkIds: ["chk_1", "chk_2"] },
  ],
  comparacion: {
    convergencias: ["las dos apelan a Miqueas"],
    diferencias: [],
    matices: ["una lo hace en clave personal"],
  },
  interpretacionTeologica: [
    {
      inferencia: "Hay un énfasis profético",
      alcance: "estas dos prédicas",
      limites: "no permite hablar del canal entero",
    },
  ],
  citas: [
    {
      chunkId: "chk_1",
      locator: "1:16:13 · https://youtu.be/abc?t=4573",
      claim: "hagamos justicia",
      page: null,
      sectionTitle: null,
    },
    {
      chunkId: "chk_2",
      locator: "0:14 · https://youtu.be/def?t=14",
      claim: "amemos misericordia",
      page: null,
      sectionTitle: null,
    },
  ],
  limitaciones: ["solo dos prédicas"],
  evidence: [],
  demoted: 1,
  invented: 0,
  reason: "",
  spend: [],
  ...over,
});

describe("splitLocator", () => {
  it("reads the two facts a timed locator carries", () => {
    expect(splitLocator("1:16:13 · https://youtu.be/abc?t=4573")).toEqual({
      momento: "1:16:13",
      url: "https://youtu.be/abc?t=4573",
    });
  });

  it("degrades rather than inventing halves it does not have", () => {
    // A book's locator carries a title and a byte range and no clock at all.
    expect(splitLocator("El reto de Dios · p. 12 · [100:400]")).toEqual({
      momento: "",
      url: "",
    });
    expect(splitLocator("")).toEqual({ momento: "", url: "" });
  });
});

describe("rowsFor", () => {
  it("writes one row per citation, not per finding", () => {
    // The unit somebody checks is a claim against a place in a video, and a
    // cell holding three links is a cell nobody clicks.
    const rows = rowsFor(synthesis()).filter((r) => r.tipo === "hallazgo");
    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.momento)).toEqual(["1:16:13", "0:14"]);
    expect(rows[0]!.url).toBe("https://youtu.be/abc?t=4573");
    expect(rows.every((r) => r.texto === "Se predica la justicia")).toBe(true);
  });

  it("gives an inference no link, because it rests on findings and not on a fragment", () => {
    const [row] = rowsFor(synthesis()).filter((r) => r.tipo === "interpretacion");
    expect(row!.texto).toBe("Hay un énfasis profético");
    expect(row!.chunkId).toBe("");
    expect(row!.url).toBe("");
    // Its scope and its limits travel with it: an inference with neither is
    // indistinguishable from a finding.
    expect(row!.nota).toBe("estas dos prédicas — no permite hablar del canal entero");
  });

  it("keeps the four kinds of row apart", () => {
    const kinds = rowsFor(synthesis()).map((r) => r.tipo);
    expect(new Set(kinds)).toEqual(
      new Set(["hallazgo", "interpretacion", "comparacion", "limitacion"]),
    );
  });

  it("names which part of the comparison a row came from", () => {
    const rows = rowsFor(synthesis()).filter((r) => r.tipo === "comparacion");
    expect(rows.map((r) => r.nota)).toEqual(["convergencias", "matices"]);
  });

  it("writes no row for a chunk id whose citation did not survive", () => {
    // An empty link beside a claim reads as a claim with a blank link, which is
    // worse than the claim being absent from the file.
    const rows = rowsFor(
      synthesis({
        hallazgos: [{ afirmacion: "Se predica la justicia", chunkIds: ["chk_ausente"] }],
      }),
    );
    expect(rows.filter((r) => r.tipo === "hallazgo")).toEqual([]);
  });
});

describe("toCsv", () => {
  it("quotes a field that would otherwise break the row", () => {
    const csv = toCsv(
      synthesis({
        limitaciones: ['dice "justicia", no "caridad"', "una coma, aquí"],
      }),
    );
    expect(csv).toContain('"dice ""justicia"", no ""caridad"""');
    expect(csv).toContain('"una coma, aquí"');
  });

  it("carries a byte-order mark, for the one tool that needs it", () => {
    // This corpus is Spanish and Excel reads a UTF-8 file without a BOM as
    // Latin-1, so «Teología» arrives as "TeologÃ­a" in the tool the people who
    // asked for a spreadsheet are most likely to open it in.
    expect(toCsv(synthesis()).startsWith("﻿")).toBe(true);
  });

  it("starts with a header naming every column", () => {
    const [header] = toCsv(synthesis()).replace("﻿", "").split("\r\n");
    expect(header).toBe("tipo,texto,chunk_id,locator,url,momento,cita,nota");
  });

  it("has a row for every row the record has", () => {
    const lines = toCsv(synthesis()).trim().split("\r\n");
    expect(lines).toHaveLength(rowsFor(synthesis()).length + 1);
  });
});

describe("toJson", () => {
  it("keeps the counts a CSV row cannot carry", () => {
    // Facts about the synthesis rather than about any row of it, and a reader
    // deciding whether to trust the file needs them.
    const parsed = JSON.parse(toJson(synthesis()));
    expect(parsed.demoted).toBe(1);
    expect(parsed.invented).toBe(0);
    expect(parsed.promptVersion).toBe("synthesis/1");
    expect(parsed.model).toBe("gemini-3.6-flash");
  });
});

describe("exportName", () => {
  it("folds accents and spaces out, because a filesystem is the consumer", () => {
    expect(exportName("Teología social y economía", "csv")).toMatch(
      /^sintesis-teologia-social-y-economia-\d{4}-\d{2}-\d{2}\.csv$/,
    );
  });

  it("still names a file when the topic reduces to nothing", () => {
    expect(exportName("¿?¡!", "json")).toMatch(/^sintesis-sintesis-\d{4}/);
  });
});
