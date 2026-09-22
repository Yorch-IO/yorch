"""Where a paragraph came from, and the two ways that can silently go wrong.

A citation that names the wrong page is worse than one that names none: the
reader goes to page 214, does not find the sentence, and learns not to trust
the locator. Neither failure below raises anything, and neither is visible in
the extracted text — so they are asserted directly on the positions.
"""

from __future__ import annotations

import pathlib

import pytest

from docagent.extract import (
    ParagraphPosition,
    Extracted,
    join_paragraphs,
    kept_paragraphs,
)
from docagent.chunk import split_paragraphs

fitz = pytest.importorskip("fitz")


def _pos(para: int = -1, page: int = 1, page_to: int | None = None) -> ParagraphPosition:
    return ParagraphPosition(
        para=para, page=page, page_to=page if page_to is None else page_to,
        x0=0.0, y0=0.0, x1=1.0, y1=1.0,
    )


# --- hazard 1: two filters, one step apart -----------------------------------


def test_the_text_and_its_positions_are_filtered_by_one_rule_and_not_two():
    """`extract` dropped empties twice — once itself, once inside
    `join_paragraphs` — and a positions list filtered by either rule alone goes
    one out of step at the first paragraph the *other* rule drops. Every
    citation after that names a confidently wrong page."""
    pairs = [
        ("primero", _pos(page=1)),
        # Non-empty to a bare `if p`, empty to `join_paragraphs`'s `p.strip()`.
        # This is exactly the paragraph the two rules disagreed about.
        ("   ", _pos(page=2)),
        ("segundo", _pos(page=3)),
    ]
    texts, positions = kept_paragraphs(pairs)

    assert texts == ["primero", "segundo"]
    assert [p.page for p in positions] == [1, 3]
    # And the index is the one the stream will actually have.
    assert [p.para for p in positions] == [0, 1]


def test_the_paragraph_index_is_the_one_the_joined_stream_reproduces():
    """The join key for the whole pipeline. `join_paragraphs` writes `\\n\\n`
    and `split_paragraphs` reproduces `0..N-1` against it; a position whose
    `para` came from before the filtering would index a different paragraph."""
    pairs = [("uno", _pos()), ("", _pos()), ("dos", _pos()), ("  ", _pos()), ("tres", _pos())]
    texts, positions = kept_paragraphs(pairs)

    parsed = split_paragraphs(join_paragraphs(texts))
    assert [p.idx for p in parsed] == [pos.para for pos in positions]
    assert [p.text for p in parsed] == texts


def test_a_stream_with_no_positions_stays_empty_rather_than_inventing_them():
    """A `.txt` has no pages and a spreadsheet has no paragraphs. Empty means
    "not knowable"; a page 0 would be a claim a reader cannot check."""
    texts, positions = kept_paragraphs([("uno", None), ("dos", None)])
    assert texts == ["uno", "dos"] and positions == []
    assert Extracted(text=b"x").positions == []


# --- hazard 2: a paragraph that survives a page break ------------------------


def _pdf(tmp_path: pathlib.Path, pages: list[list[str]]) -> pathlib.Path:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72.0
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 16.0
    path = tmp_path / "doc.pdf"
    doc.save(str(path))
    doc.close()
    return path


def test_every_paragraph_carries_the_page_it_starts_on(tmp_path: pathlib.Path):
    from docagent.chunk import DocRules
    from docagent.extract import pdf_text

    # Every page well over `MIN_TEXT_PER_PAGE`. A 37-character page is
    # discarded as having no text layer at all — a recorded defect of its own,
    # measured at 26 real pages across the corpus — and a fixture that tripped
    # it would report this test failing for a reason it is not about.
    path = _pdf(tmp_path, [
        ["La prudencia ordena la razon practica hacia el obrar humano."],
        ["Santo Tomas la llama auriga virtutum, porque conduce a las demas."],
        ["Sin ella la fortaleza se vuelve temeridad y la justicia rigor."],
    ])
    out = pdf_text.extract(str(path), DocRules())

    assert out.positions, "a PDF with a text layer must place its paragraphs"
    assert len(out.positions) == len(split_paragraphs(out.text))
    assert {p.page for p in out.positions} == {1, 2, 3}


def test_the_page_sequence_never_goes_backwards(tmp_path: pathlib.Path):
    """A paragraph is page-confined *except* when the last kept row of a page
    strips to empty, and then `current` survives into the next page. Whatever
    the assembly does, a reader walking the document must never be sent
    backwards — that is the cheap guard on the whole join."""
    from docagent.chunk import DocRules
    from docagent.extract import pdf_text

    path = _pdf(tmp_path, [
        ["Primera linea de la primera pagina del documento.",
         "Segunda linea de esa misma primera pagina."],
        ["   ", "Una linea real en la segunda pagina del documento."],
        ["Tercera pagina con su propio parrafo bien entero."],
    ])
    out = pdf_text.extract(str(path), DocRules())

    pages = [p.page for p in out.positions]
    assert pages == sorted(pages), f"page sequence goes backwards: {pages}"
    # And a paragraph that does span a break says so rather than hiding it.
    for p in out.positions:
        assert p.page_to >= p.page


def test_the_box_covers_the_rows_on_the_page_the_paragraph_starts_on(
    tmp_path: pathlib.Path,
):
    """A union across a page break would be a rectangle covering nothing: the
    two pages' coordinate spaces are unrelated."""
    from docagent.chunk import DocRules
    from docagent.extract import pdf_text

    path = _pdf(tmp_path, [
        ["Una linea de texto.", "Otra linea justo debajo de la primera."]
    ])
    out = pdf_text.extract(str(path), DocRules())

    for p in out.positions:
        assert p.x1 > p.x0 and p.y1 > p.y0, f"degenerate box: {p}"
        assert p.page >= 1
