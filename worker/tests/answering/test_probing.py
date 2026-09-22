"""`probing.probe` against the real index, with the provider doubled.

What `proberetrieval.py` decides is unit-tested there; what is asserted here is
that the driver hands it production's numbers and that every candidate comes
back with its legs pulled apart — the thing the screen exists to show.
"""

from __future__ import annotations

from brainworker.answering.effort import budget_for
from brainworker.answering.retrieve import MIN_SCORE, PER_SECTION
from brainworker.graph.schema import LEGACY_TENANT_ID
from brainworker.probing import ProbeRequest, probe


def _req(library: str, **kw) -> ProbeRequest:
    return ProbeRequest(question="¿Qué hace feliz a la gente?", library_id=library,
                        tenant_id=LEGACY_TENANT_ID, **kw)


def test_every_candidate_carries_its_legs_pulled_apart(settings, on_topic, indexed, library):
    on_topic.settings.rerank_model = ""
    report = probe(settings, _req(library, effort="brief"), provider=on_topic)
    assert report["on_topic"] is True
    assert report["candidates"], "the point its own vector came from is a candidate"
    first = report["candidates"][0]
    for key in ("chunk_id", "rrf_rank", "rank", "dense_rank", "dense_score",
                "sparse_rank", "sparse_score", "rerank_score", "delivered_rank", "preview"):
        assert key in first, key
    # The point whose own vector was the question ranks first on the dense leg.
    own = [c for c in report["candidates"] if c["chunk_id"] == indexed["payload"]["chunk_id"]]
    assert own and own[0]["dense_rank"] == 1
    delivered = [c for c in report["candidates"] if c["delivered_rank"]]
    assert 0 < len(delivered) <= budget_for("brief").top_k


def test_the_probe_reproduces_productions_numbers_not_its_own(settings, on_topic, library):
    """A probe that explained a retrieval production never ran is worse than none."""
    on_topic.settings.rerank_model = ""
    served = probe(settings, _req(library, effort="thorough"), provider=on_topic)["served_with"]
    b = budget_for("thorough")
    assert served["min_score"] == MIN_SCORE and served["per_section"] == PER_SECTION
    assert served["top_k"] == b.top_k and served["candidate_limit"] == b.candidate_limit
    assert served["prefetch_limit"] == b.prefetch_limit
    assert served["reranked"] is False and served["rerank_model"] == ""


def test_placing_a_chunk_that_was_delivered_says_so(settings, on_topic, indexed, library):
    on_topic.settings.rerank_model = ""
    target = indexed["payload"]["chunk_id"]
    report = probe(settings, _req(library, chunk_id=target, effort="brief"), provider=on_topic)
    assert report["target"]["verdict"]["reached"] is True
    assert report["target"]["dense"]["clears_floor"] is True


def test_reranking_runs_where_and_when_production_runs_it(settings, on_topic, library):
    """`brief` reranks and the report carries the score per candidate; `thorough`
    does not, and neither does an off-corpus question."""
    on_topic.settings.rerank_model = "semantic-ranker-default-005"
    on_topic.rank_script = lambda texts: [1.0 - i / len(texts) for i in range(len(texts))]
    report = probe(settings, _req(library, effort="brief"), provider=on_topic)
    assert report["served_with"]["reranked"] is True
    assert all(c["rerank_score"] is not None for c in report["candidates"])
    assert report["spent"]["rerank_usd"] > 0 and report["spent"]["recorded"] is False
    on_topic.rank_calls.clear()
    probe(settings, _req(library, effort="thorough"), provider=on_topic)
    assert on_topic.rank_calls == []


def test_an_off_corpus_question_delivers_nothing_and_pays_for_no_ranking(
    settings, off_topic, library
):
    off_topic.settings.rerank_model = "semantic-ranker-default-005"
    report = probe(settings, ProbeRequest(question="asdfgh qwerty zxcvb", library_id=library,
                                          tenant_id=LEGACY_TENANT_ID, effort="brief",
                                          chunk_id="chk_" + "c" * 24), provider=off_topic)
    assert report["on_topic"] is False
    assert not [c for c in report["candidates"] if c["delivered_rank"]]
    assert off_topic.rank_calls == []
    assert "refused as off-corpus" in report["target"]["note"]


def test_the_probe_excludes_a_hidden_chunk_exactly_as_production_does(
    settings, on_topic, indexed, library, monkeypatch
):
    """The probe's whole contract is that it reproduces the retrieval
    production runs. It did not carry the exclusion, and the first hidden
    chunk showed up ranking third in a probe of a search that would never
    return it."""
    from docagent.qdrant import Excluded

    seen: list[dict] = []
    from docagent import qdrant as q

    original = q.Qdrant.search

    def spy(self, vector, opts):
        seen.append(dict(opts.filters))
        return original(self, vector, opts)

    monkeypatch.setattr(q.Qdrant, "search", spy)
    on_topic.settings.rerank_model = ""
    probe(settings, _req(library, effort="brief"), provider=on_topic)

    # The two *placing* legs look without it — "where would this rank if it
    # were not hidden" is the question somebody probing a hidden chunk asks —
    # and the two production legs carry it.
    served = [f for f in seen if isinstance(f.get("disabled"), Excluded)]
    assert len(served) == 2, f"production's legs must carry the exclusion: {seen}"
    assert all("disabled" not in f for f in seen if f not in served)


def test_a_hidden_chunk_is_named_as_hidden_and_not_as_a_width_problem(
    settings, on_topic, indexed, library
):
    """The only gate whose remedy is a person rather than a parameter.
    Reporting `prefetch` would send somebody to tune a number that was never
    in the way — the failure `where_lost` exists to prevent."""
    on_topic.settings.rerank_model = ""
    target = indexed["payload"]["chunk_id"]

    plain = probe(settings, _req(library, chunk_id=target, effort="brief"),
                  provider=on_topic)
    assert plain["target"]["verdict"]["lost_at"] != "hidden"

    from brainworker.probing import _is_hidden

    class _Hit:
        payload = {"chunk_id": target, "disabled": True}

    assert _is_hidden([_Hit()], [], target) is True
    assert _is_hidden([], [], target) is False
