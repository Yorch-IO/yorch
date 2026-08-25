"""Excel extraction.

A spreadsheet is not prose, and treating it as prose loses the one thing that
makes a row meaningful: its column names. So this extractor produces chunks
directly, and every row window **repeats the header** — the same job the
``Chapter > Section`` breadcrumb does for the book.

Three traps that are specific to real spreadsheets, all handled here:

* **Formulas.** ``openpyxl`` returns ``"=SUM(A1:A9)"`` unless opened with
  ``data_only=True``, in which case it returns the value Excel last cached.
  Indexing formula strings would be useless, so ``data_only=True`` it is — with
  the caveat that a workbook never opened by Excel has no cached values, which
  this extractor detects and reports rather than silently indexing blanks.
* **Merged cells.** A merged header spans several columns but only the top-left
  cell holds the text; the rest read as ``None``. Merged ranges are unmerged
  logically by propagating that value across the span.
* **Several tables per sheet**, separated by blank rows, each with its own header.

The verifiable anchor is ``cell_ref`` ("Ventas 2025!A41:D60") — the spreadsheet
analogue of the byte-exact ``char_span`` used for text, and checkable the same
way by reopening the workbook.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..chunk import KIND_TABLE_ROW, Chunk, DocRules
from . import Evidence, Extracted

ROWS_PER_WINDOW = 20
MAX_WINDOW_CHARS = 1200
# A header row must have at least this fraction of its cells filled with text.
HEADER_TEXT_RATIO = 0.6
# Scan at most this many rows looking for a header before giving up.
HEADER_SEARCH_DEPTH = 10

Row = list[object]


@dataclass
class Table:
    """One contiguous table inside a sheet."""

    sheet: str
    header: list[str]
    rows: list[Row]
    first_data_row: int  # 1-based row number in the sheet
    first_col: int  # 1-based
    header_row: int | None


def extract(path: str, rules: DocRules) -> Extracted:
    import openpyxl
    from openpyxl.utils import get_column_letter

    # data_only=True yields cached values instead of formula strings.
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    uncached = _uncached_formulas(path)

    chunks: list[Chunk] = []
    notes: list[str] = []

    for ws in wb.worksheets:
        grid = _materialise(ws)
        if not grid:
            continue

        tables = _split_tables(ws.title, grid)
        if not tables:
            continue

        for t in tables:
            for start in range(0, len(t.rows), ROWS_PER_WINDOW):
                window = _take_window(t.rows, start)
                if not window:
                    continue
                r0 = t.first_data_row + start
                r1 = r0 + len(window) - 1
                c0 = get_column_letter(t.first_col)
                c1 = get_column_letter(t.first_col + max(1, len(t.header)) - 1)
                chunks.append(
                    Chunk(
                        index=len(chunks),
                        kind=KIND_TABLE_ROW,
                        chapter=f"Hoja {t.sheet}",
                        section=f"filas {r0}-{r1}",
                        text=render_window(t.header, window),
                        char_from=0,
                        char_to=0,
                        para_from=r0,
                        para_to=r1,
                        cell_ref=f"{t.sheet}!{c0}{r0}:{c1}{r1}",
                        extra={
                            "sheet": t.sheet,
                            "columns": t.header,
                            "rows": len(window),
                            "header_row": t.header_row,
                        },
                    )
                )
        notes.append(
            f"{ws.title}: {len(tables)} table(s), "
            f"{sum(len(t.rows) for t in tables)} data rows"
        )

    wb.close()

    if uncached:
        detail = ", ".join(f"{sheet} ({n} cells)" for sheet, n in sorted(uncached.items()))
        notes.append(
            f"WARNING: no cached values for formulas on {detail} — the workbook was "
            "probably never opened by Excel, so those cells read as empty and are "
            "NOT indexed. Open and save the file, or that data is silently missing."
        )

    ev = Evidence(source=path, extractor="excel", pages=len(wb.worksheets), notes=notes)
    return Extracted(chunks=chunks, evidence=ev)


def _materialise(ws) -> list[Row]:
    """Rows as plain lists, with merged-cell values propagated across their span.

    A merged header cell holds its text only in the top-left position; every
    other cell in the range reads as None. Propagating fixes the common case of a
    header merged across several columns.
    """
    max_row, max_col = ws.max_row or 0, ws.max_column or 0
    if not max_row or not max_col:
        return []

    grid: list[Row] = [
        [ws.cell(row=r, column=c).value for c in range(1, max_col + 1)]
        for r in range(1, max_row + 1)
    ]

    for rng in ws.merged_cells.ranges:
        anchor = grid[rng.min_row - 1][rng.min_col - 1]
        if anchor is None:
            continue
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if grid[r - 1][c - 1] is None:
                    grid[r - 1][c - 1] = anchor
    return grid


def _uncached_formulas(path: str) -> dict[str, int]:
    """Sheet -> count of formula cells that have no cached value.

    Detected precisely rather than by heuristic, because the failure mode is
    silent data loss. openpyxl cannot evaluate formulas: with ``data_only=True``
    a formula cell whose value Excel never cached reads as ``None`` and simply
    vanishes from the index. So the workbook is opened twice — once to see which
    cells hold formulas, once to see which of those have a value — and the
    difference is reported. A second parse is cheap next to indexing blanks and
    not knowing.
    """
    import openpyxl

    formulas = openpyxl.load_workbook(path, data_only=False)
    values = openpyxl.load_workbook(path, data_only=True)
    out: dict[str, int] = {}
    try:
        for ws in formulas.worksheets:
            vws = values[ws.title]
            missing = 0
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        if vws[cell.coordinate].value is None:
                            missing += 1
            if missing:
                out[ws.title] = missing
    finally:
        formulas.close()
        values.close()
    return out


def _split_tables(sheet: str, grid: list[Row]) -> list[Table]:
    """Split a sheet into tables on fully empty rows, finding each one's header."""
    tables: list[Table] = []
    block_start = 0
    blocks: list[tuple[int, list[Row]]] = []

    for i, row in enumerate(grid):
        if _row_empty(row):
            if i > block_start:
                blocks.append((block_start, grid[block_start:i]))
            block_start = i + 1
    if block_start < len(grid):
        blocks.append((block_start, grid[block_start:]))

    for offset, block in blocks:
        trimmed, first_col = _trim_columns(block)
        if not trimmed:
            continue
        h_idx = _find_header(trimmed)
        if h_idx is None:
            header = [f"col{i + 1}" for i in range(len(trimmed[0]))]
            rows = trimmed
            first_data_row = offset + 1
            header_row = None
        else:
            header = _header_names(trimmed[: h_idx + 1])
            rows = trimmed[h_idx + 1 :]
            first_data_row = offset + h_idx + 2
            header_row = offset + h_idx + 1

        rows = [r for r in rows if not _row_empty(r)]
        if rows:
            tables.append(
                Table(
                    sheet=sheet,
                    header=header,
                    rows=rows,
                    first_data_row=first_data_row,
                    first_col=first_col,
                    header_row=header_row,
                )
            )
    return tables


