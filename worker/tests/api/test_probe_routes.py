"""`POST /libraries/{id}/probe`, with the driver replaced.

The HTTP surface only: what the route hands `probing.probe`, that the tenant
is the plane's own and never the caller's, how a bad level and a down store
are answered, and that the response is marked as the sandbox it is. What the
driver reports against a real index is exercised by
`scripts/probe_retrieval.py` and by the retrieval tests.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker.api import main  # noqa: E402
from brainworker.graph.schema import LEGACY_TENANT_ID  # noqa: E402
from brainworker import probing  # noqa: E402
from brainworker.providers import ProviderError  # noqa: E402


@pytest.fixture
def configured(monkeypatch):
    class _Gemini:
        configured = True
        rerank_model = "semantic-ranker-default-005"

    class _Settings:
        gemini = _Gemini()
        qdrant_url = "http://127.0.0.1:1"

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    return monkeypatch


@pytest.fixture
def api():
    return TestClient(main.app)


def _driver(monkeypatch, outcome):
    seen: list[probing.ProbeRequest] = []

    def fake(settings, req):
        seen.append(req)
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)

    monkeypatch.setattr(probing, "probe", fake)
    return seen


def test_the_route_hands_the_driver_the_planes_own_tenant(configured, api):
    """The free plane *is* the legacy organisation. A tenant in the body would be
    a choice this product does not have, and one taken from the caller would be
    the recorded `/reindex` hole from a new direction."""
    seen = _driver(configured, {"candidates": [], "spent": {}})
    r = api.post(
        "/libraries/lib_a/probe",
        json={"question": "¿qué?", "chunk_id": "chk_" + "c" * 24, "effort": "brief",
              "tenant_id": "tnt_someone_else"},
    )
    assert r.status_code == 200, r.text
    assert len(seen) == 1
    req = seen[0]
    assert req.tenant_id == LEGACY_TENANT_ID
    assert req.library_id == "lib_a"
    assert req.chunk_id == "chk_" + "c" * 24
    assert req.effort == "brief"


def test_the_response_says_it_is_a_sandbox(configured, api):
    """Nothing changed here changes what a question is answered with. A debug
    screen whose knobs were settings would be a settings screen nobody audited,
    and the flag is what the screen prints the sentence from."""
    _driver(configured, {"candidates": [], "spent": {"rerank_usd": 0.001, "recorded": False}})
    body = api.post("/libraries/lib_a/probe", json={"question": "¿qué?"}).json()
    assert body["sandbox"] is True
    assert body["spent"]["recorded"] is False


def test_a_chunk_is_optional_so_the_ranked_view_stands_alone(configured, api):
    seen = _driver(configured, {"candidates": [], "spent": {}})
    assert api.post("/libraries/lib_a/probe", json={"question": "¿qué?"}).status_code == 200
    assert seen[0].chunk_id == "" and seen[0].effort == "standard"


def test_a_bad_level_is_refused_by_the_framework_not_by_hand(configured, api):
    """FastAPI's own 422-with-a-list, the shape the paid plane reproduces on
    purpose — never a hand-raised 400 carrying a kind."""
    _driver(configured, {"candidates": [], "spent": {}})
    r = api.post("/libraries/lib_a/probe", json={"question": "¿qué?", "effort": "extreme"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


def test_an_unconfigured_project_is_refused_before_any_store_is_touched(monkeypatch, api):
    class _Gemini:
        configured = False

    class _Settings:
        gemini = _Gemini()

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    seen = _driver(monkeypatch, {"candidates": []})
    r = api.post("/libraries/lib_a/probe", json={"question": "¿qué?"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "provider_unconfigured"
    assert seen == []


def test_a_provider_failure_keeps_its_kind(configured, api):
    _driver(configured, ProviderError("no ADC", kind="provider_no_credentials"))
    r = api.post("/libraries/lib_a/probe", json={"question": "¿qué?"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "provider_no_credentials"


def test_a_down_index_is_a_503_naming_the_fix(configured, api):
    """Same shape as `graph_unreachable`: the answer is "start the stack", and
    a kind the app's guidance map knows beats a transport error echoed back."""
    from docagent.qdrant import QdrantError

    _driver(configured, QdrantError("POST /collections/brain/points/query -> HTTP 000"))
    r = api.post("/libraries/lib_a/probe", json={"question": "¿qué?"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "qdrant_unreachable"
