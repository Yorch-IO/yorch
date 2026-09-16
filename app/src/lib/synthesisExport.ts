/**
 * A synthesis as a file somebody can audit line by line.
 *
 * Pure, and its own module for the reason the rest of `lib/` is: what is worth
 * asserting is the *shape of the record* — that every factual row carries the
 * link that proves it, and that an inference is never in a row that looks like
 * one — and a rendered test could not see any of that.
 *
 * **One row per citation, not per finding.** A finding resting on three
 * fragments is three rows, because the unit somebody checks is a claim against
 * a place in a video, and a cell holding three links is a cell nobody clicks.
 * `tipo` is what keeps the four kinds of row apart in one file: collapsing
 * `hallazgo` and `interpretacion` into undifferentiated rows would put the text
 * a reader must treat sceptically in the same column as the text they can
 * verify — the one thing the five-section shape exists to prevent.
 */
import type { Citation, Synthesis } from "./api";

/** The four kinds of row, Spanish on the wire like everything else here. */
export type RowKind = "hallazgo" | "interpretacion" | "comparacion" | "limitacion";

export interface ExportRow {
  tipo: RowKind;
  texto: string;
  /** Empty for everything that is not a finding: an inference rests on the
   *  findings, not directly on a fragment, and pretending otherwise would give
   *  it a checkability it does not have. */
  chunkId: string;
  locator: string;
  url: string;
  momento: string;
  cita: string;
  /** `alcance`/`limites` for an inference, the comparison's own heading for a
   *  comparison, empty otherwise. */
  nota: string;
}

/** `1:16:13 · https://youtu.be/x?t=4573` -> its two halves.
 *
 *  Parsed rather than recomputed: the locator *is* the citation's identity —
 *  `citation_id` is `digest(chunk, locator)` — so anything derived from it has
 *  to come out of the same string the graph stored. */
export function splitLocator(locator: string): { momento: string; url: string } {
  const parts = (locator || "").split(" · ");
  const url = parts.find((p) => p.startsWith("http")) ?? "";
  const momento = parts.find((p) => /^\d+:\d/.test(p)) ?? "";
  return { momento, url };
}

export function rowsFor(synthesis: Synthesis): ExportRow[] {
  const byId = new Map<string, Citation>();
  for (const c of synthesis.citas) if (!byId.has(c.chunkId)) byId.set(c.chunkId, c);

  const rows: ExportRow[] = [];
  for (const finding of synthesis.hallazgos) {
    // A finding with no citation cannot reach here — it was demoted to
    // `limitaciones` before the payload was built — but a chunk id naming a
    // citation that did not survive still has to produce no row rather than an
    // empty one, which would read as a claim with a blank link.
    for (const id of finding.chunkIds) {
      const cita = byId.get(id);
      if (!cita) continue;
      const { momento, url } = splitLocator(cita.locator);
      rows.push({
        tipo: "hallazgo",
        texto: finding.afirmacion,
        chunkId: id,
        locator: cita.locator,
        url,
        momento,
        cita: cita.claim,
        nota: cita.sectionTitle ?? "",
      });
    }
  }
  for (const item of synthesis.interpretacionTeologica) {
    rows.push({
      tipo: "interpretacion",
      texto: item.inferencia,
      chunkId: "",
      locator: "",
      url: "",
      momento: "",
      cita: "",
      nota: [item.alcance, item.limites].filter(Boolean).join(" — "),
    });
  }
  const comparison: [string, string[]][] = [
    ["convergencias", synthesis.comparacion.convergencias],
    ["diferencias", synthesis.comparacion.diferencias],
    ["matices", synthesis.comparacion.matices],
  ];
  for (const [heading, items] of comparison) {
    for (const text of items) {
      rows.push({
        tipo: "comparacion",
        texto: text,
        chunkId: "",
        locator: "",
        url: "",
        momento: "",
        cita: "",
        nota: heading,
      });
    }
  }
  for (const text of synthesis.limitaciones) {
    rows.push({
      tipo: "limitacion",
      texto: text,
      chunkId: "",
      locator: "",
      url: "",
      momento: "",
      cita: "",
      nota: "",
    });
  }
  return rows;
}

const HEADER = [
  "tipo",
  "texto",
  "chunk_id",
  "locator",
  "url",
  "momento",
  "cita",
  "nota",
] as const;

function cell(value: string): string {
  const text = value ?? "";
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/**
 * The rows as CSV, with a byte-order mark.
 *
 * The BOM is there for one named consumer: this corpus is Spanish, and Excel
 * reads a UTF-8 file without one as Latin-1 — so «Teología» arrives as
 * "TeologÃ­a" in the one tool the people who asked for a spreadsheet are most
 * likely to open it in. LibreOffice and every command-line tool skip it.
 */
export function toCsv(synthesis: Synthesis): string {
  const lines = [HEADER.join(",")];
  for (const row of rowsFor(synthesis)) {
    lines.push(
      [
        row.tipo,
        row.texto,
        row.chunkId,
        row.locator,
        row.url,
        row.momento,
        row.cita,
        row.nota,
      ]
        .map(cell)
        .join(","),
    );
  }
  return "﻿" + lines.join("\r\n") + "\r\n";
}

/** The whole record, unflattened — including the counts a CSV row cannot carry.
 *
 *  `demoted` and `invented` travel here because they are facts about the
 *  synthesis rather than about any row of it, and a reader deciding whether to
 *  trust the file needs them. */
export function toJson(synthesis: Synthesis): string {
  return JSON.stringify(synthesis, null, 2);
}

/** A filename that says which channel, which topic and which day.
 *
 *  Accents and spaces are folded out rather than escaped: this string reaches a
 *  file dialog and then a filesystem, and the one that does not accept `«»` is
 *  the one somebody is using. */
export function exportName(topic: string, extension: string): string {
  const slug =
    topic
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 48) || "sintesis";
  const day = new Date().toISOString().slice(0, 10);
  return `sintesis-${slug}-${day}.${extension}`;
}
