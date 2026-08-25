"""The whole-library overview, against a real Memgraph.

Two properties this file exists to pin, both of which a mocked graph cannot see:

- **The library scope reaches the query.** `library_mentions` enters from
  `Document` and walks *to* `Concept`, which is the safe direction — but concepts
  merge by canonical name with no library in the id, so the constraint has to be
  in the Cypher and not merely in the handler. `test_library_scope.py` records
  what it cost to learn that.
- **Repeated mentions collapse into one weighted edge.** A book mentioning a
  concept in nine chunks is one edge of weight nine, and the aggregation happens
  in the database because the alternative is shipping every chunk to fold it
  in Python.

The concept names are randomised for the reason `test_library_scope.py` writes
down: `concept_id` hashes the canonical name and nothing else, so a fixed test
name merges into whatever this machine's real library already holds.
"""

from __future__ import annotations

import secrets

import pytest

from brainworker.graph import projection as proj
from brainworker.graph.projection import ChunkNode, SectionNode, SemanticEdge, VersionNode
from brainworker.graph.schema import chunk_id as make_chunk_id
from brainworker.graph.schema import concept_id as make_concept_id

MODEL = "gemini-3.6-flash"
MINE = "lib_overview_mine"
THEIRS = "lib_overview_theirs"


def _version(library: str, title: str, chunks: int = 3, source_key: str | None = None) -> VersionNode:
    return VersionNode(
        library=library,
        source_key=source_key or f"libros/{secrets.token_hex(6)}.pdf",
        content_sha256=secrets.token_hex(32),
        title=title,
        fmt="pdf",
        sections=(SectionNode(path=(1,), title="Único", level=1),),
        chunks=tuple(
            ChunkNode(i, "cuerpo", f"{title}, fragmento {i}.", i * 40, i * 40 + 30,
                      section_path=(1,))
            for i in range(chunks)
        ),
    )


def _mentions(version: VersionNode, concept: str, ordinals, confidence=0.95):
    return [
        SemanticEdge(
            type="MENTIONS",
            source_id=make_chunk_id(version.version, i),
            target_id=concept,
            confidence=confidence,
            extractor_model=MODEL,
            source_chunk_id=make_chunk_id(version.version, i),
        )
        for i in ordinals
    ]


@pytest.fixture
def library(graph):
    """Two books in one library and one book in another, sharing a concept.

    `mine_a` mentions `shared` from three chunks, `mine_b` from one, and the
    outsider from one — so the aggregation, the degree and the isolation are all
    observable in a single fixture.
    """
    from tests.graph.conftest import _purge

    shared = f"Concepto compartido {secrets.token_hex(6)}"
    lonely = f"Concepto solitario {secrets.token_hex(6)}"
    weak = f"Concepto dudoso {secrets.token_hex(6)}"

    mine_a = _version(MINE, "Aaa primero")
    mine_b = _version(MINE, "Bbb segundo")
    theirs = _version(THEIRS, "Ajeno")
    for v in (mine_a, mine_b, theirs):
        proj.project_structure(graph, v)
        proj.activate(graph, v)

    proj.project_concepts(
        graph,
        [{"name": shared, "type": "Doctrina"},
         {"name": lonely, "type": "Doctrina"},
         {"name": weak, "type": "Doctrina"}],
    )
    ids = {n: make_concept_id(n) for n in (shared, lonely, weak)}
    proj.project_semantic_edges(
        graph,
        _mentions(mine_a, ids[shared], [0, 1, 2])
        + _mentions(mine_b, ids[shared], [0])
        + _mentions(theirs, ids[shared], [0])
        + _mentions(mine_a, ids[lonely], [0])
        + _mentions(mine_a, ids[weak], [1], confidence=0.30),
    )
    try:
        yield {
            "mine_a": mine_a,
            "mine_b": mine_b,
            "theirs": theirs,
            "shared": ids[shared],
            "lonely": ids[lonely],
            "weak": ids[weak],
        }
    finally:
        for v in (mine_a, mine_b, theirs):
            _purge(graph, v)


def _rows(graph, template, **args):
    # `min_documents=1` unless a test is about the filter: the fixture's books
    # deliberately hold concepts only one of them mentions, and the shipping
    # default would hide exactly those.
    if template == "library_mentions":
        args.setdefault("min_documents", 1)
    return [row.data for row in graph.query(template, args)]


def test_the_documents_of_a_library_are_its_own(graph, library):
    rows = _rows(graph, "library_documents", library_id=MINE)
    versions = {r["version_id"] for r in rows}
    assert versions == {library["mine_a"].version, library["mine_b"].version}
    assert library["theirs"].version not in versions


