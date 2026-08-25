"""Projection against a real Memgraph.

The properties under test are the ones a mock cannot check: that `MERGE` really
converges, that a partially written version really can be re-run, and that the
shipped templates really return rows against the shape the projection writes.
"""

from __future__ import annotations

import pytest

from brainworker.graph import Graph
from brainworker.graph.projection import (
    SemanticEdge,
    VersionNode,
    activate,
    project_claims,
    project_concepts,
    project_semantic_edges,
    project_structure,
    read_concept_descriptions,
    set_concept_descriptions,
)
from brainworker.graph.schema import chunk_id, concept_id


def test_structure_lands_with_the_counts_it_reports(graph: Graph, version: VersionNode):
    counts = project_structure(graph, version)
    assert counts == {"sections": 3, "chunks": 3, "citations": 3}

    rows = graph.query("document_outline", {"version_id": version.version})
    assert [r["title"] for r in rows] == [
        "Libro I",
        "Del conocimiento de Dios",
        "Libro II",
    ]


def test_projecting_twice_converges_instead_of_duplicating(
    graph: Graph, version: VersionNode
):
    """Temporal retries activities. A projection that duplicated on retry would
    put two copies of every chunk into the candidate set for one question."""
    project_structure(graph, version)
    first = graph.write(
        "MATCH (v:DocumentVersion {id: $id})-[:HAS_CHUNK]->(c:Chunk) "
        "RETURN count(c) AS n",
        {"id": version.version},
    )[0]["n"]

    project_structure(graph, version)
    second = graph.write(
        "MATCH (v:DocumentVersion {id: $id})-[:HAS_CHUNK]->(c:Chunk) "
        "RETURN count(c) AS n",
        {"id": version.version},
    )[0]["n"]

    assert first == second == 3


def test_two_paths_with_identical_bytes_share_one_version(
    graph: Graph, version: VersionNode
):
    """The fix for the inherited `doc_id_for()` defect, end to end.

    A duplicate file must arrive as a second HAS_VERSION edge, not as a second
    set of chunks competing in ranking.
    """
    project_structure(graph, version)
    duplicate = VersionNode(
        library=version.library,
        source_key=version.source_key.replace("libros/", "copias/"),
        content_sha256=version.content_sha256,
        title=version.title,
        sections=version.sections,
        chunks=version.chunks,
    )
    assert duplicate.version == version.version
    assert duplicate.document != version.document

    project_structure(graph, duplicate)
    try:
        documents = graph.write(
            "MATCH (d:Document)-[:HAS_VERSION]->(v:DocumentVersion {id: $id}) "
            "RETURN count(d) AS n",
            {"id": version.version},
        )[0]["n"]
        chunks = graph.write(
            "MATCH (v:DocumentVersion {id: $id})-[:HAS_CHUNK]->(c:Chunk) "
            "RETURN count(c) AS n",
            {"id": version.version},
        )[0]["n"]
        assert documents == 2, "both paths should point at one version"
        assert chunks == 3, "the duplicate must not re-chunk"
    finally:
        graph.write(
            "MATCH (d:Document {id: $id}) DETACH DELETE d",
            {"id": duplicate.document},
        )


def test_sections_nest_and_chunks_attach_to_their_section(
    graph: Graph, version: VersionNode
):
    project_structure(graph, version)
    nested = graph.write(
        "MATCH (p:Section)-[:CONTAINS]->(c:Section) WHERE p.version_id = $id "
        "RETURN p.title AS parent, c.title AS child",
        {"id": version.version},
    )
    assert [(r["parent"], r["child"]) for r in nested] == [
        ("Libro I", "Del conocimiento de Dios")
    ]

    section = graph.write(
        "MATCH (s:Section {version_id: $id, title: 'Del conocimiento de Dios'}) "
        "RETURN s.id AS id",
        {"id": version.version},
    )[0]["id"]
    chunks = graph.query("section_chunks", {"section_id": section})
    assert [r["kind"] for r in chunks] == ["cuerpo", "preguntas"]


