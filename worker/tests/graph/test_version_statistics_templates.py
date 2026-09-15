"""The five statistics templates, against a real Memgraph.

They exist because `validate_template` reads labels and keywords, **not scope**.
A template whose `ORDER BY` names a pattern variable the `RETURN` has already
aggregated away loads perfectly and fails at *query* time with `Unbound
variable` — which is the failure `library_mentions` records, and the only thing
that catches it is a database.

The second property here is the one a mocked graph cannot have an opinion about:
`version_concepts_reached` must walk **all three** routes to a concept. A
concept named only as a claim's subject carries no `MENTIONS` edge from any
chunk, and counting the first route alone is how claim-only concepts were
orphaned once already.
"""

from __future__ import annotations

import secrets

import pytest

from brainworker.graph import Graph
from brainworker.graph.projection import (
    SemanticEdge,
    VersionNode,
    project_claims,
    project_concepts,
    project_semantic_edges,
    project_structure,
)
from brainworker.graph.schema import LEGACY_TENANT_ID, chunk_id

MODEL = "gemini-3.6-flash"


@pytest.fixture
def projected(graph: Graph, version: VersionNode) -> VersionNode:
    project_structure(graph, version)
    return version


def _rows(graph: Graph, template: str, version: VersionNode):
    return graph.query(template, {"version_id": version.version})


# --- the counts themselves ---------------------------------------------------


def test_the_counts_match_what_the_projection_reported(graph, projected):
    """`version_counts` is `projection._COUNT_VERSION_SUBGRAPH` with the tenant
    predicate it lacks — and it must keep agreeing with it, because removal
    reports what it deleted from that one."""
    row = _rows(graph, "version_counts", projected)[0]
    assert (row["chunks"], row["sections"], row["citations"]) == (3, 3, 3)
    assert row["claims"] == 0


def test_the_kind_histogram_is_the_chunks_own_vocabulary(graph, projected):
    """Spanish on the wire: these are stored in Qdrant payloads and used in
    filters, so renaming them breaks every existing collection."""
    rows = _rows(graph, "version_chunk_kinds", projected)
    assert {r["kind"]: r["chunks"] for r in rows} == {
        "cuerpo": 1,
        "preguntas": 1,
        "nota": 1,
    }


def test_the_section_levels_are_counted_per_level(graph, projected):
    rows = _rows(graph, "version_section_levels", projected)
    assert {r["level"]: r["sections"] for r in rows} == {1: 2, 2: 1}


def test_ordering_by_an_aggregated_alias_actually_runs(graph, projected):
    """The whole reason this file needs a database. `ORDER BY c.kind` after a
    `RETURN` that aggregates fails with `Unbound variable: c`, and the validator
    cannot see it — it reads labels and keywords, not scope."""
    for template in (
        "version_counts",
        "version_chunk_kinds",
        "version_section_levels",
        "version_claim_shape",
        "version_concepts_reached",
    ):
        _rows(graph, template, projected)  # must not raise


# --- claims ------------------------------------------------------------------


def test_a_claim_with_no_status_comes_back_as_sin_estado(graph, projected):
    """There is no safe default: a text expounding the doctrine it is about to
    rebut enunciates it in the same words as one who holds it, so a claim
    extracted before the field existed must not render as `afirma`."""
    source = chunk_id(projected.version, 0)
    project_claims(
        graph,
        [
            {"text": "Con estado.", "confidence": 0.9, "source_chunk_id": source,
             "status": "niega", "quote": "Casi toda la suma"},
            {"text": "Sin estado.", "confidence": 0.8, "source_chunk_id": source},
        ],
    )
    rows = {r["status"]: r for r in _rows(graph, "version_claim_shape", projected)}
    assert rows["niega"]["claims"] == 1
    assert rows["sin_estado"]["claims"] == 1


def test_only_the_claims_carrying_a_quote_are_counted_as_checkable(graph, projected):
    """`count(cl.quote)` skips nulls, which is what makes "how many of these can
    anybody check" a figure rather than an assumption. A claim nobody can check
    must not look like one that can."""
    source = chunk_id(projected.version, 0)
    project_claims(
        graph,
        [
            {"text": "Con cita.", "confidence": 0.9, "source_chunk_id": source,
             "status": "afirma", "quote": "Casi toda la suma"},
            {"text": "Sin cita.", "confidence": 0.9, "source_chunk_id": source,
             "status": "afirma"},
        ],
    )
    row = next(
        r for r in _rows(graph, "version_claim_shape", projected)
        if r["status"] == "afirma"
    )
    assert (row["claims"], row["with_quote"]) == (2, 1)


# --- concepts, by all three routes ------------------------------------------


def test_a_concept_reached_only_through_a_claim_is_still_counted(graph, projected):
    """The property `_CANDIDATE_CONCEPTS` exists for. `MENTIONS` is one route;
    a claim's `ABOUT` and its `INVOLVES` are the other two, and a concept named
    only as a claim's subject has no `MENTIONS` edge from any chunk at all."""
    # Randomised, for the reason `test_library_scope.py` records: `concept_id`
    # hashes the canonical name and nothing else, so a fixed name merges into
    # whatever this machine's real library already holds.
    from brainworker.graph.schema import concept_id

    names = [f"concepto {secrets.token_hex(8)}" for _ in range(3)]
    project_concepts(graph, [{"name": n, "type": "Doctrina"} for n in names])
    # `project_concepts` derives the id from the name and the tenant; it does
    # not take one. Deriving it here rather than inventing one is the same rule
    # the audit follows — a test that minted its own ids could only confirm its
    # own arithmetic.
    mentioned, about, involved = (concept_id(n, LEGACY_TENANT_ID) for n in names)
    source = chunk_id(projected.version, 0)
    project_claims(
        graph,
        [{"text": "Una afirmación.", "confidence": 0.9, "source_chunk_id": source}],
    )
    from brainworker.graph.schema import claim_id

    claim = claim_id(source, "Una afirmación.")
    project_semantic_edges(
        graph,
        [
            # `source_chunk_id` on every edge, not only on the MENTIONS one:
            # a re-salt that touches only nodes leaves ~49 000 edges pointing at
            # chunk ids that no longer exist.
            SemanticEdge(type="MENTIONS", source_id=source, target_id=mentioned,
                         confidence=0.9, extractor_model=MODEL,
                         source_chunk_id=source),
            SemanticEdge(type="ABOUT", source_id=claim, target_id=about,
                         confidence=0.9, extractor_model=MODEL,
                         source_chunk_id=source),
            SemanticEdge(type="INVOLVES", source_id=claim, target_id=involved,
                         confidence=0.9, extractor_model=MODEL,
                         source_chunk_id=source),
        ],
    )
    assert _rows(graph, "version_concepts_reached", projected)[0]["concepts"] == 3


def test_another_organisations_version_is_invisible_to_every_template(graph, projected):
    """The templates are named by id over HTTP, so the tenant predicate has to
    be in the Cypher. `_COUNT_VERSION_SUBGRAPH` has none because removal and the
    audit already hold the tenant; these do not have that excuse."""
    inner = graph._inner if hasattr(graph, "_inner") else graph
    args = {"version_id": projected.version, "tenant_id": "tnt_" + "0" * 23 + "9"}
    assert inner.query("version_counts", args)[0]["chunks"] == 0
    assert inner.query("version_chunk_kinds", args) == []
    assert inner.query("version_concepts_reached", args)[0]["concepts"] == 0
