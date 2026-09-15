"""Deleting and re-pointing points, which is what makes a library shrinkable.

Two levels, deliberately. The request-shape tests use a fake transport and run
with the rest of the suite, which is offline by design. The round trip at the
bottom needs a real Qdrant and skips — loudly, naming the URL it tried — when
there is not one, the same bargain the worker's integration tests make.

**The disposable collection is not optional.** Leftover test points once
outnumbered real ones in this project's index 105 to 5 and silently turned eight
retrieval tests into skips, so the live test writes to a randomly-named
collection and drops it whatever happens.
"""

from __future__ import annotations

import os
import secrets

import pytest

from docagent import qdrant as qd


class FakeQdrant(qd.Qdrant):
    """Records requests instead of sending them."""

    def __init__(self, *, count: int = 3):
        self.base_url, self.collection = "", "c"
        self.calls: list[tuple[str, str, dict | None]] = []
        self._count = count

    def _ok(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/points/count"):
            return {"result": {"count": self._count}}
        return {"result": {}}


def test_a_deletion_filters_exactly_as_a_search_would():
    """`_filter` is shared with search on purpose: a selector that deleted a
    different set from the one it would have returned is the worst possible
    behaviour for this operation."""
    q = FakeQdrant()
    removed = q.delete_by_filter({"version_id": "ver_1", "library_id": "lib_1"})

    assert removed == 3
    method, path, body = q.calls[-1]
    assert method == "POST" and path.endswith("/points/delete?wait=true")
    assert body["filter"] == q._filter({"version_id": "ver_1", "library_id": "lib_1"})


def test_a_deletion_waits_for_the_points_to_actually_be_gone():
    """Without `wait=true` the call returns once the operation is queued, so a
    verification read straight afterwards still sees the points and reports a
    deletion that worked as one that did not."""
    q = FakeQdrant()
    q.delete_by_filter({"version_id": "ver_1"})
    assert all(
        "wait=true" in path
        for _, path, _ in q.calls
        if path.endswith("delete?wait=true")
    )


def test_deleting_with_no_filter_is_refused():
    """`_filter({})` is None and Qdrant reads a missing filter as every point in
    the collection. That operation exists — it is `drop()` — and must never be
    something an empty dict reaches by accident."""
    with pytest.raises(qd.QdrantError, match="drop"):
        FakeQdrant().delete_by_filter({})
    with pytest.raises(qd.QdrantError):
        FakeQdrant().set_payload({}, {"document_id": "doc_2"})


def test_deleting_nothing_makes_no_delete_request():
    """Removal is retried after a partial failure, so a second pass finding zero
    matches is the normal case, not an error."""
    q = FakeQdrant(count=0)
    assert q.delete_by_filter({"version_id": "ver_gone"}) == 0
    assert not any(p.startswith("/collections/c/points/delete") for _, p, _ in q.calls)


def test_setting_a_payload_names_only_the_keys_it_changes():
    """A merge, not a replace. The point's `text`, `char_span` and `chunk_id`
    must survive re-pointing its `document_id`."""
    q = FakeQdrant()
    affected = q.set_payload({"version_id": "ver_1"}, {"document_id": "doc_2"})

    assert affected == 3
    _, path, body = q.calls[-1]
    assert path.endswith("/points/payload?wait=true")
    assert body["payload"] == {"document_id": "doc_2"}
    assert "text" not in body["payload"]


def test_a_reverted_chunk_candidate_prunes_the_points_it_left_behind():
    """Observed indexing `07-LlavesDelPoder-INT.pdf` on 2026-08-30: the tuning
    loop tried a 625-chunk candidate, rejected it, and re-indexed the reverted
    502-chunk config. `upsert` only overwrites ids `0..501` — the ids the new
    run actually produces — so ids `502..624` from the rejected candidate
    survived in the collection, each still carrying that candidate's
    (wrong) `char_span`. Found only because a document was indexed end to
    end and the collection was scrolled and inspected by hand; nothing in
    the test suite would have caught it.

    Point ids are deterministic (`point_id(doc_id, i)`), so the stale range is
    exactly `[keep, old_count)` and needs no range filter to compute.
    """
    q = FakeQdrant(count=625)
    removed = q.prune_tail("doc_1", keep=502)

    assert removed == 123
    method, path, body = q.calls[-1]
    assert method == "POST" and path.endswith("/points/delete?wait=true")
    assert body["points"] == [qd.point_id("doc_1", i) for i in range(502, 625)]


def test_pruning_the_tail_is_a_noop_when_nothing_was_left_behind():
    """The common case — no tuning candidate ran, or it was smaller than the
    kept config — must not issue a delete request at all."""
    q = FakeQdrant(count=502)
    assert q.prune_tail("doc_1", keep=502) == 0
    assert not any(c[1].endswith("/points/delete?wait=true") for c in q.calls)


def test_the_count_is_exact():
    """It is used to tell a user how much of their library just disappeared.
    An estimate from segment metadata is fine for a progress bar and wrong for
    that."""
    q = FakeQdrant()
    q.count({"version_id": "ver_1"})
    _, path, body = q.calls[-1]
    assert path.endswith("/points/count")
    assert body["exact"] is True


# -- the round trip, against a real Qdrant ----------------------------------


def test_delete_and_repoint_against_a_live_qdrant():
    url = os.environ.get("QDRANT_URL") or os.environ.get(
        "BRAIN_QDRANT_URL", "http://127.0.0.1:6333"
    )
    collection = f"docagent_removal_{secrets.token_hex(6)}"
    try:
        probe = qd.Qdrant(url, collection)
        probe.wait_ready(timeout=3.0)
    except Exception as e:
        pytest.skip(f"no Qdrant at {url}: {type(e).__name__}: {e}")

    from docagent.bm25 import avg_doc_len, doc_sparse_vector, tokenize

    with probe as q:
        try:
            q.create(8, ("library_id", "version_id", "document_id"))
            docs = [tokenize("uno dos tres"), tokenize("cuatro cinco seis")]
            avgdl = avg_doc_len(docs)
            q.upsert(
                [
                    qd.Point(
                        id=qd.point_id("ver_1", i),
                        dense=[0.1 * (i + 1)] * 8,
                        sparse=doc_sparse_vector(docs[i], avgdl),
                        payload={
                            "library_id": "lib_1",
                            "version_id": "ver_1" if i == 0 else "ver_2",
                            "document_id": "doc_1",
                            "text": "no me toques",
                        },
                    )
                    for i in range(2)
                ]
            )

            assert q.count({"library_id": "lib_1"}) == 2

            # Re-point one, and check the untouched keys survive the merge.
            assert q.set_payload({"version_id": "ver_2"}, {"document_id": "doc_2"}) == 1
            after = {p["version_id"]: p for p in q.scroll_all()}
            assert after["ver_2"]["document_id"] == "doc_2"
            assert after["ver_2"]["text"] == "no me toques"
            assert after["ver_1"]["document_id"] == "doc_1"

            # Delete one, and check the other is still there.
            assert q.delete_by_filter({"version_id": "ver_1"}) == 1
            assert q.count({"library_id": "lib_1"}) == 1
            assert q.delete_by_filter({"version_id": "ver_1"}) == 0
        finally:
            q.drop()