def test_reading_order_is_linked_in_both_directions(graph: Graph, version: VersionNode):
    project_structure(graph, version)
    middle = chunk_id(version.version, 1)
    row = graph.query("chunk_neighbours", {"chunk_id": middle})[0]
    assert row["before_id"] == chunk_id(version.version, 0)
    assert row["after_id"] == chunk_id(version.version, 2)


def test_every_chunk_gets_a_citation_carrying_its_page(
    graph: Graph, version: VersionNode
):
    """No answer may be returned without a citation, so this cannot be sparse."""
    project_structure(graph, version)
    ids = [chunk_id(version.version, i) for i in range(3)]
    rows = graph.query("citations_for_chunks", {"chunk_ids": ids})
    assert len(rows) == 3
    by_chunk = {r["chunk_id"]: r for r in rows}
    assert by_chunk[ids[0]]["page"] == 17
    assert "Del conocimiento de Dios" in by_chunk[ids[0]]["locator"]
    assert "[0:40]" in by_chunk[ids[0]]["locator"]


def test_activation_is_the_last_step_and_leaves_the_old_version_readable(
    graph: Graph, version: VersionNode
):
    project_structure(graph, version)
    before = graph.write(
        "MATCH (v:DocumentVersion {id: $id}) RETURN v.active AS active",
        {"id": version.version},
    )[0]["active"]
    assert before is False, "a version must not be answerable before it completes"

    activate(graph, version)
    after = graph.write(
        "MATCH (v:DocumentVersion {id: $id}) RETURN v.active AS active",
        {"id": version.version},
    )[0]["active"]
    assert after is True


def test_semantic_edges_carry_their_provenance(graph: Graph, version: VersionNode):
    project_structure(graph, version)
    project_concepts(graph, [{"name": "Conocimiento de Dios", "type": "doctrina"}])
    kid = concept_id("Conocimiento de Dios")

    project_semantic_edges(
        graph,
        [
            SemanticEdge(
                type="MENTIONS",
                source_id=chunk_id(version.version, 0),
                target_id=kid,
                confidence=0.91,
                extractor_model="gemini-2.5-flash",
                source_chunk_id=chunk_id(version.version, 0),
            )
        ],
    )

    rows = graph.query("concepts_in_version", {"version_id": version.version})
    assert [r["name"] for r in rows] == ["Conocimiento de Dios"]

    props = graph.write(
        "MATCH (:Chunk {id: $c})-[m:MENTIONS]->(:Concept {id: $k}) "
        "RETURN m.confidence AS confidence, m.extractor_model AS model, "
        "m.created_at AS created_at, m.source_chunk_id AS source_chunk_id",
        {"c": chunk_id(version.version, 0), "k": kid},
    )[0]
    assert props["confidence"] == pytest.approx(0.91)
    assert props["model"] == "gemini-2.5-flash"
    assert props["created_at"] and props["source_chunk_id"]


def test_a_low_confidence_edge_is_stored_but_cannot_support_an_answer(
    graph: Graph, version: VersionNode
):
    project_structure(graph, version)
    project_concepts(graph, [{"name": "Especulación dudosa"}])
    project_semantic_edges(
        graph,
        [
            SemanticEdge(
                type="MENTIONS",
                source_id=chunk_id(version.version, 0),
                target_id=concept_id("Especulación dudosa"),
                confidence=0.2,
                extractor_model="gemini-2.5-flash",
                source_chunk_id=chunk_id(version.version, 0),
            )
        ],
    )

    default_floor = graph.query("concepts_in_version", {"version_id": version.version})
    assert "Especulación dudosa" not in [r["name"] for r in default_floor]

    lowered = graph.query(
        "concepts_in_version",
        {"version_id": version.version, "confidence_floor": 0.1},
    )
    assert "Especulación dudosa" in [r["name"] for r in lowered]


