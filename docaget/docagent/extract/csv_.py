"""CSV extraction.

Structured, and it shares the Excel philosophy: a row of values is meaningless
without its column names, so every row window repeats the header. The dialect is
sniffed rather than assumed — these files come from many sources and semicolons
are common in Spanish-locale exports.
"""

from __future__ import annotations

import csv
import pathlib

from ..chunk import KIND_TABLE_ROW, Chunk, DocRules
from . import Evidence, Extracted
from .excel import ROWS_PER_WINDOW, render_window

_SNIFF_BYTES = 16384


def extract(path: str, rules: DocRules) -> Extracted:
    text = _read_text(path)
    dialect, has_header = _sniff(text)

    reader = csv.reader(text.splitlines(), dialect)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return Extracted(chunks=[], evidence=Evidence(source=path, extractor="csv_"))

    if has_header:
        header, body = rows[0], rows[1:]
        first_data_row = 2
    else:
        # No header: name the columns positionally so the breadcrumb still says
        # something, rather than pretending the first data row is a header.
        header = [f"col{i + 1}" for i in range(len(rows[0]))]
        body = rows
        first_data_row = 1

    table = pathlib.Path(path).stem
    chunks: list[Chunk] = []
    for start in range(0, len(body), ROWS_PER_WINDOW):
        window = body[start : start + ROWS_PER_WINDOW]
        r0 = first_data_row + start
        r1 = r0 + len(window) - 1
        chunks.append(
            Chunk(
                index=len(chunks),
                kind=KIND_TABLE_ROW,
                chapter=table,
                section=f"filas {r0}-{r1}",
                text=render_window(header, window),
                char_from=0,
                char_to=0,
                para_from=r0,
                para_to=r1,
                cell_ref=f"{table}!{r0}:{r1}",
                extra={"columns": header, "rows": len(window)},
            )
        )

    ev = Evidence(
        source=path,
        extractor="csv_",
        pages=1,
        notes=[
            f"delimiter={dialect.delimiter!r} header={has_header} "
            f"columns={len(header)} rows={len(body)}"
        ],
    )
    return Extracted(chunks=chunks, evidence=ev)


def _read_text(path: str) -> str:
    raw = pathlib.Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _sniff(text: str) -> tuple[csv.Dialect, bool]:
    sample = text[:_SNIFF_BYTES]
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.get_dialect("excel")  # type: ignore[assignment]
    try:
        has_header = sniffer.has_header(sample)
    except csv.Error:
        has_header = True
    return dialect, has_header  # type: ignore[return-value]
