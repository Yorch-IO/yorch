"""A chunk's page, joined through the paragraph index.

`ChunkNode.page` was declared, written by `_MERGE_CHUNKS`, returned by
`queries.py` and **set by nothing** — `activities/ingest.py` read
`row.get("page")` off a row `chunk_row` never put a page in, which is
`document_version.page_count` in a second place. `_locator`'s `p. {page}`
branch has been unreachable for as long as it has existed.

What is asserted here is the join and its absences, because a wrong page is
worse than no page and neither raises anything.
"""

from __future__ import annotations

from types import SimpleNamespace

from brainworker.indexing import chunk_row, pages_by_paragraph


def _chunk(index: int = 0, para_from: int = 0, para_to: int = 0):
    return SimpleNamespace(
        index=index, kind="cuerpo", chapter="", section="", text="t",
        context="", overlap="", char_from=0, char_to=1, cell_ref="",
        para_from=para_from, para_to=para_to,
        embed_text=lambda: "t",
    )


def test_a_chunk_takes_the_page_of_the_paragraph_it_starts_on():
    pages = pages_by_paragraph([
        {"para": 0, "page": 7, "page_to": 7},
        {"para": 1, "page": 8, "page_to": 8},
        {"para": 2, "page": 8, "page_to": 8},
    ])
    assert chunk_row(_chunk(para_from=0, para_to=1), pages=pages)["page"] == 7
    assert chunk_row(_chunk(para_from=1, para_to=2), pages=pages)["page"] == 8


def test_a_document_with_no_positions_carries_no_page_at_all():
    """Absent, never 0. `_locator` renders `p. 0` for a falsy-but-present
    value, and a citation naming a page the reader cannot find teaches them not
    to trust the locator — which is worth more than the page."""
    assert "page" not in chunk_row(_chunk(), pages=None)
    assert "page" not in chunk_row(_chunk(), pages={})


def test_a_paragraph_the_sidecar_never_placed_leaves_its_chunk_unplaced():
    """A sparse table is the honest shape: one paragraph having no position is
    the same answer as the document having none."""
    pages = pages_by_paragraph([{"para": 0, "page": 3, "page_to": 3}])
    assert chunk_row(_chunk(para_from=0), pages=pages)["page"] == 3
    assert "page" not in chunk_row(_chunk(para_from=1), pages=pages)


def test_a_page_of_zero_is_read_as_no_page():
    """`_Row.page` defaults to 0 for a row nothing placed, and a sidecar built
    from one would claim page zero. Refused at the reader, so a single
    unplaced row cannot put `p. 0` on a citation."""
    assert pages_by_paragraph([{"para": 0, "page": 0, "page_to": 0}]) == {}


def test_the_row_still_carries_everything_it_did_before():
    """`StoredChunk.from_row` is the reader this writer must agree with, and a
    field lost here is a field lost from every consumer of `chunks.jsonl` —
    including the EPUB writer and `rebuild`."""
    row = chunk_row(_chunk(para_from=4, para_to=5), pages={4: 11})
    for key in ("index", "kind", "chapter", "section", "text", "context",
                "overlap", "embed_text", "char_from", "char_to", "cell_ref",
                "para_from", "para_to"):
        assert key in row, key
    assert row["page"] == 11
    # A timed source still gets its clock, and still gets no page: a transcript
    # has no pages and `_locator` returns before the page branch for one.
    timed = chunk_row(_chunk(), start_s=1.0, end_s=2.0)
    assert timed["start_s"] == 1.0 and "page" not in timed


# --- what the reader sees, and what re-projecting an old version costs -------


def _version(**kw):
    from brainworker.graph.projection import VersionNode

    return VersionNode(
        library="lib_a", source_key="libros/x.pdf", content_sha256="s",
        title="Un libro", tenant_id="tnt_000000000000000000000001", **kw
    )


def _node(**kw):
    from brainworker.graph.projection import ChunkNode

    base = dict(ordinal=0, kind="cuerpo", text="t", char_start=0, char_end=10)
    return ChunkNode(**{**base, **kw})


def test_a_placed_chunk_puts_its_page_in_the_locator():
    """`_locator`'s `p. {page}` branch has existed since it was written and
    nothing could reach it, because no row schema carried a page."""
    from brainworker.graph.projection import _locator

    assert _locator(_version(), _node(page=214)) == "Un libro · p. 214 · [0:10]"


def test_re_projecting_a_version_indexed_before_pages_changes_no_citation_id():
    """The consequence worth getting right before touching the corpus.

    `citation_id` is `digest(chunk_id, locator)`, so a locator that gained a
    part would re-mint every `cit_` in the graph. It does not: a run written
    before the `positions` sidecar existed has no `page` on any row,
    `ChunkNode.page` stays `None`, and the locator is byte-identical to the one
    that minted the id. Only a version that is actually *re-indexed* — which
    re-extracts and therefore writes positions — gets pages and new ids, and
    `project_structure` already prunes the citations it did not produce.
    """
    from brainworker.graph.schema import citation_id
    from brainworker.graph.projection import _locator

    version = _version()
    before = _locator(version, _node(page=None))
    assert before == "Un libro · [0:10]"
    assert citation_id("chk_x", before) == citation_id("chk_x", _locator(version, _node()))

    # And the same chunk once its run has been re-indexed with positions.
    after = _locator(version, _node(page=7))
    assert citation_id("chk_x", after) != citation_id("chk_x", before)


def test_a_page_never_renders_as_zero():
    """`_locator` tests `is not None`, so a 0 would print `p. 0`. Nothing can
    put one there — `pages_by_paragraph` drops it at the reader — and this is
    the assertion that keeps the two ends agreeing about why."""
    from brainworker.graph.projection import _locator

    assert "p. 0" not in _locator(_version(), _node(page=pages_by_paragraph(
        [{"para": 0, "page": 0}]
    ).get(0)))
