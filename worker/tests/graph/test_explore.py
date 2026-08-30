"""What the Explore screen asks the graph for.

Six of the nine templates had no caller but the answering planner, which reaches
them only when it happens to pick one. These tests exercise them the way a person
browsing a document does — outline, into a section, into a chunk, out to its
concepts and its neighbours — against a real Memgraph, and pin the empty cases,
because a document with no concepts is the ordinary state of one nobody paid to
extract semantics from.
"""

from __future__ import annotations

import pytest

from brainworker.graph import projection as proj
from brainworker.graph.projection import ChunkNode, SectionNode, SemanticEdge, VersionNode
from brainworker.graph.queries import TemplateError
from brainworker.graph.schema import LEGACY_TENANT_ID, chunk_id as make_chunk_id
from brainworker.graph.schema import claim_id as make_claim_id
from brainworker.graph.schema import concept_id as make_concept_id
from brainworker.graph.schema import section_id as make_section_id

NAME = "Conocimiento de Dios"
CLAIM_TEXT = "El conocimiento de Dios y el de uno mismo van juntos."
MODEL = "gemini-3.6-flash"


@pytest.fixture
def projected(graph, version: VersionNode) -> VersionNode:
    proj.project_structure(graph, version)
    return version


# -- the deterministic half --------------------------------------------------


def test_the_outline_is_the_documents_own_headings(graph, projected):
    rows = graph.query("document_outline", {"version_id": projected.version})
    assert [r["title"] for r in rows] == [
        "Libro I", "Del conocimiento de Dios", "Libro II",
    ]
    # Ordered by ordinal path, so the nesting survives the round trip. Counting
    # ordinals globally instead of per parent once produced a `1.1` numbered
    # `2.1`, whose implied parent named a section that did not exist.
    #
    # The path comes back as a dotted **string**, not a list of integers, so the
    # Explore screen reads nesting depth from `path.split(".").length` rather
    # than from the value itself.
    assert [r["path"] for r in rows] == ["1", "1.1", "2"]
    assert [r["level"] for r in rows] == [1, 2, 1]


def test_a_section_hands_back_its_chunks_with_their_words(graph, projected):
    """The screen shows the chunk, not just its offsets. Returning ids alone
    would make a reading pane one extra round trip per row."""
    section = make_section_id(projected.version, (1, 1))
    rows = graph.query("section_chunks", {"section_id": section})
    assert [r["ordinal"] for r in rows] == [0, 1]
    assert rows[0]["text"].startswith("Casi toda la suma")
    assert rows[0]["kind"] == "cuerpo"
    assert (rows[0]["char_start"], rows[0]["char_end"]) == (0, 40)


def test_a_chunk_knows_what_comes_before_and_after_it(graph, projected):
    row = graph.query(
        "chunk_neighbours", {"chunk_id": make_chunk_id(projected.version, 1)}
    )[0]
    assert row["before_id"] == make_chunk_id(projected.version, 0)
    assert row["after_id"] == make_chunk_id(projected.version, 2)
    assert row["before_text"].startswith("Casi toda")
    assert row["after_text"] == "Cf. Gén. 2:15."
    assert row["text"] == "¿Qué es conocer a Dios?"
    assert row["kind"] == "preguntas"


def test_the_first_chunk_has_no_predecessor_and_says_so(graph, projected):
    """An edge case the reading pane meets on every document's first chunk."""
    row = graph.query(
        "chunk_neighbours", {"chunk_id": make_chunk_id(projected.version, 0)}
    )[0]
    assert row["before_id"] is None
    assert row["before_text"] is None
    assert row["after_id"] == make_chunk_id(projected.version, 1)


def test_every_chunk_carries_a_locator_someone_can_open(graph, projected):
    ids = [make_chunk_id(projected.version, i) for i in range(3)]
    rows = graph.query("citations_for_chunks", {"chunk_ids": ids, "limit": len(ids)})
    assert len(rows) == 3
    assert all(r["locator"] for r in rows)
    assert any("17" in r["locator"] for r in rows)


# -- the model-proposed half -------------------------------------------------


@pytest.fixture
def semantics(graph, projected) -> tuple[str, str]:
    """Concepts and claims, as `extract_semantics` would have written them."""
    source = make_chunk_id(projected.version, 0)
    concept = make_concept_id(NAME, LEGACY_TENANT_ID)
    claim = make_claim_id(source, CLAIM_TEXT)

    proj.project_concepts(graph, [{"name": NAME, "type": "Concepto teológico"}])
    proj.project_claims(
        graph, [{"text": CLAIM_TEXT, "confidence": 0.88, "source_chunk_id": source}]
    )
    proj.project_semantic_edges(
        graph,
        [
            SemanticEdge(
                type="MENTIONS", source_id=source, target_id=concept,
                confidence=0.93, extractor_model=MODEL, source_chunk_id=source,
            ),
            SemanticEdge(
                type="ABOUT", source_id=claim, target_id=concept,
                confidence=0.88, extractor_model=MODEL, source_chunk_id=source,
            ),
        ],
    )
    return concept, claim


