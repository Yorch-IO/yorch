"""A page with a text layer must not be discarded as having none.

`MIN_TEXT_PER_PAGE` answered two questions at once and they are not the same
question. "Is this page worth running OCR over" is a judgement about the scan;
"should we keep the characters we already extracted" is not a judgement at all.
Coupling them deleted real prose, and silently — nothing raises, the text is
simply shorter than the document.

Measured over the 84 PDFs in `libros/` before the split: 146 of 3,514 pages
fall under the threshold and **122 of those `_filter_header_footer` empties
anyway**, so the coupling only ever decided 24. Two of the 24 are sentence
tails — `teológica nunca del todo dirimida.` and `la iglesia tradicional.` —
and because a paragraph flushes at each page's last row those were lost whole,
not truncated. Paragraph count over the corpus moved 16,717 -> 16,749, and the
OCR recommendation is unchanged at 146 pages, which is the property that had to
survive: the threshold still means what it meant.
"""

from __future__ import annotations

import pathlib

import pytest

fitz = pytest.importorskip("fitz")

from docagent.extract import pdf_text  # noqa: E402
from docagent.rules import DocRules  # noqa: E402

#: Comfortably under `MIN_TEXT_PER_PAGE` and unmistakably prose — the shape of
#: the two real losses, a sentence stranded at the top of its own page.
THIN = "teológica nunca del todo dirimida."


def _pdf(tmp_path: pathlib.Path, pages: list[list[str]]) -> pathlib.Path:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72.0
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 16.0
    path = tmp_path / "thin.pdf"
    doc.save(str(path))
    doc.close()
    return path


def _extract(path: pathlib.Path):
    return pdf_text.extract(path, DocRules())


def test_a_thin_page_keeps_its_text(tmp_path: pathlib.Path):
    assert len(THIN) < pdf_text.MIN_TEXT_PER_PAGE, "the fixture must be under the threshold"
    fat = ["Una página cualquiera con bastante texto corrido para superar el umbral",
           "sin ninguna dificultad, porque lo que se prueba aquí es la otra."]
    ex = _extract(_pdf(tmp_path, [fat, [THIN]]))
    blob = ex.text if isinstance(ex.text, bytes) else ex.text.encode("utf-8")
    assert THIN.encode("utf-8") in blob, (
        "a page's own prose was deleted because the page was a candidate for OCR"
    )


def test_a_thin_page_is_still_recommended_for_ocr(tmp_path: pathlib.Path):
    """Keeping the text must not cost the recommendation. The constant still
    means what it meant, which is the whole point of splitting the two."""
    ex = _extract(_pdf(tmp_path, [["Texto largo y suficiente para pasar el umbral de sobra aquí."],
                                  [THIN]]))
    assert ex.evidence.pages_without_text == [2]


def test_a_page_with_no_text_at_all_still_contributes_nothing(tmp_path: pathlib.Path):
    """The `continue` that was removed is not needed for a genuinely blank page:
    `rows` is empty, so the header/footer filter returns nothing and the
    `if not kept` guard below skips it exactly as before. This is what makes the
    removal safe rather than merely permissive."""
    ex = _extract(_pdf(tmp_path, [["Texto largo y suficiente para pasar el umbral de sobra aquí."],
                                  []]))
    blob = ex.text if isinstance(ex.text, bytes) else ex.text.encode("utf-8")
    paragraphs = [p for p in blob.split(b"\n\n") if p.strip()]
    assert len(paragraphs) == 1
    assert ex.evidence.pages_without_text == [2]


def test_a_thin_page_gets_a_position_like_every_other(tmp_path: pathlib.Path):
    """The sidecar is filtered in lockstep with the text (invariant from
    `test_paragraph_positions`), so a paragraph recovered here must not be the
    one that puts the two out of step — after which every later citation names
    a confidently wrong page."""
    ex = _extract(_pdf(tmp_path, [["Texto largo y suficiente para pasar el umbral de sobra aquí."],
                                  [THIN]]))
    blob = ex.text if isinstance(ex.text, bytes) else ex.text.encode("utf-8")
    paragraphs = [p for p in blob.split(b"\n\n") if p.strip()]
    assert len(ex.positions) == len(paragraphs)
    assert ex.positions[-1].page == 2, "the recovered paragraph must name its own page"