def test_a_relationship_type_outside_the_allowed_set_never_reaches_cypher(
    graph: Graph, version: VersionNode
):
    """The one place a value is interpolated into a query string."""
    with pytest.raises(ValueError, match="not a semantic edge type"):
        project_semantic_edges(
            graph,
            [
                SemanticEdge(
                    type="MENTIONS]->() DETACH DELETE n //",
                    source_id=chunk_id(version.version, 0),
                    target_id=concept_id("x"),
                    confidence=1.0,
                    extractor_model="m",
                    source_chunk_id=chunk_id(version.version, 0),
                )
            ],
        )


def test_claims_stay_attached_to_the_chunk_that_produced_them(
    graph: Graph, version: VersionNode
):
    project_structure(graph, version)
    source = chunk_id(version.version, 0)
    project_claims(
        graph,
        [{"text": "La sabiduría comienza en el conocimiento de Dios.",
          "confidence": 0.8, "source_chunk_id": source}],
    )
    rows = graph.write(
        "MATCH (cl:Claim)-[:DERIVED_FROM]->(c:Chunk {id: $c}) "
        "RETURN cl.text AS text, cl.confidence AS confidence",
        {"c": source},
    )
    assert len(rows) == 1 and rows[0]["confidence"] == pytest.approx(0.8)


def test_a_claim_without_a_quote_projects_exactly_as_it_used_to(
    graph: Graph, version: VersionNode
):
    """The case a rebuild replays. `semantics.json` files written before quotes
    existed must keep projecting, or the free path back from a dropped graph
    stops working for everything already indexed."""
    project_structure(graph, version)
    source = chunk_id(version.version, 0)
    project_claims(
        graph,
        [{"text": "Un claim de un artifact anterior.", "confidence": 0.7,
          "source_chunk_id": source}],
    )
    rows = graph.write(
        "MATCH (cl:Claim)-[:DERIVED_FROM]->(c:Chunk {id: $c}) "
        "RETURN cl.text AS text, cl.quote AS quote",
        {"c": source},
    )
    assert len(rows) == 1 and rows[0]["quote"] is None


def test_a_verified_quote_is_never_lost_by_a_later_replay(
    graph: Graph, version: VersionNode
):
    """A claim's id is keyed on its chunk and its text, so a quote verified
    against that chunk stays valid for that id. Replaying an older artifact over
    it must not null the span — the write is monotonic on purpose."""
    project_structure(graph, version)
    source = chunk_id(version.version, 0)
    claim = {"text": "La sabiduría comienza en Dios.", "confidence": 0.8,
             "source_chunk_id": source}

    project_claims(graph, [{**claim, "quote": "suma de nuestra sabiduría",
                            "quote_char_start": 9, "quote_char_end": 34}])
    project_claims(graph, [claim])  # the old artifact, replayed afterwards

    rows = graph.write(
        "MATCH (cl:Claim)-[:DERIVED_FROM]->(c:Chunk {id: $c}) "
        "RETURN cl.quote AS quote, cl.quote_char_start AS start",
        {"c": source},
    )
    assert len(rows) == 1, "one claim, not two: the id converges"
    assert rows[0]["quote"] == "suma de nuestra sabiduría"
    assert rows[0]["start"] == 9


def test_a_concepts_descriptions_accumulate_without_doubling_on_a_retry(graph: Graph):
    """`SET list = list + new` is the one write in the projection module a
    Temporal retry would double. A concept is shared across documents, so the
    list has to grow — and re-running the same activity must not grow it."""
    import secrets

    name = f"Concepto {secrets.token_hex(6)}"
    cid = concept_id(name)
    try:
        project_concepts(graph, [{"name": name, "descriptions": ["Del primer chunk."]}])
        project_concepts(graph, [{"name": name, "descriptions": ["Del primer chunk."]}])
        [row] = read_concept_descriptions(graph, [cid])
        assert row["raw"] == ["Del primer chunk."], "a retry added it twice"

        # A second document's reading is a genuine addition.
        project_concepts(graph, [{"name": name, "descriptions": ["De otro libro."]}])
        [row] = read_concept_descriptions(graph, [cid])
        assert row["raw"] == ["Del primer chunk.", "De otro libro."]

        # Condensing writes the readable description and leaves the raw list, so
        # it can be re-condensed later without re-extracting anything.
        set_concept_descriptions(graph, [{"id": cid, "description": "Una frase."}])
        [row] = read_concept_descriptions(graph, [cid])
        assert row["description"] == "Una frase."
        assert len(row["raw"]) == 2
    finally:
        graph.write("MATCH (k:Concept {id: $id}) DETACH DELETE k", {"id": cid})