def test_concepts_come_back_with_the_confidence_that_qualifies_them(
    graph, projected, semantics
):
    """A concept is a model's proposal. The screen has to label it as one, and
    the confidence is what it labels it with."""
    concept, _ = semantics
    rows = graph.query("concepts_in_version", {"version_id": projected.version})
    found = next(r for r in rows if r["id"] == concept)
    assert found["name"] == NAME
    assert found["type"] == "Concepto teológico"
    assert found["mentions"] == 1
    assert found["confidence"] == pytest.approx(0.93)


def test_the_confidence_floor_filters_rather_than_ranks(graph, projected, semantics):
    """A low-confidence relation must not answer a question until it clears the
    threshold, so the floor has to remove it, not sort it last."""
    concept, _ = semantics
    above = graph.query(
        "concepts_in_version", {"version_id": projected.version, "confidence_floor": 0.5}
    )
    below = graph.query(
        "concepts_in_version", {"version_id": projected.version, "confidence_floor": 0.99}
    )
    assert any(r["id"] == concept for r in above)
    assert not any(r["id"] == concept for r in below)


def test_a_claim_names_the_chunk_a_person_can_check_it_against(
    graph, projected, semantics
):
    """A relation nobody can verify is worse than no relation, because it still
    looks like evidence."""
    concept, claim = semantics
    rows = graph.query("claims_about_concept", {"concept_id": concept})
    found = next(r for r in rows if r["id"] == claim)
    assert found["text"] == CLAIM_TEXT
    assert found["source_chunk_id"] == make_chunk_id(projected.version, 0)
    assert found["confidence"] == pytest.approx(0.88)
    # And the chunk it names really is one of this version's, so the link the
    # screen offers actually resolves.
    assert graph.query("chunk_neighbours", {"chunk_id": found["source_chunk_id"]})


def test_related_documents_name_a_document_not_only_a_version(
    graph, projected, semantics
):
    """A version id is not something a person can navigate to. The screen needs
    the document that owns it, and that edge is already projected."""
    from tests.graph.conftest import _purge

    concept, _ = semantics
    other = VersionNode(
        library="lib_test",
        tenant_id=LEGACY_TENANT_ID,
        source_key="libros/otro-calvino.pdf",
        content_sha256="e" * 64,
        title="Otro tratado",
        fmt="pdf",
        sections=(SectionNode(path=(1,), title="Único", level=1),),
        chunks=(ChunkNode(0, "cuerpo", "También trata de Dios.", 0, 22,
                          section_path=(1,)),),
    )
    proj.project_structure(graph, other)
    try:
        proj.project_semantic_edges(
            graph,
            [
                SemanticEdge(
                    type="MENTIONS",
                    source_id=make_chunk_id(other.version, 0),
                    target_id=concept,
                    confidence=0.9,
                    extractor_model=MODEL,
                    source_chunk_id=make_chunk_id(other.version, 0),
                )
            ],
        )
        rows = graph.query("related_documents", {"version_id": projected.version})
        found = next(r for r in rows if r["id"] == other.version)
        assert found["title"] == "Otro tratado"
        assert found["document_id"] == other.document
        assert found["shared_concepts"] == 1
    finally:
        _purge(graph, other)


# -- the empty cases, which are the ordinary ones ----------------------------


def test_a_version_with_no_semantics_returns_nothing_rather_than_failing(
    graph, projected
):
    """Extracting semantics is a paid stage a user can decline, so "no concepts"
    is an ordinary state and not an error. The screen has to tell it apart from
    "this document has no ideas in it", which is why it must not be an
    exception."""
    assert graph.query("concepts_in_version", {"version_id": projected.version}) == []
    assert graph.query("related_documents", {"version_id": projected.version}) == []


def test_a_well_formed_id_that_names_nothing_returns_no_rows(graph):
    assert graph.query("document_outline", {"version_id": "ver_" + "0" * 24}) == []
    assert graph.query("section_chunks", {"section_id": "sec_" + "0" * 24}) == []
    assert graph.query("chunk_neighbours", {"chunk_id": "chk_" + "0" * 24}) == []


def test_a_malformed_id_is_refused_before_it_reaches_the_database(graph):
    """The shape check is not decoration. It is the same validator that stops a
    planner's output becoming query syntax, and it guards the Explore endpoints'
    path parameters too — which arrive from the webview."""
    with pytest.raises(TemplateError):
        graph.query(
            "document_outline", {"version_id": "'; MATCH (n) DETACH DELETE n //"}
        )