def _trim_columns(block: list[Row]) -> tuple[list[Row], int]:
    """Drop leading and trailing all-empty columns, returning the 1-based index
    of the first kept column so cell_ref stays truthful."""
    if not block:
        return [], 1
    width = max(len(r) for r in block)
    padded = [list(r) + [None] * (width - len(r)) for r in block]
    used = [c for c in range(width) if any(row[c] is not None for row in padded)]
    if not used:
        return [], 1
    lo, hi = used[0], used[-1]
    return [row[lo : hi + 1] for row in padded], lo + 1


def _find_header(block: list[Row]) -> int | None:
    """Index of the last header row, supporting multi-row headers.

    A header row is mostly text; the row under it must contain at least one
    non-text value (number, date) or the block is all-text and the first row is
    taken as the header.
    """
    depth = min(HEADER_SEARCH_DEPTH, len(block) - 1)
    for i in range(depth):
        if not _mostly_text(block[i]):
            continue
        # Extend downward while the following rows are also mostly text: that is
        # a multi-row header.
        last = i
        while last + 1 < depth and _mostly_text(block[last + 1]):
            last += 1
        below = block[last + 1] if last + 1 < len(block) else None
        if below is not None and (not _mostly_text(below) or _has_non_text(below)):
            return last
        if below is not None and _mostly_text(below):
            return last
    if block and _mostly_text(block[0]):
        return 0
    return None


def _header_names(header_rows: list[Row]) -> list[str]:
    """Flatten one or more header rows into one name per column."""
    width = max(len(r) for r in header_rows)
    names: list[str] = []
    for c in range(width):
        parts = [
            str(r[c]).strip()
            for r in header_rows
            if c < len(r) and r[c] is not None and str(r[c]).strip()
        ]
        # Deduplicate repeats introduced by merged-cell propagation.
        seen: list[str] = []
        for p in parts:
            if p not in seen:
                seen.append(p)
        names.append(" / ".join(seen) if seen else f"col{c + 1}")
    return names


def _row_empty(row: Row) -> bool:
    return all(v is None or (isinstance(v, str) and not v.strip()) for v in row)


def _mostly_text(row: Row) -> bool:
    filled = [v for v in row if v is not None and str(v).strip()]
    if not filled:
        return False
    text = sum(1 for v in filled if isinstance(v, str) and not _numeric_str(v))
    return text / len(filled) >= HEADER_TEXT_RATIO


def _has_non_text(row: Row) -> bool:
    return any(
        v is not None and (not isinstance(v, str) or _numeric_str(v)) for v in row
    )


def _numeric_str(v: str) -> bool:
    s = v.strip().replace(",", "").replace("%", "")
    try:
        float(s)
        return True
    except ValueError:
        return False


def _take_window(rows: list[Row], start: int) -> list[Row]:
    """Up to ROWS_PER_WINDOW rows, cut early if the rendered text would get long.

    Wide tables hit the character budget well before the row count, and an
    oversized chunk would be truncated by the embedding model rather than split.
    """
    window: list[Row] = []
    size = 0
    for row in rows[start : start + ROWS_PER_WINDOW]:
        rendered = _render_row(row)
        if window and size + len(rendered) > MAX_WINDOW_CHARS:
            break
        window.append(row)
        size += len(rendered)
    return window


def render_window(header: list[str], rows: list[Row]) -> str:
    """Render a row window as a pipe table with the header repeated.

    The header is inside every chunk on purpose: a retrieved window of numbers is
    unreadable without it, and the embedding needs the column names to place the
    values semantically.
    """
    lines = [" | ".join(header)]
    lines.append("-" * min(len(lines[0]), 80))
    for row in rows:
        lines.append(_render_row(row))
    return "\n".join(lines)


def _render_row(row: Row) -> str:
    return " | ".join("" if v is None else str(v).strip() for v in row)
