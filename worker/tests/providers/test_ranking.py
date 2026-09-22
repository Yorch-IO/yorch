"""The ranking client, with the network scripted.

What is worth asserting is the contract: the price arithmetic the pricing page
states, the normalisation rule borrowed from RAGFlow, the retry on the statuses
that mean "not you", and the classification of the ones that do not.
"""

from __future__ import annotations

import pytest

from brainworker.providers.gemini import ProviderError
from brainworker.providers import ranking
from brainworker.providers.ranking import Ranker, normalise, queries_for, usd_for


class _Resp:
    def __init__(self, status: int, body=None, headers=None, text="") -> None:
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._body


def _scripted(monkeypatch, responses):
    """A ranker whose POSTs answer from a list and whose sleeps are recorded."""
    r = Ranker("proj-test")
    calls: list[dict] = []
    slept: list[float] = []

    def post(body):
        calls.append(body)
        return responses.pop(0)

    monkeypatch.setattr(r, "_post", post)
    monkeypatch.setattr(ranking.time, "sleep", lambda s: slept.append(s))
    return r, calls, slept


# --- price -------------------------------------------------------------------


@pytest.mark.parametrize(
    "records, queries",
    [(0, 0), (1, 1), (20, 1), (100, 1), (101, 2), (120, 2), (132, 2), (200, 2), (399, 4), (401, 5)],
)
def test_a_query_is_a_hundred_records_as_the_pricing_page_defines_it(records, queries):
    """Verbatim from the page: '132 documents to rank = 2 queries … 399 = 4 … 401 = 5'."""
    assert queries_for(records) == queries
    assert usd_for(records) == pytest.approx(queries * ranking.RANK_USD_PER_1000_QUERIES / 1000)


def test_the_narrow_levels_cost_a_tenth_of_a_cent():
    from brainworker.answering.effort import BUDGETS

    for level in ("brief", "standard"):
        assert usd_for(BUDGETS[level].candidate_limit) == pytest.approx(0.001)


# --- normalisation -----------------------------------------------------------


def test_a_calibrated_batch_is_passed_through_unchanged():
    """RAGFlow's rule, and the reason: re-scaling three scores already in
    [0, 1] would turn the worst candidate into a zero it did not earn."""
    assert normalise([0.87, 0.11, 0.07]) == [0.87, 0.11, 0.07]


def test_an_unbounded_batch_is_min_maxed():
    assert normalise([4.0, 2.0, 0.0]) == [1.0, 0.5, 0.0]


def test_a_spreadless_batch_is_clamped_not_zeroed():
    assert normalise([3.0, 3.0]) == [1.0, 1.0]
    assert normalise([-2.0]) == [0.0]
    assert normalise([]) == []


# --- the call ----------------------------------------------------------------


def _ok(scores):
    return _Resp(200, {"records": [{"id": str(i), "score": s} for i, s in enumerate(scores)]})


def test_scores_come_back_in_the_order_the_texts_were_given(monkeypatch):
    # The API answers sorted by score; the caller wants them by input position.
    r, calls, _ = _scripted(monkeypatch, [
        _Resp(200, {"records": [{"id": "2", "score": 0.9}, {"id": "0", "score": 0.5}, {"id": "1", "score": 0.1}]})
    ])
    out = r.rank("q", ["a", "b", "c"])
    assert out.scores == [0.5, 0.1, 0.9]
    assert out.records == 3 and out.queries == 1
    assert calls[0]["topN"] == 3 and [x["content"] for x in calls[0]["records"]] == ["a", "b", "c"]


def test_a_missing_id_scores_zero_rather_than_shifting_its_neighbours(monkeypatch):
    r, _, _ = _scripted(monkeypatch, [_Resp(200, {"records": [{"id": "0", "score": 0.4}]})])
    assert r.rank("q", ["a", "b"]).scores == [0.4, 0.0]


def test_no_texts_means_no_call(monkeypatch):
    r, calls, _ = _scripted(monkeypatch, [])
    assert r.rank("q", []).records == 0
    assert calls == []


def test_a_502_is_retried_and_the_second_answer_wins(monkeypatch):
    """The 502 that interrupted the first real measurement, two hundred calls in."""
    r, calls, slept = _scripted(monkeypatch, [_Resp(502, text="Bad Gateway"), _ok([0.3])])
    assert r.rank("q", ["a"]).scores == [0.3]
    assert len(calls) == 2 and len(slept) == 1


def test_retry_after_wins_over_the_backoff(monkeypatch):
    r, _, slept = _scripted(monkeypatch, [_Resp(429, headers={"retry-after": "7"}), _ok([0.3])])
    r.rank("q", ["a"])
    assert slept == [7.0]


def test_a_status_that_will_not_change_is_not_retried(monkeypatch):
    r, calls, _ = _scripted(monkeypatch, [_Resp(403, text="PERMISSION_DENIED")])
    with pytest.raises(ProviderError) as e:
        r.rank("q", ["a"])
    assert e.value.kind == "provider_forbidden" and not e.value.retryable
    assert len(calls) == 1


def test_the_attempts_run_out_with_the_retryable_kind(monkeypatch):
    r, calls, _ = _scripted(monkeypatch, [_Resp(503)] * ranking.ATTEMPTS)
    with pytest.raises(ProviderError) as e:
        r.rank("q", ["a"])
    assert e.value.kind == "provider_unavailable" and e.value.retryable
    assert len(calls) == ranking.ATTEMPTS


def test_an_unconfigured_project_is_refused_before_any_network():
    with pytest.raises(ProviderError) as e:
        Ranker("")
    assert e.value.kind == "provider_unconfigured"


def test_the_endpoint_is_global_like_everything_else_here():
    """`location` is `global`, never a region — the recorded reason is that
    regionalising silently pins the product elsewhere. Same rule, new service."""
    assert "/locations/global/" in Ranker("proj-test").url
    assert Ranker("proj-test").url.startswith("https://discoveryengine.googleapis.com/v1/projects/proj-test/")
