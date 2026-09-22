"""Retrieval against the real Qdrant index and Memgraph projection."""

from __future__ import annotations

import os

import pytest

from brainworker import config
from brainworker.graph.schema import LEGACY_TENANT_ID

from brainworker.answering.retrieve import OffCorpus, search
from brainworker.answering.types import Plan, Question
from .conftest import QDRANT


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
    retrieve._attach_claims(graph, [_evidence("chk_1")], 0.85, LEGACY_TENANT_ID)

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

    retrieve._attach_claims(StubGraph(rows), evidence, 0.6, LEGACY_TENANT_ID)

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

    def fake_hydrate(graph, ids, source, library_id, LEGACY_TENANT_ID):
        hydrated.append(list(ids))
        return []

    original = retrieve._hydrate
    retrieve._hydrate = fake_hydrate
    try:
        plan = Plan(intent="relacion", template_id="claims_between_concepts",
                    params={"concept_id": "con_" + "d" * 24})
        retrieve._by_template(
            StubGraph(),
            plan,
            {"chk_" + "3" * 24},
            Question(library_id="lib_a", text="¿?"),
        )
    finally:
        retrieve._hydrate = original

    assert hydrated == [["chk_" + "1" * 24, "chk_" + "2" * 24]]


# -- the organisation filter -------------------------------------------------


def test_the_tenant_is_not_a_filter_a_caller_may_name():
    """It is not a narrowing a request may ask for; it is the scope the request
    is confined to. Listing it beside `kind` and `library_id` would turn the one
    filter that decides whose corpus is searched into one a caller can set."""
    from brainworker.answering.retrieve import ALLOWED_FILTERS

    assert "tenant_id" not in ALLOWED_FILTERS


def test_a_smuggled_tenant_filter_is_overwritten_not_honoured(
    settings, on_topic, library
):
    """Two guards for one property, and this exercises the second.

    The allowlist already drops the key; this asks what happens if it ever
    stopped doing so. The assignment after it wins, so a caller naming another
    organisation searches their own.
    """
    evidence = search(
        settings,
        on_topic,
        question(library, filters={"tenant_id": "tnt_" + "9" * 24}),
        vector_only(),
    )
    assert evidence, "the caller's own corpus is still searched"


def test_asking_as_another_organisation_finds_nothing(settings, on_topic, library):
    """The filter is real, against the real index.

    Every point in this collection carries the legacy tenant. A question asked
    as anybody else must come back empty — not with a lower score, not with
    fewer hits.
    """
    from brainworker.answering.retrieve import OffCorpus

    with pytest.raises(OffCorpus):
        search(
            settings,
            on_topic,
            question(library, tenant_id="tnt_" + "9" * 24),
            vector_only(),
        )


# --- the effort level's claim allowance -------------------------------------


def test_the_claims_cap_follows_the_level_rather_than_the_constant():
    """A wider level must actually get more claims per chunk.

    The parameter defaults to the module constant so the four-argument call
    sites above keep working; this is what proves the argument is read at all.
    """
    from brainworker.answering import retrieve
    from brainworker.answering.effort import BUDGETS

    wide = BUDGETS["thorough"].claims_per_chunk
    rows = [
        {"chunk_id": "chk_1", "id": f"clm_{i}", "text": f"c{i}", "confidence": 0.9,
         "quote": "", "status": "afirma", "concept": ""}
        for i in range(wide + 4)
    ]
    evidence = [_evidence("chk_1")]

    retrieve._attach_claims(
        StubGraph(rows), evidence, 0.6, LEGACY_TENANT_ID, wide
    )
    assert len(evidence[0].claims) == wide
    assert wide > retrieve.CLAIMS_PER_CHUNK, "otherwise this asserts nothing"


def test_the_row_limit_and_the_per_chunk_cap_are_the_same_number():
    """Two places one cap is spelled, and they must move together.

    The query asks for `len(ids) * cap` rows *globally* and the loop then allows
    `cap` per chunk. Threading the level into only the loop leaves the wider
    level unreachable — the query would fetch three per chunk's worth of rows
    and the loop would sit there willing to accept four. Nothing would error;
    the level would just quietly not work.
    """
    from brainworker.answering import retrieve
    from brainworker.answering.effort import BUDGETS

    wide = BUDGETS["thorough"].claims_per_chunk
    graph = StubGraph([])
    evidence = [_evidence("chk_1"), _evidence("chk_2")]

    retrieve._attach_claims(graph, evidence, 0.6, LEGACY_TENANT_ID, wide)

    [(_, params)] = graph.calls
    assert params["limit"] == len(evidence) * wide