def test_a_concept_from_an_older_artifact_still_projects(graph: Graph):
    """The replay path: `semantics.json` written before descriptions existed."""
    import secrets

    name = f"Concepto {secrets.token_hex(6)}"
    cid = concept_id(name)
    try:
        project_concepts(graph, [{"name": name, "type": "doctrina"}])
        [row] = read_concept_descriptions(graph, [cid])
        assert row["raw"] == [] and row["description"] is None
    finally:
        graph.write("MATCH (k:Concept {id: $id}) DETACH DELETE k", {"id": cid})


def test_claims_for_chunks_returns_what_the_answer_prompt_needs(
    graph: Graph, version: VersionNode
):
    """The read path behind `retrieve._attach_claims`. Validation proves a
    template is safe, not that it returns rows against the shape the projection
    actually writes — and this one is what puts a claim in front of the answering
    model."""
    project_structure(graph, version)
    source = chunk_id(version.version, 0)
    project_claims(graph, [{
        "text": "La sabiduría comienza en el conocimiento de Dios.",
        "confidence": 0.9, "source_chunk_id": source,
        "quote": "suma de nuestra sabiduría", "quote_char_start": 9,
        "quote_char_end": 34, "status": "afirma",
    }])

    rows = graph.query(
        "claims_for_chunks",
        {"chunk_ids": [source], "confidence_floor": 0.6, "limit": 10},
    )
    [row] = [r for r in rows if r["chunk_id"] == source]
    assert row["quote"] == "suma de nuestra sabiduría"
    assert row["status"] == "afirma"

    # Below the floor it must not reach an answer at all.
    assert graph.query(
        "claims_for_chunks",
        {"chunk_ids": [source], "confidence_floor": 0.95, "limit": 10},
    ) == []


def test_a_claim_projected_before_the_state_existed_reads_as_unknown(
    graph: Graph, version: VersionNode
):
    """`sin_estado` is the absence of the field, not a fourth judgement — and it
    is coalesced in the template so no consumer has to invent a default."""
    project_structure(graph, version)
    source = chunk_id(version.version, 1)
    project_claims(graph, [
        {"text": "Un claim viejo.", "confidence": 0.8, "source_chunk_id": source},
    ])
    rows = graph.query(
        "claims_for_chunks",
        {"chunk_ids": [source], "confidence_floor": 0.6, "limit": 10},
    )
    assert [r["status"] for r in rows] == ["sin_estado"]


