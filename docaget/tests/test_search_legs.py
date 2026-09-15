"""The two search options that make a hybrid result explainable.

`SearchOpts` grew `sparse_only` and `prefetch_limit` for one reason each, and
both reasons are about *measurement* rather than about answering better:

- A hybrid result is a fused rank, so a chunk missing from it could have been
  missing from either leg or from both — three states with three different
  fixes, collapsed into one observation. `dense_only` already existed (the
  topicality gate uses it); `sparse_only` is its mirror and completes the pair.
- `prefetch_limit` was a module constant, so the one retrieval knob that decides
  *which* chunks can be ranked at all could not be swept by the tuning grid.

Asserted on the request body, like the rest of the offline Qdrant tests, so the
file runs in milliseconds with no store.
"""

from __future__ import annotations

from docagent import qdrant as qd


class FakeQdrant(qd.Qdrant):
    """Captures the request body instead of sending it."""

    def __init__(self) -> None:
        self.base_url, self.collection = "", "c"
        self.bodies: list[dict] = []

    def _ok(self, method, path, body=None):
        self.bodies.append(body or {})
        return {"result": {"points": []}}


def _prefetch(body: dict, using: str) -> dict:
    return next(p for p in body["prefetch"] if p["using"] == using)


# --- sparse_only -------------------------------------------------------------


def test_a_sparse_only_search_queries_the_bm25_vector_with_no_fusion():
    q = FakeQdrant()
    q.search([0.1], qd.SearchOpts(limit=7, sparse_only=True, query_text="jesucristo"))
    body = q.bodies[-1]
    assert body["using"] == qd.SPARSE_VEC
    assert body["limit"] == 7
    assert "prefetch" not in body, "a single leg must not go through fusion"


def test_a_cosine_floor_never_reaches_the_sparse_leg():
    """`min_score` is a cosine and means nothing against a BM25 score.

    Applying it here would not merely be meaningless — every BM25 score in this
    collection is above 1.0, so a floor of 0.6 would pass everything, while a
    floor above the top score would silently empty the leg. Either way the
    number would be doing something other than what it says.
    """
    q = FakeQdrant()
    q.search([0.1], qd.SearchOpts(min_score=0.6, sparse_only=True, query_text="x"))
    assert "score_threshold" not in q.bodies[-1]


def test_dense_only_still_wins_when_both_flags_are_set():
    """An explicit precedence, so the combination is not undefined behaviour."""
    q = FakeQdrant()
    q.search([0.1], qd.SearchOpts(dense_only=True, sparse_only=True, query_text="x"))
    assert q.bodies[-1]["using"] == qd.DENSE_VEC


# --- prefetch_limit ----------------------------------------------------------


def test_the_prefetch_width_reaches_both_legs():
    """Both, not one. A wider dense leg with a narrow sparse one would change
    which leg carries a borderline chunk, and the sweep would be measuring that
    asymmetry rather than the width."""
    q = FakeQdrant()
    q.search([0.1], qd.SearchOpts(limit=40, prefetch_limit=200, query_text="x"))
    body = q.bodies[-1]
    assert _prefetch(body, qd.DENSE_VEC)["limit"] == 200
    assert _prefetch(body, qd.SPARSE_VEC)["limit"] == 200
    assert body["limit"] == 40, "the fused limit is not the prefetch width"


def test_the_default_prefetch_width_is_still_the_module_constant():
    """A caller that names no width must get the one the engine would have used.

    This used to say that *every* caller in both planes omitted the field, which
    stopped being true when the worker's answering path grew effort levels and
    began passing one. The property that matters is unchanged and is if anything
    now load-bearing rather than merely tidy: the default level passes the same
    number this constant holds, so the two must not drift apart — a narrower
    default here would silently retrieve less for every question, and nothing
    would error.
    """
    assert qd.SearchOpts().prefetch_limit == qd.PREFETCH_LIMIT
    q = FakeQdrant()
    q.search([0.1], qd.SearchOpts(limit=40, query_text="x"))
    body = q.bodies[-1]
    assert _prefetch(body, qd.DENSE_VEC)["limit"] == qd.PREFETCH_LIMIT
    assert _prefetch(body, qd.SPARSE_VEC)["limit"] == qd.PREFETCH_LIMIT


def test_the_dense_floor_still_sits_on_the_dense_prefetch_alone():
    """Invariant #8, re-asserted through the new parameter path.

    The hybrid branch was rewritten to read `opts.prefetch_limit`; this pins
    that the rewrite did not move the threshold while it was in there.
    """
    q = FakeQdrant()
    q.search([0.1], qd.SearchOpts(limit=5, min_score=0.6, prefetch_limit=120, query_text="x"))
    body = q.bodies[-1]
    assert "score_threshold" not in body
    assert _prefetch(body, qd.DENSE_VEC)["score_threshold"] == 0.6
    assert "score_threshold" not in _prefetch(body, qd.SPARSE_VEC)
