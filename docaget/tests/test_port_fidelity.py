"""Port fidelity: the Python chunker must reproduce the Go implementation exactly.

The Go pipeline in ``sociologia/`` was measured on a real book. If this port
disagrees with those numbers, the port has a bug — this test is the cheapest and
strongest regression signal available, and it costs nothing to run.

Reference numbers, measured from ``sociologia/indexer -dry-run`` on
``output_corrected_peluquiado.txt``:

    418,300 bytes · 305 paragraphs -> 328 chunks
    kinds: cuerpo=309 · preguntas=10 · nota=9
    text sizes: min 53 · median 1390 · max 1998 (hard cap 2000)
    embedded:   median 1518 · max 2268 (cap 2600) · 246/328 carry an overlap
    4 chapters · 59 distinct section paths
    question chunks carrying a section path: 0
"""

from __future__ import annotations

import pathlib

import pytest

from docagent.chunk import (
    KIND_BODY,
    KIND_FOOTNOTE,
    KIND_QUESTIONS,
    ChunkRules,
    build_chunks,
    classify_kind,
    heading_level,
    read_source,
)

BOOK = (
    pathlib.Path(__file__).resolve().parents[2]
    / "sociologia"
    / "output_corrected_peluquiado.txt"
)

# Everything the Go implementation reported, as one table.
EXPECTED = {
    "source_bytes": 418_300,
    "paragraphs": 305,
    "chunks": 328,
    "kinds": {KIND_BODY: 309, KIND_QUESTIONS: 10, KIND_FOOTNOTE: 9},
    "chapters": [
        "1. Cultura y sociedad",
        "2. Modernidad",
        "3. Posmodernidad",
        "4. Globalización",
    ],
    "chunks_per_chapter": {
        "1. Cultura y sociedad": 56,
        "2. Modernidad": 109,
        "3. Posmodernidad": 62,
        "4. Globalización": 101,
    },
    "distinct_sections": 59,
    "text_min": 53,
    "text_median": 1390,
    "text_max": 1998,
    "embed_median": 1518,
    "embed_max": 2268,
    "with_overlap": 246,
}


@pytest.fixture(scope="module")
def book():
    if not BOOK.exists():
        pytest.skip(f"reference book not found: {BOOK}")
    src, paras = read_source(str(BOOK))
    return src, paras, build_chunks(src, paras, ChunkRules())


def test_source_and_paragraphs(book):
    src, paras, _ = book
    assert len(src) == EXPECTED["source_bytes"]
    assert len(paras) == EXPECTED["paragraphs"]


def test_chunk_count(book):
    _, _, chunks = book
    assert len(chunks) == EXPECTED["chunks"]


def test_kind_distribution(book):
    """The nine footnotes are the ones a merely-optional dot misclassifies as
    review questions. If this fails with preguntas=19, NUMBERED_ITEM_RE has
    started matching footnotes again."""
    _, _, chunks = book
    counts: dict[str, int] = {}
    for c in chunks:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    assert counts == EXPECTED["kinds"]


def test_chapters_and_sections(book):
    _, _, chunks = book
    seen: list[str] = []
    for c in chunks:
        if c.chapter and c.chapter not in seen:
            seen.append(c.chapter)
    assert seen == EXPECTED["chapters"]

    per_chapter: dict[str, int] = {}
    for c in chunks:
        per_chapter[c.chapter] = per_chapter.get(c.chapter, 0) + 1
    assert per_chapter == EXPECTED["chunks_per_chapter"]

    sections = {c.section for c in chunks if c.section}
    assert len(sections) == EXPECTED["distinct_sections"]


def test_review_questions_never_carry_a_section_path(book):
    """Invariant #11. This is the defect the whole exercise set out to fix: a
    chapter's review questions inheriting whichever subsection came last."""
    _, _, chunks = book
    offenders = [c for c in chunks if c.kind == KIND_QUESTIONS and c.section]
    assert offenders == [], [
        (c.index, c.breadcrumb()) for c in offenders
    ]


def test_no_chunk_exceeds_the_caps(book):
    """Invariants #2 and #3."""
    _, _, chunks = book
    rules = ChunkRules()
    for c in chunks:
        assert len(c.text.encode()) <= rules.hard_cap_chars, c.index
        assert len(c.embed_text().encode()) <= rules.max_embed_chars, c.index


def test_size_distribution(book):
    _, _, chunks = book
    sizes = sorted(len(c.text.encode()) for c in chunks)
    embedded = sorted(len(c.embed_text().encode()) for c in chunks)
    assert sizes[0] == EXPECTED["text_min"]
    assert sizes[len(sizes) // 2] == EXPECTED["text_median"]
    assert sizes[-1] == EXPECTED["text_max"]
    assert embedded[len(embedded) // 2] == EXPECTED["embed_median"]
    assert embedded[-1] == EXPECTED["embed_max"]


def test_overlap_coverage(book):
    """The chunks without an overlap should be exactly the first of each section
    (59 sections + 4 chapters = 63), leaving 246 of 328."""
    _, _, chunks = book
    assert sum(1 for c in chunks if c.overlap) == EXPECTED["with_overlap"]


def test_char_span_is_byte_exact(book):
    """Invariant #1, over every chunk rather than a sample.

    Note the source is read as bytes: slicing a decoded str would give character
    offsets and this test would fail for the wrong reason.
    """
    src, _, chunks = book
    for c in chunks:
        assert src[c.char_from : c.char_to].strip().decode() == c.text, c.index


def test_no_chunk_mixes_kinds(book):
    """Invariant #3, checked structurally: every chunk's span, re-classified
    paragraph by paragraph, must agree with the chunk's kind."""
    _, paras, chunks = book
    rules = ChunkRules()
    by_idx = {p.idx: p for p in paras}
    for c in chunks:
        for i in range(c.para_from, c.para_to + 1):
            p = by_idx.get(i)
            if p is None or heading_level(p.text, rules) > 0:
                continue
            assert classify_kind(p.text, rules) == c.kind, (c.index, i)


# --- the classifier, on the exact paragraphs that exposed the bug -------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Review questions: the dot after the number is present.
        ("1. Defina qué es un metarrelato o metanarrativa", KIND_QUESTIONS),
        ("1. ¿Cuáles son tal vez los motivos presentes en la cultura", KIND_QUESTIONS),
        ("26. Señale algunos de los peligros de acoger sin reservas", KIND_QUESTIONS),
        # No leading number at all — caught by ¿-density.
        ("Freud\n27. ¿Hasta dónde pueden acogerse y a partir de qué", KIND_QUESTIONS),
        # Footnotes: a bare number, no dot. These nine were the false positives.
        ("1\nRecordemos que la característica principal del pensamiento", KIND_FOOTNOTE),
        ("53 La palabra “nihilismo” proviene de la raíz latina “nihil”", KIND_FOOTNOTE),
        ("37 De hecho, conceptos como “moderno” y “contemporáneo”", KIND_FOOTNOTE),
        # Body prose with a rhetorical question stays prose: the ¿-density is an
        # order of magnitude below the threshold.
        (
            "La ciencia ha entrado también a formar parte de todo el abanico. "
            + "¿Es esto razonable? " + ("Relleno de prosa corriente. " * 40),
            KIND_BODY,
        ),
    ],
)
def test_classify_kind_cases(text, expected):
    assert classify_kind(text, ChunkRules()) == expected


def test_max_embed_must_exceed_hard_cap():
    """Invariant #2 enforced at construction, not left to a comment."""
    with pytest.raises(ValueError, match="invariant #2"):
        ChunkRules(hard_cap_chars=2000, max_embed_chars=2000)
