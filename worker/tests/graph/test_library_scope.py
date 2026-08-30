"""Concepts are shared between documents; evidence must not be.

`project_concepts` merges by canonical name on purpose, so two books discussing
"felicidad" reach the same `Concept` node. That is what makes the graph useful
and it is also what made an unscoped traversal from a concept reach every library
that has ever mentioned it.

The vector leg has always filtered on `library_id` (`retrieve.search`). The graph
leg did not, and the citation guard would not have caught it: that guard checks a
cited chunk was among the evidence *given*, not that the evidence was in scope.
So an answer could cite a document from a corpus the asker never named.

Nothing had a test. These do.

The concept name is randomised per run, and that is not incidental. A first draft
used the literal "Felicidad" and the precondition assertion failed with
``{'lib_real', 'lib_scope_mine', 'lib_scope_theirs'}``: the name already existed
in this machine's real library, projected by an earlier semantic-extraction run.
Concept identity is ``concept_id(canonical_name, LEGACY_TENANT_ID)`` with no library in it, so a
test name collides with production data — which is precisely the mechanism the
leak rode on, demonstrated by accident.
"""

from __future__ import annotations

import secrets

import pytest

from brainworker.graph import projection as proj
from brainworker.graph.projection import ChunkNode, SectionNode, SemanticEdge, VersionNode
from brainworker.graph.queries import TemplateError
from brainworker.graph.schema import LEGACY_TENANT_ID, chunk_id as make_chunk_id
from brainworker.graph.schema import concept_id as make_concept_id

MODEL = "gemini-3.6-flash"
MINE = "lib_scope_mine"
THEIRS = "lib_scope_theirs"


def _version(library: str, title: str) -> VersionNode:
    return VersionNode(
        library=library,
        tenant_id=LEGACY_TENANT_ID,
        source_key=f"libros/{secrets.token_hex(6)}.pdf",
        content_sha256=secrets.token_hex(32),
        title=title,
        fmt="pdf",
        sections=(SectionNode(path=(1,), title="Único", level=1),),
        chunks=(
            ChunkNode(0, "cuerpo", f"{title}: la felicidad procede de servir.", 0, 40,
                      section_path=(1,)),
        ),
    )


@pytest.fixture
def two_libraries(graph):
    """One concept, mentioned from two different libraries."""
    from tests.graph.conftest import _purge

    # Unique per run: `concept_id` hashes the canonical name and nothing else, so
    # a fixed name would merge into whatever the real library already holds.
    shared = f"Concepto de prueba {secrets.token_hex(6)}"
    mine = _version(MINE, "Mi documento")
    theirs = _version(THEIRS, "Documento ajeno")
    proj.project_structure(graph, mine)
    proj.project_structure(graph, theirs)
    proj.project_concepts(graph, [{"name": shared, "type": "Estado"}])
    concept = make_concept_id(shared, LEGACY_TENANT_ID)
    proj.project_semantic_edges(
        graph,
        [
            SemanticEdge(
                type="MENTIONS",
                source_id=make_chunk_id(v.version, 0),
                target_id=concept,
                confidence=0.95,
                extractor_model=MODEL,
                source_chunk_id=make_chunk_id(v.version, 0),
            )
            for v in (mine, theirs)
        ],
    )
    try:
        yield mine, theirs, concept
    finally:
        _purge(graph, mine)
        _purge(graph, theirs)


def test_the_concept_really_is_shared_across_both_libraries(graph, two_libraries):
    """The precondition. Without this the scope test would pass vacuously — and a
    test that cannot fail is worse than none, because it reads as coverage."""
    mine, theirs, concept = two_libraries
    rows = graph.write(
        """
        MATCH (d:Document)-[:HAS_VERSION]->(:DocumentVersion)-[:HAS_CHUNK]->(:Chunk)
              -[:MENTIONS]->(k:Concept {id: $concept})
        RETURN collect(DISTINCT d.library_id) AS libraries
        """,
        {"concept": concept},
    )
    assert set(rows[0]["libraries"]) == {MINE, THEIRS}


def test_a_concept_traversal_returns_only_the_asking_librarys_chunks(
    graph, two_libraries
):
    """The leak, closed. This is the reachable path: the planner supplies concept
    *names*, `concept_by_name` resolves them to library-agnostic ids, and this is
    what keeps the chunks they reach in scope."""
    mine, theirs, concept = two_libraries
    rows = graph.query(
        "chunks_for_concepts", {"concept_ids": [concept], "library_id": MINE}
    )
    ids = {r["id"] for r in rows}
    assert make_chunk_id(mine.version, 0) in ids, "my own chunk is still reachable"
    assert make_chunk_id(theirs.version, 0) not in ids, "the other library leaked"


def test_each_library_sees_only_its_own_side_of_the_shared_concept(
    graph, two_libraries
):
    mine, theirs, concept = two_libraries
    for library, expected in ((MINE, mine), (THEIRS, theirs)):
        rows = graph.query(
            "chunks_for_concepts", {"concept_ids": [concept], "library_id": library}
        )
        assert {r["id"] for r in rows} == {make_chunk_id(expected.version, 0)}


def test_the_library_cannot_be_omitted(graph, two_libraries):
    """Required, not defaulted. A default would mean a caller that forgot the
    scope silently got a different one rather than an error."""
    _, _, concept = two_libraries
    with pytest.raises(TemplateError, match="library_id"):
        graph.query("chunks_for_concepts", {"concept_ids": [concept]})


def test_hydration_refuses_a_chunk_from_another_library(graph, two_libraries):
    """The choke point, tested through the function every graph result passes.

    `chunks_for_concepts` already filters, so this is belt and braces — and it is
    the half that survives someone adding a template later and forgetting to
    scope it.
    """
    from brainworker.answering.retrieve import _hydrate

    mine, theirs, _ = two_libraries
    asked = [make_chunk_id(mine.version, 0), make_chunk_id(theirs.version, 0)]

    got = _hydrate(graph, asked, "graph", MINE, LEGACY_TENANT_ID)
    assert [e.chunk_id for e in got] == [make_chunk_id(mine.version, 0)]
    assert all(e.title == "Mi documento" for e in got)

    # And it is symmetric — nothing about `MINE` is privileged in the code.
    got = _hydrate(graph, asked, "graph", THEIRS, LEGACY_TENANT_ID)
    assert [e.chunk_id for e in got] == [make_chunk_id(theirs.version, 0)]


def test_hydration_of_a_chunk_with_no_owning_document_drops_it(graph):
    """A chunk that cannot be attributed to a library cannot be attributed to a
    source either, and an answer is not allowed to cite something whose origin it
    cannot name. This match is required where it used to be OPTIONAL."""
    from brainworker.answering.retrieve import _hydrate

    orphan = "chk_" + secrets.token_hex(12)
    graph.write("CREATE (c:Chunk {id: $id, text: 'huérfano', kind: 'cuerpo'})",
                {"id": orphan})
    try:
        assert _hydrate(graph, [orphan], "graph", MINE, LEGACY_TENANT_ID) == []
    finally:
        graph.write("MATCH (c:Chunk {id: $id}) DETACH DELETE c", {"id": orphan})
