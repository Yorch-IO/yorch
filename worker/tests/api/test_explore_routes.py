"""The Explore endpoints, with the graph replaced.

What is under test here is the HTTP surface — which template each route names,
what it does with a graph that is down or an id that is malformed, and whether
the response tells the UI that a result was proposed by a model. Whether the
templates return the right rows is `tests/graph/test_explore.py`, against a real
Memgraph.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker.api import main  # noqa: E402
from brainworker.graph import GraphError  # noqa: E402

VER = "ver_" + "a" * 24
SEC = "sec_" + "b" * 24
CHK = "chk_" + "c" * 24
CON = "con_" + "d" * 24


@pytest.fixture
def called(monkeypatch):
    """Record which template each route names, and answer with one row."""
    seen: list[tuple[str, dict]] = []

    def fake(template_id: str, args: dict):
        seen.append((template_id, args))
        return [{"id": "row_1", "template": template_id}]

    monkeypatch.setattr(main, "_explore", fake)
    return seen


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.mark.parametrize(
    "url,template,key",
    [
        (f"/versions/{VER}/outline", "document_outline", "sections"),
        (f"/sections/{SEC}/chunks", "section_chunks", "chunks"),
        (f"/versions/{VER}/concepts", "concepts_in_version", "concepts"),
        (f"/versions/{VER}/related", "related_documents", "documents"),
        (f"/concepts/{CON}/claims", "claims_about_concept", "claims"),
    ],
)
def test_each_route_names_exactly_one_registered_template(
    client, called, url, template, key
):
    """No route takes a template id from its caller. Memgraph does not enforce
    read-only, so the guarantee that a question cannot write is structural: the
    id is a literal in the handler and only typed parameters travel."""
    body = client.get(url).json()
    assert [t for t, _ in called] == [template]
    assert body[key] == [{"id": "row_1", "template": template}]


def test_the_context_route_fetches_the_neighbours_and_the_locator(client, called):
    """One question — "what am I looking at, and where is it in the original?" —
    so one round trip. A citation the user cannot open is not a citation."""
    body = client.get(f"/chunks/{CHK}/context").json()
    assert [t for t, _ in called] == ["chunk_neighbours", "citations_for_chunks"]
    assert body["context"]["template"] == "chunk_neighbours"
    assert body["citation"]["template"] == "citations_for_chunks"


def test_a_chunk_with_no_citation_says_none_rather_than_omitting_it(
    client, monkeypatch
):
    monkeypatch.setattr(main, "_explore", lambda *_: [])
    body = client.get(f"/chunks/{CHK}/context").json()
    assert body["context"] is None
    assert body["citation"] is None


@pytest.mark.parametrize(
    "url", [f"/versions/{VER}/concepts", f"/versions/{VER}/related",
            f"/concepts/{CON}/claims"]
)
def test_a_model_proposed_result_declares_itself(client, called, url):
    """These three templates carry `uses_semantic_edges`, and that flag exists
    because an edge a model proposed and an edge read off the document's own
    table of contents have different standing as evidence. The response repeats
    it so the UI has something to render the difference from."""
    body = client.get(url).json()
    assert body["semantic"] is True
    assert body["confidence_floor"] == 0.6


def test_the_deterministic_routes_claim_no_confidence(client, called):
    """The outline is the document's own headings. Attaching a confidence to it
    would suggest something proposed it."""
    for url in (f"/versions/{VER}/outline", f"/sections/{SEC}/chunks"):
        body = client.get(url).json()
        assert "semantic" not in body
        assert "confidence_floor" not in body


def test_the_floor_is_the_callers_to_set(client, called):
    client.get(f"/versions/{VER}/concepts?confidence_floor=0.9&limit=5")
    _, args = called[0]
    assert args["confidence_floor"] == 0.9
    assert args["limit"] == 5


def test_chunks_default_to_well_under_the_registry_clamp(client, called):
    """Chunks carry their text, so 200 of them is a few hundred kilobytes for a
    pane showing a dozen."""
    client.get(f"/sections/{SEC}/chunks")
    assert called[0][1]["limit"] == 50


def test_a_graph_that_is_down_is_a_503_naming_the_fix(client, monkeypatch):
    """"Start the stack" is an instruction; a Bolt error echoed at the user is
    not."""
    def boom(url, **kw):
        raise GraphError("connection refused")

    monkeypatch.setattr(main, "Graph", boom)
    response = client.get(f"/versions/{VER}/outline")
    assert response.status_code == 503
    assert response.json()["detail"]["kind"] == "graph_unreachable"


def test_a_malformed_identifier_is_a_400_not_a_500(client, monkeypatch):
    """These parameters arrive from the webview, so the shape check that stops a
    planner's output becoming query syntax guards them too — and a UI following
    a stale link deserves to be told, not shown a server error."""
    def boom(url, **kw):
        raise AssertionError("the id must be refused before a session is opened")

    # Nothing connects: validation runs first, so a bad id is diagnosed as a bad
    # id even when the graph is unreachable. The other order reported "start the
    # stack" for a stale link.
    monkeypatch.setattr(main, "Graph", boom)
    response = client.get("/versions/not-an-id/outline")
    assert response.status_code == 400
    assert response.json()["detail"]["kind"] == "bad_identifier"
