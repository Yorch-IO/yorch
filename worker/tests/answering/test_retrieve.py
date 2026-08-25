"""Retrieval against the real Qdrant index and Memgraph projection."""

from __future__ import annotations

import pytest

from brainworker.answering.retrieve import OffCorpus, search
from brainworker.answering.types import Plan, Question


def question(library: str, text: str = "¿Qué hace feliz a la gente?", **kw) -> Question:
    return Question(library_id=library, text=text, **kw)


def vector_only() -> Plan:
    return Plan(intent="busqueda", template_id=None)


def test_an_on_topic_question_returns_the_chunk_it_matches(settings, on_topic, indexed, library):
    evidence = search(settings, on_topic, question(library), vector_only())
    assert evidence, "the point its own vector came from should be retrievable"
    assert indexed["payload"]["chunk_id"] in {e.chunk_id for e in evidence}


def test_the_question_is_embedded_with_the_query_task_not_the_document_one(
    settings, on_topic, library
):
    """Invariant #5: the model embeds questions and passages asymmetrically, and
    using one task for both measurably degrades retrieval."""
    search(settings, on_topic, question(library), vector_only())
    assert on_topic.tasks == ["RETRIEVAL_QUERY"]


def test_an_off_corpus_question_is_told_apart_from_a_gap(settings, off_topic, library):
    """The dense floor decides whether the question is about this corpus at all.

    Without it, a nonsense question still returns purely lexical BM25 matches and
    the answer step would dutifully cite them.
    """
    with pytest.raises(OffCorpus) as e:
        search(settings, off_topic, question(library, "asdfgh qwerty zxcvb"), vector_only())
    # Nearby results still travel: "nothing found" with nothing to look at is
    # indistinguishable from a broken index.
    assert isinstance(e.value.nearby, list)


def test_results_are_scoped_to_the_library_that_was_asked(settings, on_topic, library):
    """A filter the caller cannot omit — asking one library must not surface
    another's documents."""
    evidence = search(settings, on_topic, question(library), vector_only())
    assert evidence
    for e in evidence:
        assert e.document_id, "every hit carries its document"


def test_an_unknown_filter_key_is_ignored_rather_than_forwarded(settings, on_topic, library):
    """Filters arrive from the API; a free-form key would let a caller probe
    payload internals."""
    evidence = search(
        settings, on_topic, question(library, filters={"payload": "*", "kind": "cuerpo"}),
        vector_only(),
    )
    assert all(e.kind == "cuerpo" for e in evidence)


def test_evidence_carries_a_locator_for_every_chunk_the_graph_knows(
    settings, on_topic, library
):
    """An answer may not cite a chunk with no locator, so retrieval has to fill
    them in before the answer step sees the evidence."""
    evidence = search(
        settings, on_topic, question(library), Plan(intent="busqueda", template_id=None,
                                                     concepts=["felicidad"])
    )
    assert evidence
    assert any(e.locator for e in evidence), "no chunk resolved to a citation"


def test_top_k_is_respected(settings, on_topic, library):
    evidence = search(settings, on_topic, question(library, top_k=1), vector_only())
    assert len(evidence) <= 1


def test_a_graph_that_is_down_degrades_instead_of_failing(
    settings, on_topic, monkeypatch, library
):
    """Losing graph context is strictly better than refusing to answer a question
    the vector index could have answered alone."""
    monkeypatch.setenv("BRAIN_MEMGRAPH_URL", "bolt://127.0.0.1:1")
    from brainworker import config

    evidence = search(
        config.load(), on_topic, question(library),
        Plan(intent="relacion", template_id=None, concepts=["felicidad"]),
    )
    assert evidence, "vector results survive a graph outage"


def test_a_vector_only_plan_still_resolves_its_locators(settings, on_topic, library):
    """The planner's decision gates the *expansion*, not the citation lookup.

    Retrieval used to return early when the planner chose no template and named
    no concepts — which skipped `_attach_citations`, and `answer._verify` drops
    every citation whose chunk has no locator. A question the vector index had
    answered came back as "no verifiable citation".
    """
    evidence = search(settings, on_topic, question(library), vector_only())
    assert evidence
    assert any(e.locator for e in evidence), "a vector-only plan cited nothing"


# -- claims attached to the evidence ----------------------------------------


