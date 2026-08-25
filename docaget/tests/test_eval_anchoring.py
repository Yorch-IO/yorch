"""The eval set must survive re-chunking.

Tuning chunk parameters renumbers every chunk. An eval set keyed by
``chunk_index`` would therefore start scoring the wrong passages the moment the
loop changed the target size or the overlap — and the loop would happily optimise
against that noise, reporting improvements that are measurement artefacts.

Anchoring each question to the **byte** midpoint of its source passage makes the
eval invariant to chunking, which is what lets chunk tuning be trusted at all.

Measured on the real book rather than a synthetic fixture: a small uniform fixture
produces identical chunkings at different settings, which hides the very problem
these tests exist to demonstrate.
"""

from __future__ import annotations

import pathlib

import pytest

from docagent.chunk import ChunkRules, build_chunks, read_source
from docagent.profiles import EvalItem

import corpus

# Anchoring must survive a re-chunk of any real document, not just one.
# See tests/corpus.py.
BOOK = corpus.resolve()


@pytest.fixture(scope="module")
def source():
    if BOOK is None:
        pytest.skip("no corpus available")
    return read_source(str(BOOK))


def _chunk(source, **overrides):
    src, paras = source
    return build_chunks(src, paras, ChunkRules(**overrides))


def _deep(chunks):
    """A chunk well inside the document.

    Proportional rather than a fixed index: at a hardcoded ``[200]`` these tests
    only ran on a document with at least 201 chunks, which quietly tied them to
    one book. Three fifths of the way in is past any front matter and short of
    the tail, on a corpus of any size.
    """
    return chunks[len(chunks) * 3 // 5]


def test_rechunking_renumbers_chunks(source):
    """The premise. If this stopped being true the anchoring would be unnecessary."""
    a = _chunk(source)                                   # 1200 / overlap 150
    b = _chunk(source, target_chars=600, overlap_chars=0)
    assert len(a) != len(b), (len(a), len(b))

    offset = _deep(a).char_from + 10
    ia = next(c.index for c in a if c.char_from <= offset < c.char_to)
    ib = next(c.index for c in b if c.char_from <= offset < c.char_to)
    assert ia != ib, "the same byte lands in a differently-numbered chunk"


def test_a_byte_anchored_item_still_matches_after_rechunking(source):
    a = _chunk(source)
    origin = _deep(a)
    item = EvalItem(
        question="¿qué dice este pasaje?",
        chunk_index=origin.index,
        char_mid=(origin.char_from + origin.char_to) // 2,
    )

    b = _chunk(source, target_chars=600, overlap_chars=0)
    matching = [c for c in b if item.matches({"char_span": [c.char_from, c.char_to]})]
    assert len(matching) == 1, "exactly one chunk must contain the anchor"
    assert matching[0].index != origin.index, "and it must be a renumbered one"
    # The matched chunk really is part of the original passage.
    assert matching[0].char_from >= origin.char_from
    assert matching[0].char_to <= origin.char_to


def test_index_keying_would_have_scored_a_different_passage(source):
    """Shows the failure the anchoring prevents, rather than only asserting the fix
    works. Under index matching the eval would accept a chunk from elsewhere in the
    book entirely."""
    a = _chunk(source)
    b = _chunk(source, target_chars=600, overlap_chars=0)
    origin = _deep(a)

    impostor = next((c for c in b if c.index == origin.index), None)
    assert impostor is not None
    assert impostor.text != origin.text
    # Not merely different text — a different region of the file.
    assert impostor.char_from != origin.char_from


def test_anchoring_holds_across_the_whole_eval_set(source):
    """Every anchor taken from one chunking must resolve to exactly one chunk in
    another. A stratified eval set of 40 questions is worthless if even a few
    silently stop resolving."""
    a = _chunk(source)
    b = _chunk(source, target_chars=900, overlap_chars=300)
    # Spread ~40 anchors across the document, the size of a real eval set,
    # rather than a fixed stride that yields a usable sample on one book only.
    step = max(1, len(a) // 40)
    items = [
        EvalItem(question="q", chunk_index=c.index, char_mid=(c.char_from + c.char_to) // 2)
        for c in a[::step]
    ]
    assert len(items) > 20

    for item in items:
        hits = [c for c in b if item.matches({"char_span": [c.char_from, c.char_to]})]
        assert len(hits) == 1, (item.char_mid, len(hits))


def test_structured_sources_fall_back_to_cell_ref():
    """A spreadsheet row has no byte span, so its anchor is the cell range."""
    item = EvalItem(
        question="¿cuántas unidades se vendieron en marzo en el norte?",
        chunk_index=7,
        char_mid=-1,
        cell_ref="Ventas 2025!A41:D60",
    )
    assert item.matches({"cell_ref": "Ventas 2025!A41:D60", "char_span": [0, 0]})
    assert not item.matches({"cell_ref": "Ventas 2025!A61:D80", "char_span": [0, 0]})


def test_offset_outside_every_span_is_a_miss(source):
    a = _chunk(source)
    item = EvalItem(question="q", chunk_index=0, char_mid=10_000_000)
    assert not any(item.matches({"char_span": [c.char_from, c.char_to]}) for c in a)