def test_a_claim_whose_chunk_is_gone_is_not_offered_as_evidence(
    graph: Graph, version: VersionNode
):
    """A claim reachable only through `ABOUT` looks exactly like a checkable one
    in the UI, and its `source_chunk_id` resolves to nothing.

    Found by counting: 214 such claims across 155 concepts on 2026-08-21, left by
    a removal path that predates the one now in `projection.py`. The fix is
    structural rather than a cleanup — the read requires the chunk, so debris
    cannot surface however it got there.
    """
    import secrets

    project_structure(graph, version)
    source = chunk_id(version.version, 0)
    name = f"Concepto {secrets.token_hex(6)}"
    kid = concept_id(name)
    edge = dict(confidence=0.9, extractor_model="m", source_chunk_id=source)

    project_concepts(graph, [{"name": name}])
    project_claims(graph, [
        {"text": "Vive en un chunk que existe.", "confidence": 0.9,
         "source_chunk_id": source},
        {"text": "Vive en un chunk que no existe.", "confidence": 0.9,
         "source_chunk_id": chunk_id("ver_" + "f" * 24, 0)},
    ])
    try:
        # Both claims get an ABOUT edge; only one of them has a chunk. The
        # orphan's `DERIVED_FROM` never lands, because `_MERGE_CLAIMS` matches
        # the chunk and finds none.
        from brainworker.graph.projection import claim_id as _claim_id

        project_semantic_edges(graph, [
            SemanticEdge(type="ABOUT", target_id=kid, **edge,
                         source_id=_claim_id(source, "Vive en un chunk que existe.")),
            SemanticEdge(type="ABOUT", target_id=kid, **edge,
                         source_id=_claim_id(chunk_id("ver_" + "f" * 24, 0),
                                             "Vive en un chunk que no existe.")),
        ])

        rows = graph.query(
            "claims_about_concept",
            {"concept_id": kid, "confidence_floor": 0.6, "limit": 10},
        )
        assert [r["text"] for r in rows] == ["Vive en un chunk que existe."]
    finally:
        graph.write(
            "MATCH (cl:Claim) WHERE cl.source_chunk_id IN $ids DETACH DELETE cl",
            {"ids": [source, chunk_id("ver_" + "f" * 24, 0)]},
        )
        graph.write("MATCH (k:Concept {id: $id}) DETACH DELETE k", {"id": kid})


def test_a_claim_relates_two_concepts_and_the_traversal_finds_it_either_way(
    graph: Graph, version: VersionNode
):
    """The graph's only concept-to-concept path.

    Every other route between two concepts runs through a chunk that mentioned
    both, which is co-occurrence rather than anything the document said. This one
    goes through a claim, so the relation arrives with a quote to check it — and
    it is found whichever concept the caller names first, because which one the
    extractor made the subject is an artefact of the sentence.
    """
    import secrets

    from brainworker.graph.projection import claim_id as _claim_id

    project_structure(graph, version)
    source = chunk_id(version.version, 0)
    tag = secrets.token_hex(6)
    fe, obras = f"Fe {tag}", f"Obras {tag}"
    text = "La fe sin obras está muerta."
    edge = dict(confidence=0.9, extractor_model="m", source_chunk_id=source)

    project_concepts(graph, [{"name": fe}, {"name": obras}])
    project_claims(graph, [{"text": text, "confidence": 0.9,
                            "source_chunk_id": source, "quote": "sabiduría",
                            "status": "afirma"}])
    try:
        project_semantic_edges(graph, [
            SemanticEdge(type="ABOUT", source_id=_claim_id(source, text),
                         target_id=concept_id(fe), **edge),
            SemanticEdge(type="INVOLVES", source_id=_claim_id(source, text),
                         target_id=concept_id(obras), **edge),
        ])

        for first, second in ((fe, obras), (obras, fe)):
            rows = graph.query(
                "claims_between_concepts",
                {"concept_id": concept_id(first), "other_id": concept_id(second),
                 "confidence_floor": 0.6, "limit": 10},
            )
            assert [r["text"] for r in rows] == [text], f"{first} → {second}"
            assert rows[0]["quote"] == "sabiduría"
            assert rows[0]["status"] == "afirma"

        # A concept pair the corpus never related returns nothing rather than
        # everything: co-occurrence is not a relation.
        assert graph.query(
            "claims_between_concepts",
            {"concept_id": concept_id(fe), "other_id": concept_id(fe),
             "confidence_floor": 0.6, "limit": 10},
        ) == []
    finally:
        graph.write("MATCH (cl:Claim {id: $id}) DETACH DELETE cl",
                    {"id": _claim_id(source, text)})
        graph.write("MATCH (k:Concept) WHERE k.id IN $ids DETACH DELETE k",
                    {"ids": [concept_id(fe), concept_id(obras)]})
