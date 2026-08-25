"""Scanned-PDF extraction: render each page and have Gemini transcribe it.

Verified working through the same ``x-goog-api-key`` path as everything else —
inline base64 PNG on ``gemini-2.5-flash:generateContent``.

**Measured cost: $0.00068 per page** (2,597 input + 233 output tokens on a dense
A5 page at 200 DPI). That is about 8x the cost of embedding this project's entire
175-page book, so OCR is gated: the caller sees an estimate first and has to pass
``ocr_confirm`` above ``CONFIRM_THRESHOLD_USD``.

Transcriptions are cached on disk by page-image hash, so a re-run — or a tuning
loop that re-chunks the same document — never pays twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re

import fitz  # PyMuPDF

from ..chunk import DocRules
from ..vertex import Vertex
from . import Evidence, Extracted, join_paragraphs
from .pdf_text import MIN_TEXT_PER_PAGE, PAGE_NUMBER_RE, _clean_text

DPI = 200
# Measured on a real page; used only for the pre-flight estimate, never for
# billing — the report always uses the API's own usageMetadata.
EST_INPUT_TOKENS_PER_PAGE = 2600
EST_OUTPUT_TOKENS_PER_PAGE = 250
CONFIRM_THRESHOLD_USD = 0.05

CACHE_DIR = pathlib.Path("cache/ocr")

OCR_PROMPT = """Transcribe literalmente todo el texto de esta página de un libro.

Reglas estrictas:
- Devuelve ÚNICAMENTE el texto, sin comentarios ni descripciones de la imagen.
- Conserva los saltos de párrafo como líneas en blanco.
- Conserva los encabezados numerados tal cual (ej: "1. Cultura y sociedad").
- Une las palabras cortadas por guion al final de línea.
- Si la página está en blanco o solo tiene una imagen sin texto, devuelve una cadena vacía.
- NO corrijas, NO resumas, NO reordenes."""


class OcrNotConfirmed(RuntimeError):
    """Raised when the estimated cost needs explicit confirmation."""


def estimate_usd(pages: int) -> float:
    from ..ledger import FLASH_INPUT_PER_M, FLASH_OUTPUT_PER_M

    return (
        pages * EST_INPUT_TOKENS_PER_PAGE / 1e6 * FLASH_INPUT_PER_M
        + pages * EST_OUTPUT_TOKENS_PER_PAGE / 1e6 * FLASH_OUTPUT_PER_M
    )


def extract(
    path: str,
    rules: DocRules,
    *,
    vertex: Vertex | None = None,
    ocr_confirm: bool = False,
    pages: list[int] | None = None,
) -> Extracted:
    """Transcribe the requested pages (default: those with no text layer).

    Pages that already have a text layer are left to ``pdf_text``; mixing the two
    is normal in scanned books that have an OCR'd front matter.
    """
    ev = Evidence(source=path, extractor="pdf_ocr")
    own_vertex = vertex is None
    v = vertex or Vertex()

    try:
        with fitz.open(path) as doc:
            ev.pages = doc.page_count
            targets = pages if pages is not None else _pages_without_text(doc)
            ev.pages_without_text = list(targets)

            if not targets:
                ev.notes.append("every page has a text layer; nothing to OCR")
                return Extracted(text=b"", evidence=ev)

            # Pre-flight cost gate. Cached pages are free, so only count misses.
            renders = {n: _render(doc, n) for n in targets}
            misses = [n for n, png in renders.items() if _cached(png) is None]
            estimate = estimate_usd(len(misses))
            ev.notes.append(
                f"OCR: {len(targets)} pages, {len(misses)} not cached, "
                f"estimated ${estimate:.4f} at ${estimate_usd(1):.5f}/page (measured)"
            )
            if estimate > CONFIRM_THRESHOLD_USD and not ocr_confirm:
                raise OcrNotConfirmed(
                    f"OCR of {len(misses)} pages is estimated at ${estimate:.4f}, "
                    f"above the ${CONFIRM_THRESHOLD_USD:.2f} threshold. "
                    "Re-run with --ocr-confirm to proceed."
                )

            paragraphs: list[str] = []
            for n in targets:
                png = renders[n]
                text = _cached(png)
                if text is None:
                    text = v.generate(
                        OCR_PROMPT, image_png=png, stage="ocr", temperature=0.0
                    )
                    _store(png, text)
                else:
                    v.ledger.record("ocr", "gemini-2.5-flash", calls=0, cache_hits=1)
                paragraphs.extend(_page_paragraphs(text, rules))
    finally:
        if own_vertex:
            v.close()

    return Extracted(text=join_paragraphs(paragraphs), evidence=ev)


def _pages_without_text(doc: "fitz.Document") -> list[int]:
    return [
        n
        for n, page in enumerate(doc, start=1)
        if len(page.get_text().strip()) < MIN_TEXT_PER_PAGE
    ]


def _render(doc: "fitz.Document", page_no: int) -> bytes:
    return doc[page_no - 1].get_pixmap(dpi=DPI).tobytes("png")


def _page_paragraphs(text: str, rules: DocRules) -> list[str]:
    """Split a transcription into paragraphs, applying the same content-based
    header and folio filtering the text path uses.

    The model transcribes the running header too — it is on the page, after all —
    so it has to be dropped here by the learned patterns rather than by position.
    """
    header_res = rules.header_res()
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = _clean_text(" ".join(line.strip() for line in block.splitlines()))
        if not block:
            continue
        if PAGE_NUMBER_RE.match(block):
            continue
        if any(p.match(block) for p in header_res):
            continue
        out.append(block)
    return out


def _key(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()


def _cache_path(png: bytes) -> pathlib.Path:
    return CACHE_DIR / f"{_key(png)}.json"


def _cached(png: bytes) -> str | None:
    p = _cache_path(png)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))["text"]
    except (OSError, ValueError, KeyError):
        return None


def _store(png: bytes, text: str) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    _cache_path(png).write_text(
        json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8"
    )