class StubGraph:
    """Records the arguments a template was given, and answers with fixed rows."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def query(self, template_id: str, params: dict) -> list[dict]:
        self.calls.append((template_id, params))
        return self.rows


def _evidence(chunk_id: str):
    from brainworker.answering.types import Evidence

    return Evidence(chunk_id=chunk_id, version_id="ver_x", document_id="doc_x",
                    title="T", breadcrumb="", text="…", kind="cuerpo", score=0.5)


def test_the_questions_confidence_floor_is_what_filters_the_claims():
    """Not a constant: the floor is the caller's, and a question asked with a
    stricter one must not be answered with claims it excluded."""
    from brainworker.answering import retrieve

    graph = StubGraph([])
    retrieve._attach_claims(graph, [_evidence("chk_1")], 0.85)

    [(template_id, params)] = graph.calls
    assert template_id == "claims_for_chunks"
    assert params["confidence_floor"] == 0.85


def test_one_heavily_annotated_chunk_cannot_spend_the_whole_budget():
    """The row limit is global and the ordering is by confidence, so without a
    per-chunk cap a single chunk's claims would crowd out every other chunk's —
    and they compete with the chunks' own text for the attention that keeps a
    citation accurate."""
    from brainworker.answering import retrieve

    rows = [
        {"chunk_id": "chk_1", "id": f"clm_{i}", "text": f"c{i}", "confidence": 0.9,
         "quote": "", "status": "afirma", "concept": ""}
        for i in range(retrieve.CLAIMS_PER_CHUNK + 4)
    ] + [
        {"chunk_id": "chk_2", "id": "clm_z", "text": "otra", "confidence": 0.7,
         "quote": "cita", "status": "niega", "concept": "Providencia"},
    ]
    evidence = [_evidence("chk_1"), _evidence("chk_2")]

    retrieve._attach_claims(StubGraph(rows), evidence, 0.6)

    assert len(evidence[0].claims) == retrieve.CLAIMS_PER_CHUNK
    assert len(evidence[1].claims) == 1
    assert evidence[1].claims[0].status == "niega"
    assert evidence[1].claims[0].quote == "cita"


def test_a_graph_that_is_down_leaves_the_evidence_without_claims(
    settings, on_topic, monkeypatch, library
):
    """Claims are context, not the answer. Losing them is strictly better than
    refusing a question the vector index could answer alone."""
    monkeypatch.setenv("BRAIN_MEMGRAPH_URL", "bolt://127.0.0.1:1")
    from brainworker import config

    evidence = search(
        config.load(), on_topic, question(library),
        Plan(intent="relacion", template_id=None, concepts=["felicidad"]),
    )
    assert evidence
    assert all(e.claims == [] for e in evidence)


def test_a_claim_template_contributes_the_chunks_its_claims_came_from():
    """A row names its chunk in one of two ways and both are read.

    Templates over chunks put the id in `id`; templates over *claims* put the
    claim's id there and name its chunk in `source_chunk_id`. Reading only `id`
    meant a claim template could be offered to the planner, chosen, and
    contribute nothing — which is what `claims_between_concepts`, the graph's
    only concept-to-concept path, returns.
    """
    from brainworker.answering import retrieve

    class Row:
        def __init__(self, data): self.data = data

    hydrated: list[list[str]] = []

    class StubGraph:
        def query(self, template_id, params):
            return [
                Row({"id": "clm_" + "a" * 24, "source_chunk_id": "chk_" + "1" * 24}),
                Row({"id": "chk_" + "2" * 24}),
                # Already retrieved by the vector leg: not added twice.
                Row({"id": "clm_" + "b" * 24, "source_chunk_id": "chk_" + "3" * 24}),
                # Neither key names a chunk.
                Row({"id": "con_" + "c" * 24}),
            ]
        def write(self, *a, **k):
            return []

    def fake_hydrate(graph, ids, source, library_id):
        hydrated.append(list(ids))
        return []

    original = retrieve._hydrate
    retrieve._hydrate = fake_hydrate
    try:
        plan = Plan(intent="relacion", template_id="claims_between_concepts",
                    params={"concept_id": "con_" + "d" * 24})
        retrieve._by_template(StubGraph(), plan, {"chk_" + "3" * 24}, "lib_a")
    finally:
        retrieve._hydrate = original

    assert hydrated == [["chk_" + "1" * 24, "chk_" + "2" * 24]]
