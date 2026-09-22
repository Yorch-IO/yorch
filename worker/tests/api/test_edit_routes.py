"""`PUT /versions/{id}/chunks/{n}`, with the editing module replaced.

The HTTP surface only: what the route hands `editing.edit_chunk`, that the
tenant is the plane's own and never the caller's, and that a refusal keeps the
kind a guidance map can act on — 404 for a chunk that is not there, 503 for a
store that is not answering, and never 403, because a version another
organisation owns must be indistinguishable from one that does not exist.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker import editing  # noqa: E402
from brainworker.api import main  # noqa: E402
from brainworker.editing import EditOutcome, EditRefused  # noqa: E402
from brainworker.graph.schema import LEGACY_TENANT_ID  # noqa: E402

VER = "ver_" + "a" * 24


@pytest.fixture
def configured(monkeypatch):
    class _Gemini:
        configured = True

    class _Settings:
        gemini = _Gemini()
        database_url = "postgresql://brain:x@127.0.0.1:1/brain"

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    return monkeypatch


@pytest.fixture
def api():
    return TestClient(main.app)


def _module(monkeypatch, outcome):
    seen: list[dict] = []

    def fake(settings, **kw):
        seen.append(kw)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(editing, "edit_chunk", fake)
    return seen


def _ok(**kw) -> EditOutcome:
    base = dict(version_id=VER, chunk_index=3, reindexed=True,
                claims_checked=2, claims_unverified=1, usd=0.000002)
    return EditOutcome(**{**base, **kw})


def test_the_route_hands_the_module_the_planes_own_tenant(configured, api):
    """The free plane *is* the legacy organisation. A tenant in the body would
    be a choice this product does not have, and one taken from the caller is
    the recorded `/reindex` hole."""
    seen = _module(configured, _ok())
    r = api.put(f"/versions/{VER}/chunks/3",
                json={"text": "corregido", "tenant_id": "tnt_otro"})
    assert r.status_code == 200, r.text
    assert seen[0]["tenant_id"] == LEGACY_TENANT_ID
    assert seen[0]["version_id"] == VER and seen[0]["chunk_index"] == 3
    assert seen[0]["text"] == "corregido" and seen[0]["disabled"] is False


def test_an_undo_is_a_null_text_and_no_flag(configured, api):
    """The override is deleted and the chunk goes back to what the run
    produced, which is why the original is never overwritten anywhere."""
    seen = _module(configured, _ok(reindexed=False))
    assert api.put(f"/versions/{VER}/chunks/3", json={}).status_code == 200
    assert seen[0]["text"] is None and seen[0]["disabled"] is False


def test_hiding_a_chunk_carries_no_text(configured, api):
    seen = _module(configured, _ok(reindexed=False))
    api.put(f"/versions/{VER}/chunks/3", json={"disabled": True})
    assert seen[0]["text"] is None and seen[0]["disabled"] is True


def test_the_response_reports_what_the_edit_cost_the_claims(configured, api):
    """A quote that no longer checks out costs the claim its span, not its
    existence — and the count is how a reader learns it happened."""
    _module(configured, _ok())
    body = api.put(f"/versions/{VER}/chunks/3", json={"text": "x"}).json()
    assert body["claims_checked"] == 2 and body["claims_unverified"] == 1
    assert body["usd"] == pytest.approx(0.000002)


def test_a_missing_chunk_is_a_404_never_a_403(configured, api):
    """A version another organisation owns must be indistinguishable from one
    that does not exist — the same rule `activate` follows."""
    _module(configured, EditRefused("no existe", kind="chunk_not_found"))
    r = api.put(f"/versions/{VER}/chunks/3", json={"text": "x"})
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "chunk_not_found"


def test_a_store_that_is_down_is_a_503_with_the_kind_intact(configured, api):
    _module(configured, EditRefused("bolt", kind="graph_unreachable"))
    r = api.put(f"/versions/{VER}/chunks/3", json={"text": "x"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "graph_unreachable"


def test_an_unconfigured_project_is_refused_before_any_store_is_touched(monkeypatch, api):
    class _Gemini:
        configured = False

    class _Settings:
        gemini = _Gemini()

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    seen = _module(monkeypatch, _ok())
    r = api.put(f"/versions/{VER}/chunks/3", json={"text": "x"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "provider_unconfigured"
    assert seen == []


def test_an_oversized_edit_is_refused_by_the_framework(configured, api):
    """FastAPI's own 422-with-a-list, the shape the paid plane reproduces —
    never a hand-raised 400 carrying a kind."""
    _module(configured, _ok())
    r = api.put(f"/versions/{VER}/chunks/3", json={"text": "x" * 100_001})
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
