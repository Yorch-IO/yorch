"""PDF text extraction with PyMuPDF.

Port of ``sociologia/main.go``'s ``extractText`` / ``pageRows`` /
``filterHeaderFooter`` / ``cleanText``, with one real simplification: PyMuPDF
gives **line-level bounding boxes** via ``page.get_text("dict")``, so there is no
need to group glyphs by rounded Y coordinate the way the Go PDF library forced.

Two things carried over deliberately:

* **Header removal matches content, not position.** Position-based cutoffs clipped
  the first body line of every page. The patterns are anchored regexes, learned
  per document family, so an in-body mention of the same phrase survives.
* **Paragraph breaks come from the line gap**, at ``line_gap_factor`` times the
  page's median gap, computed per page.

One coordinate trap worth naming: PyMuPDF's origin is **top-left with y growing
downward**, the opposite of the PDF-native coordinates the Go implementation used.
So the footer zone is ``y > height * (1 - cutoff)`` here, where in Go it was
``y < height * cutoff``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

import fitz  # PyMuPDF

from ..chunk import DocRules, heading_level, ChunkRules
from . import Evidence, Extracted, join_paragraphs

# Bare page numbers that sit above the footer cutoff zone.
PAGE_NUMBER_RE = re.compile(r"^\d{1,4}$")

# Numbered section headings, e.g. "1. Cultura y sociedad", "1.1.1. El motivo".
HEADING_ROW_RE = re.compile(r"^\d+(\.\d+)*\.\s+\w", re.UNICODE)
HEADING_ROW_MAX = 80  # longer numbered rows are footnotes, not headings

# PDF encoding garbage: the Unicode replacement char and stray dingbats.
ARTIFACTS_RE = re.compile(r"[�✓]+")

# Characters in the Private Use Area come from symbol fonts and carry no Unicode
# meaning, so what they *mean* has to be read off their context. In this book's
# 174 occurrences, U+F02D is consistently an em dash opening and closing a
# parenthetical aside ("una evidente relación como ha venido sucediendo…
# programas de estudio"), so mapping it to a hyphen would be wrong.
PUA_REPLACEMENTS = {"": "—"}
PUA_RE = re.compile(r"[-]")
# Soft hyphen at a line break: hyphen + whitespace + lowercase letter.
HYPHEN_BREAK_RE = re.compile(r"-\s+(\p{Ll})".replace(r"\p{Ll}", r"[a-záéíóúüñ]"))
# The same break, typeset with a real U+00AD instead of an ASCII hyphen. It is
# invisible, so it survives every eyeball check and still splits the word for
# the tokenizer: "reproduc\xadción" indexes as `reproduc` + `cion`, and
# "propósi\xadto" loses its tail entirely because BM25 drops tokens under three
# characters. Measured at 2,140 occurrences on 01_RetoDeDios_INT-S.pdf, 368 of
# them followed by a space. Deterministic for the same reason as the PUA map:
# a discretionary hyphen has exactly one meaning.
SOFT_HYPHEN_RE = re.compile("\u00ad\\s*")
MULTI_SPACE_RE = re.compile(r"  +")

# A page with less than this much text has no usable text layer -> OCR.
MIN_TEXT_PER_PAGE = 40


@dataclass
class _Row:
    y0: float
    y1: float
    text: str
    #: Where the row starts across the page. Carried only to order rows that
    #: share a baseline — see `_page_rows`. Last, with a default, so nothing
    #: that builds a row positionally has to change.
    x0: float = 0.0


def extract(path: str, rules: DocRules) -> Extracted:
    ev = Evidence(source=path, extractor="pdf_text")
    chunk_rules = ChunkRules()
    header_res = rules.header_res()

    paragraphs: list[str] = []
    current: list[str] = []
    all_gaps: list[float] = []
    line_counter: Counter[str] = Counter()

    with fitz.open(path) as doc:
        ev.pages = doc.page_count
        for pno, page in enumerate(doc, start=1):
            height = page.rect.height or 841.0
            ev.page_height = height

            rows = _page_rows(page)
            if sum(len(r.text) for r in rows) < MIN_TEXT_PER_PAGE:
                ev.pages_without_text.append(pno)
                continue

            for r in rows:
                line_counter[r.text] += 1
            if rows:
                ev.first_lines.append(rows[0].text)
                ev.last_lines.append(rows[-1].text)

            kept = _filter_header_footer(rows, height, rules, header_res)
            if not kept:
                continue

            gaps = _line_gaps(kept)
            all_gaps.extend(gaps)
            median_gap = _median(gaps) if gaps else 12.0
            threshold = median_gap * rules.line_gap_factor

            for i, row in enumerate(kept):
                line = row.text.strip()
                if not line:
                    continue

                # Numbered headings become standalone paragraphs so downstream
                # heading detection works; gap-based detection alone merges them
                # into the following body paragraph.
                is_heading = (
                    len(line) <= HEADING_ROW_MAX and HEADING_ROW_RE.match(line) is not None
                )
                if is_heading and current:
                    paragraphs.append(" ".join(current))
                    current = []
                current.append(line)

                is_last = i == len(kept) - 1
                gap_big = not is_last and (kept[i + 1].y0 - row.y0) > threshold
                if is_last or gap_big or is_heading:
                    if current:
                        paragraphs.append(" ".join(current))
                        current = []

    if current:
        paragraphs.append(" ".join(current))

    ev.median_line_gap = _median(all_gaps) if all_gaps else 0.0
    # Header/footer candidates: lines seen on a good share of pages.
    threshold_pages = max(3, ev.pages // 4)
    ev.repeated_lines = {
        text: n
        for text, n in line_counter.most_common(40)
        if n >= threshold_pages and len(text) < 120
    }

    cleaned = [_clean_text(p) for p in paragraphs]
    cleaned = [p for p in cleaned if p]

    ev.numbered_lines = [
        p for p in cleaned if heading_level(p, chunk_rules) > 0
    ][:40]
    ev.numbered_paragraphs = [
        p[:200] for p in cleaned if re.match(r"^\d+[.)]?\s", p)
    ][:40]

    if ev.pages_without_text:
        ev.notes.append(
            f"{len(ev.pages_without_text)} of {ev.pages} pages have no text layer; "
            "run with --ocr to transcribe them"
        )

    return Extracted(text=join_paragraphs(cleaned), evidence=ev)


def _page_rows(page: "fitz.Page") -> list[_Row]:
    """Visual rows from PyMuPDF's line bboxes, top to bottom.

    ``get_text("dict")`` already groups spans into lines, which is the work the
    Go version had to do by rounding glyph Y coordinates to 6-unit buckets.
    """
    rows: list[_Row] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:  # 0 = text, 1 = image
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            text = text.replace("\n", " ").replace("\r", "").strip()
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            rows.append(_Row(y0=y0, y1=y1, text=text, x0=x0))
    # Left to right within a baseline, which is the order a person reads them.
    # The tiebreak used to be the row's own *text*, so every line sharing a
    # baseline with another came out in dictionary order: two-column tables,
    # bullets in their own text run, and any line PyMuPDF splits per span.
    # Measured 2026-09-03 over the 84 PDFs in `libros/`: 9,302 of 109,444 lines
    # share a baseline with another, 418 pages reorder, and **679 paragraphs
    # across 41 documents come out different** — while the level-1 heading count
    # over the whole set moves only 148 -> 147, so nothing downstream is
    # destabilised by it. The damage is shipped, not hypothetical:
    # `libros/done/Hermeneutica Capitulo 4.pdf.corrected.txt` reads "en el
    # pueblo judío, cada siete años se 15:2 perdona toda clase de deudas", with
    # the verse number twenty characters into its own sentence and sixty from
    # the "Deuteronomio" it belongs to — far enough that `bm25.scripture_tokens`
    # cannot mint `deuteronomio15v2` for it either. Correction cannot repair it,
    # because reformulating is the one thing that pass is forbidden to do.
    rows.sort(key=lambda r: (r.y0, r.x0))
    return rows


def _filter_header_footer(
    rows: list[_Row],
    height: float,
    rules: DocRules,
    header_res: list[re.Pattern[str]],
) -> list[_Row]:
    """Drop the footer zone, bare page numbers, and known repeating headers.

    Note the footer test: PyMuPDF y grows downward, so the footer is at *large*
    y — the inverse of the PDF-native coordinates the Go version worked in.
    """
    footer_start = height * (1.0 - rules.footer_cutoff)
    out: list[_Row] = []
    for r in rows:
        if r.y0 >= footer_start:
            continue  # footer / folio zone
        if PAGE_NUMBER_RE.match(r.text):
            continue  # bare page number above the cutoff zone
        if any(p.match(r.text) for p in header_res):
            continue  # learned repeating header
        out.append(r)
    return out


def _line_gaps(rows: list[_Row]) -> list[float]:
    """Line pitch: top-of-line to top-of-line, i.e. baseline-to-baseline.

    NOT the gap between bounding boxes. Measured on this book, the inter-bbox gap
    has a median of 2.48 points and swings by 5% between adjacent lines, so a
    1.5x threshold on it fires constantly — that produced 1064 paragraphs where
    the document has 305. The pitch has a median of 15.84 points and a 1.5x
    threshold of 23.8, which on a sample page is exceeded by exactly 2 of 39 gaps:
    the page's two real paragraph breaks.

    This is also what the Go implementation measured, since it worked from glyph
    row baselines rather than boxes.
    """
    return [
        rows[i + 1].y0 - rows[i].y0
        for i in range(len(rows) - 1)
        if rows[i + 1].y0 - rows[i].y0 > 0
    ]


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[len(s) // 2]


def _clean_text(s: str) -> str:
    """Strip PDF encoding artifacts, join soft-hyphenated words, collapse spaces.

    Note what this does NOT fix: intra-word spacing from sub-word glyph fragments
    ("s o ciedad", "nat ur a leza"). Those need the LLM correction pass.
    """
    s = ARTIFACTS_RE.sub("", s)
    for pua, replacement in PUA_REPLACEMENTS.items():
        s = s.replace(pua, replacement)
    # Anything still in the Private Use Area is a glyph we have no mapping for;
    # leaving it in would put an unrenderable character into the index.
    s = PUA_RE.sub("", s)
    s = SOFT_HYPHEN_RE.sub("", s)
    s = HYPHEN_BREAK_RE.sub(r"\1", s)
    s = MULTI_SPACE_RE.sub(" ", s)
    return s.strip()