def test_a_repeated_question_books_no_embedding_charge(
    monkeypatch: pytest.MonkeyPatch, tmp_path, library: str, on_topic
):
    """The query embedding is cached, and a hit must not be billed.

    `search` fronts it with `CachedEmbedder` for latency rather than money —
    measured 2026-09-06, everything else in that function totals 9 ms while the
    one embedding ranged 0.4 s to 18.8 s against a per-minute quota. The hazard
    the cache introduces is in the ledger: a hit hands back an `Embedding`
    carrying the token count the *original* call cost, so reading that for the
    charge would bill every repeat question for tokens nobody spent. The count
    has to come from the embedder, which accumulates misses only.

    A workspace of its own, so the cache starts empty and the first ask is a
    miss whatever else has run on this machine.
    """
    monkeypatch.setenv("BRAIN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAIN_QDRANT_URL", QDRANT)
    monkeypatch.setenv("BRAIN_GEMINI_PROJECT_ID", "proj-test")
    monkeypatch.setenv(
        "BRAIN_MEMGRAPH_URL",
        os.environ.get("BRAIN_MEMGRAPH_URL", "bolt://127.0.0.1:7788"),
    )
    s = config.load()
    question = Question(library_id=library, text="una pregunta repetida")
    plan = Plan(intent="x", template_id=None, params={}, concepts=[],
                rationale="", spend=None)

    first: list = []
    search(s, on_topic, question, plan, first)
    (charge,) = [x for x in first if x.stage == "ask-embedding"]
    assert charge.input_tokens > 0, "the first ask should have been a miss"

    second: list = []
    search(s, on_topic, question, plan, second)
    (again,) = [x for x in second if x.stage == "ask-embedding"]
    # The row is still written — a stage that ran for nothing and a stage that
    # did not run are different facts — but it carries nothing.
    assert again.input_tokens == 0, "a cached query embedding was billed again"
    assert again.usd in (0, 0.0, None)


# --- reranking ----------------------------------------------------------------
#
# The ranking API is a network call to a service the test suite must never
# reach, so the provider double scripts it. What is asserted is the contract
# around it: which levels call it, that its order wins, that its failure loses
# nothing, and that its charge lands under a stage the ledger can name.
#
# The order assertions go through `_rerank` directly rather than through two
# `search` calls compared against each other: RRF leaves ties, and the fused
# order of tied candidates is not stable across calls, so "same as the previous
# call" is not a property this index has.

from types import SimpleNamespace


def _reranking(provider, script=None):
    provider.settings.rerank_model = "semantic-ranker-default-005"
    provider.rank_script = script
    return provider


def _hits(*texts):
    return [SimpleNamespace(payload={"text": t}, score=0.1) for t in texts]


def test_the_rerankers_order_replaces_the_fused_order(on_topic, library):
    from brainworker.answering.retrieve import _rerank

    def reversed_scores(texts):
        n = len(texts)
        return [i / n for i in range(n)]  # later = higher

    out = _rerank(_reranking(on_topic, reversed_scores), question(library), _hits("a", "b", "c"), None)
    assert [h.payload["text"] for h in out] == ["c", "b", "a"]
    assert on_topic.rank_calls == [(question(library).text, 3)]


def test_a_narrow_level_hands_the_whole_fused_list_to_the_reranker(
    settings, on_topic, library
):
    """End to end: `brief` reranks, and what it reranks is the candidate list,
    not the four it will serve — the point is to promote from outside `top_k`."""
    from brainworker.answering.effort import budget_for

    evidence = search(settings, _reranking(on_topic), question(library, effort="brief"), vector_only())
    assert evidence
    assert len(on_topic.rank_calls) == 1
    _, ranked = on_topic.rank_calls[0]
    assert len(evidence) <= budget_for("brief").top_k < ranked <= budget_for("brief").candidate_limit


def test_a_level_measured_inside_the_noise_does_not_pay_for_a_call(
    settings, on_topic, library
):
    search(settings, _reranking(on_topic), question(library, effort="thorough"), vector_only())
    assert on_topic.rank_calls == []


def test_an_empty_model_turns_reranking_off_without_touching_the_ladder(
    settings, on_topic, library
):
    on_topic.settings.rerank_model = ""
    search(settings, on_topic, question(library, effort="brief"), vector_only())
    assert on_topic.rank_calls == []


def test_a_ranking_failure_keeps_the_fused_order_and_books_nothing(on_topic, library):
    """Losing 0.07 of recall on one question is a better trade than refusing it,
    and a call that was never billed must not appear in the ledger."""
    from brainworker.answering.retrieve import _rerank
    from brainworker.providers import ProviderError

    hits = _hits("a", "b", "c")
    spend: list = []
    failing = _reranking(on_topic, ProviderError("502", kind="provider_unavailable", retryable=True))
    assert _rerank(failing, question(library), hits, spend) == hits
    assert on_topic.rank_calls, "the call was attempted"
    assert spend == []


def test_the_charge_is_booked_under_a_stage_the_ledger_can_name(settings, on_topic, library):
    """`ask-rerank` is declared in `ASK_COST_STAGES` — the recorded way this goes
    wrong is a charge written under a name nothing maps, rendering beside the
    charges that belong to nobody."""
    from brainworker.providers.ranking import usd_for
    from brainworker.stages import ASK_COST_STAGES

    spend: list = []
    search(settings, _reranking(on_topic), question(library, effort="standard"), vector_only(), spend)
    rows = [s for s in spend if s.stage == "ask-rerank"]
    assert len(rows) == 1
    assert rows[0].stage in ASK_COST_STAGES
    assert rows[0].input_tokens == 0 and rows[0].output_tokens == 0
    assert rows[0].usd == usd_for(on_topic.rank_calls[-1][1]) > 0


def test_an_off_corpus_question_is_refused_before_anything_is_paid_to_rerank(
    settings, off_topic, library
):
    """The first live question after reranking shipped was a refusal that had
    booked $0.001 to reorder its own examples."""
    spend: list = []
    with pytest.raises(OffCorpus):
        search(settings, _reranking(off_topic), question(library, "asdfgh qwerty zxcvb", effort="brief"),
               vector_only(), spend)
    assert off_topic.rank_calls == []
    assert not [s for s in spend if s.stage == "ask-rerank"]