def test_repeated_mentions_are_one_weighted_edge(graph, library):
    """Three chunks of one book naming one concept is an edge of weight three."""
    rows = _rows(graph, "library_mentions", library_id=MINE)
    edge = next(
        r for r in rows
        if r["version_id"] == library["mine_a"].version
        and r["concept_id"] == library["shared"]
    )
    assert edge["mentions"] == 3
    assert edge["confidence"] == pytest.approx(0.95)


def test_a_concept_two_books_share_arrives_once_per_book(graph, library):
    """The precondition for everything else: the concept really is shared, so a
    result that shows one book is isolation and not an empty graph."""
    shared = library["shared"]
    rows = _rows(graph, "library_mentions", library_id=MINE)
    for_shared = [r for r in rows if r["concept_id"] == shared]
    assert {r["version_id"] for r in for_shared} == {
        library["mine_a"].version,
        library["mine_b"].version,
    }


def test_the_other_librarys_mention_of_the_same_concept_is_not_returned(graph, library):
    """The leak this scoping exists to stop, entered from the other end."""
    rows = _rows(graph, "library_mentions", library_id=MINE)
    assert library["theirs"].version not in {r["version_id"] for r in rows}

    theirs = _rows(graph, "library_mentions", library_id=THEIRS)
    assert {r["version_id"] for r in theirs} == {library["theirs"].version}


def test_the_floor_filters_rather_than_ranks(graph, library):
    """A weak edge is absent, not merely last. The badge on the screen promises
    a threshold, and a threshold that only sorts is a false promise."""
    weak = library["weak"]
    low = _rows(graph, "library_mentions", library_id=MINE, confidence_floor=0.2)
    high = _rows(graph, "library_mentions", library_id=MINE, confidence_floor=0.9)
    assert weak in {r["concept_id"] for r in low}
    assert weak not in {r["concept_id"] for r in high}


def test_the_order_is_stable_across_calls(graph, library):
    """A canvas re-fetched at a new threshold must not reshuffle itself."""
    once = _rows(graph, "library_mentions", library_id=MINE)
    twice = _rows(graph, "library_mentions", library_id=MINE)
    assert [(r["version_id"], r["concept_id"]) for r in once] == [
        (r["version_id"], r["concept_id"]) for r in twice
    ]
    assert [r["mentions"] for r in once] == sorted(
        (r["mentions"] for r in once), reverse=True
    )


def test_an_empty_library_is_empty_and_not_an_error(graph, library):
    empty = f"lib_overview_{secrets.token_hex(6)}"
    assert _rows(graph, "library_documents", library_id=empty) == []
    assert _rows(graph, "library_mentions", library_id=empty) == []


def test_an_inactive_version_is_not_drawn_twice(graph, library):
    """One node per book. A re-indexed document holds two versions and the
    overview must show the shelf, not the history."""
    from tests.graph.conftest import _purge

    # Same library and same source_key, so `document_id` matches: one document,
    # a second set of bytes.
    second = _version(MINE, "Aaa primero", source_key=library["mine_a"].source_key)
    proj.project_structure(graph, second)
    proj.activate(graph, second)
    try:
        rows = _rows(graph, "library_documents", library_id=MINE)
        by_document: dict[str, int] = {}
        for r in rows:
            by_document[r["document_id"]] = by_document.get(r["document_id"], 0) + 1
        assert set(by_document.values()) == {1}
        assert second.version in {r["version_id"] for r in rows}
        assert library["mine_a"].version not in {r["version_id"] for r in rows}
    finally:
        _purge(graph, second)


def test_the_degree_filter_keeps_what_joins_two_books(graph, library):
    """Measured on the real corpus: 10,835 concepts fall to 1,719 at
    `min_documents = 2`, and every one that goes is a leaf only one book
    mentions. Confidence cannot do this — 0.6 to 0.9 removes 3%."""
    shared = _rows(graph, "library_mentions", library_id=MINE, min_documents=2)
    ids = {r["concept_id"] for r in shared}
    assert library["shared"] in ids
    assert library["lonely"] not in ids
    assert library["weak"] not in ids


def test_the_degree_is_the_count_of_books_not_of_rows(graph, library):
    """Every row for a concept carries the same degree, so a truncated response
    still reports the true one."""
    rows = _rows(graph, "library_mentions", library_id=MINE, min_documents=2)
    for row in (r for r in rows if r["concept_id"] == library["shared"]):
        assert row["documents"] == 2


def test_a_concept_only_one_book_mentions_is_still_reachable(graph, library):
    """It is dropped from the canvas, not from the graph. The book's own panel
    calls `concepts_in_version`, which has no degree filter at all."""
    rows = [
        row.data
        for row in graph.query(
            "concepts_in_version",
            {"version_id": library["mine_a"].version, "confidence_floor": 0.2},
        )
    ]
    assert library["lonely"] in {r["id"] for r in rows}
