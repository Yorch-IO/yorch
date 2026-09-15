"""`/project-summary` and `/libraries/{id}/graph`, with both stores replaced.

The property these two exist for, and the one asserted hardest here: **an
unavailable figure is never a zero**. Every other route in this API turns a down
Memgraph into a 503, which is right for a screen whose whole content is the
graph. The landing screen is the exception — a hard failure there makes an empty
project and a stopped container look the same — so the degradation is tested
rather than assumed.

Whether the templates return the right rows is `tests/graph/test_library_overview.py`,
against a real Memgraph.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker.graph.schema import LEGACY_TENANT_ID
from brainworker.api import main  # noqa: E402
from brainworker.catalog.repo import ProjectTotals, RunSummary  # noqa: E402
from brainworker.graph import GraphError  # noqa: E402

LIB = "lib_overview"


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    """A settings object with the two URLs the summary reads, and nothing else."""

    class _Settings:
        database_url = "postgresql://nowhere/none"
        memgraph_url = "bolt://127.0.0.1:1"

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    return _Settings


# -- the library graph -----------------------------------------------------


@pytest.fixture
def called(monkeypatch):
    """Record which templates the route names, and answer with fixed rows."""
    seen: list[tuple[str, dict]] = []

    def fake(template_id: str, args: dict):
        seen.append((template_id, args))
        if template_id == "library_documents":
            return [
                {"document_id": "doc_1", "version_id": "ver_1", "title": "Uno", "format": "pdf"},
                {"document_id": "doc_2", "version_id": "ver_2", "title": "Dos", "format": "pdf"},
            ]
        return [
            {"version_id": "ver_1", "concept_id": "con_a", "name": "Gracia",
             "concept_type": "doctrina", "documents": 2, "mentions": 7, "confidence": 0.9},
            {"version_id": "ver_2", "concept_id": "con_a", "name": "Gracia",
             "concept_type": "doctrina", "documents": 2, "mentions": 3, "confidence": 0.8},
            {"version_id": "ver_1", "concept_id": "con_b", "name": "Bautismo",
             "concept_type": "doctrina", "documents": 1, "mentions": 2, "confidence": 0.7},
        ]

    monkeypatch.setattr(main, "_explore", fake)
    return seen


def test_the_library_graph_names_exactly_two_registered_templates(client, called):
    """The template ids are literals in the handler. Memgraph does not enforce
    read-only, so what stops a question writing is that no caller supplies an
    id — including this one."""
    assert client.get(f"/libraries/{LIB}/graph").status_code == 200
    assert [t for t, _ in called] == ["library_documents", "library_mentions"]


def test_the_library_reaches_both_templates_as_a_parameter(client, called):
    """Concepts merge by canonical name with no library in the id, so an
    unscoped traversal reaches every library that ever mentioned one. The scope
    has to arrive at the query, not merely at the handler."""
    client.get(f"/libraries/{LIB}/graph")
    assert all(args["library_id"] == LIB for _, args in called)


def test_the_callers_floor_reaches_the_mentions_template(client, called):
    client.get(f"/libraries/{LIB}/graph", params={"confidence_floor": 0.85})
    args = dict(called)["library_mentions"]
    assert args["confidence_floor"] == 0.85


def test_the_default_floor_is_the_schemas_and_not_a_fourth_copy_of_it(client, called):
    from brainworker.graph import DEFAULT_CONFIDENCE_FLOOR

    client.get(f"/libraries/{LIB}/graph")
    assert dict(called)["library_mentions"]["confidence_floor"] == DEFAULT_CONFIDENCE_FLOOR


def test_repeated_mentions_fold_into_one_concept_with_its_degree(client, called):
    """Two books mentioning one concept is one node of degree two, not two
    nodes — the whole reason `related_documents` works at all."""
    body = client.get(f"/libraries/{LIB}/graph").json()
    gracia = next(c for c in body["concepts"] if c["id"] == "con_a")
    assert gracia["documents"] == 2
    assert gracia["mentions"] == 10  # 7 + 3
    assert len(body["concepts"]) == 2
    assert len(body["edges"]) == 3


def test_the_payload_declares_itself_model_proposed(client, called):
    """Every edge rests on a MENTIONS a model proposed. A UI cannot render the
    difference between that and a table of contents unless the response says so."""
    body = client.get(f"/libraries/{LIB}/graph").json()
    assert body["semantic"] is True
    assert "confidence_floor" in body


def test_a_short_result_is_not_reported_as_truncated(client, called):
    body = client.get(f"/libraries/{LIB}/graph").json()
    assert body["truncated"] == {"documents": False, "edges": False}


def test_the_mention_cap_is_the_registrys_and_not_a_second_copy_of_it():
    """The number itself, asserted where it is cheap.

    Split from the truncation test below on 2026-09-01, when the cap went from
    20,000 to 60,000: that test built one dict per row, so its cost tracked the
    cap and raising the ceiling made the suite slower for no extra coverage.
    The two properties are different anyway — this one says the route does not
    keep its own copy of the number, and the one below says it acts on it."""

    from brainworker.graph import queries as q

    declared = next(
        p.default for p in q.get("library_mentions").params if p.name == "mention_limit"
    )
    assert main._LIBRARY_MENTION_LIMIT == declared


def test_a_full_page_of_mentions_is_reported_as_truncated(client, monkeypatch):
    """`truncated` is "at least this many", so the response cannot claim
    completeness about a list the database cut short.

    The cap is patched to a small number rather than filled to its real value:
    what is under test is the comparison, and a test whose runtime is a function
    of a production constant stops being a test of the comparison."""

    monkeypatch.setattr(main, "_LIBRARY_MENTION_LIMIT", 3)

    def fake(template_id: str, args: dict):
        if template_id == "library_documents":
            return []
        return [
            {"version_id": "ver_1", "concept_id": f"con_{i}", "name": str(i),
             "concept_type": None, "documents": 1, "mentions": 1, "confidence": 0.9}
            for i in range(3)
        ]

    monkeypatch.setattr(main, "_explore", fake)
    body = client.get(f"/libraries/{LIB}/graph").json()
    assert body["truncated"]["edges"] is True


def test_a_short_page_of_mentions_is_not_reported_as_truncated(client, monkeypatch):
    """The other side of the comparison, which nothing pinned before."""

    monkeypatch.setattr(main, "_LIBRARY_MENTION_LIMIT", 3)

    def fake(template_id: str, args: dict):
        if template_id == "library_documents":
            return []
        return [
            {"version_id": "ver_1", "concept_id": f"con_{i}", "name": str(i),
             "concept_type": None, "documents": 1, "mentions": 1, "confidence": 0.9}
            for i in range(2)
        ]

    monkeypatch.setattr(main, "_explore", fake)
    body = client.get(f"/libraries/{LIB}/graph").json()
    assert body["truncated"]["edges"] is False


def test_an_empty_library_is_empty_lists_and_not_an_error(client, monkeypatch):
    monkeypatch.setattr(main, "_explore", lambda *_: [])
    body = client.get(f"/libraries/{LIB}/graph").json()
    assert body["documents"] == [] and body["concepts"] == [] and body["edges"] == []


def test_a_down_graph_is_a_503_naming_the_fix(client, monkeypatch):
    def unreachable(*_, **__):
        raise GraphError("no route to host", kind="graph_unreachable")

    monkeypatch.setattr(main, "Graph", unreachable)
    r = client.get(f"/libraries/{LIB}/graph")
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "graph_unreachable"


# -- the project summary ---------------------------------------------------


TOTALS = ProjectTotals(
    libraries=3,
    documents=41,
    absent_documents=2,
    active_versions=39,
    indexed_versions=39,
    indexed_bytes=812_340_192,
    versions_with_pages=0,
    pages=0,
)


class _FakeCatalog:
    """Only what the summary calls. A real `Catalog` needs Postgres."""

    def __init__(self, totals=TOTALS, runs=()):
        self._totals = totals
        self._runs = list(runs)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def project_totals(self, *, tenant_id: str):
        self.asked_as = tenant_id
        return self._totals

    def libraries(self, *, tenant_id: str):
        self.asked_as = tenant_id
        return [
            {
                "id": "lib_1",
                "name": "Una",
                "language": "es",
                "documents": 3,
                "indexed_versions": 2,
            }
        ]

    def recent_runs(self, limit: int = 10, *, tenant_id: str):
        return self._runs[:limit]


def _catalog(monkeypatch, catalog):
    monkeypatch.setattr(main, "Catalog", lambda *_, **__: catalog)


def _graph_rows(monkeypatch, nodes, edges):
    class _Row:
        def __init__(self, data):
            self.data = data

    class _Graph:
        def __init__(self, *_, **__):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def query(self, template_id, _args):
            rows = nodes if template_id == "graph_node_counts" else edges
            return [_Row({"label": k, "total": v}) for k, v in rows.items()]

    monkeypatch.setattr(main, "Graph", _Graph)


def test_the_summary_reports_both_legs_when_both_answer(client, monkeypatch):
    _catalog(monkeypatch, _FakeCatalog())
    _graph_rows(
        monkeypatch,
        {"Chunk": 5000, "Concept": 700, "Claim": 900, "Document": 41},
        {"HAS_CHUNK": 5000, "MENTIONS": 8000, "ABOUT": 900},
    )
    body = client.get("/project-summary").json()
    assert body["catalog"]["available"] is True
    assert body["catalog"]["documents"] == 41
    assert body["graph"]["available"] is True
    assert body["graph"]["nodes"]["Concept"] == 700


def test_deterministic_and_semantic_edges_are_counted_apart(client, monkeypatch):
    """One total mixing HAS_CHUNK with MENTIONS would report a model's proposals
    as though the corpus had stated them."""
    _catalog(monkeypatch, _FakeCatalog())
    _graph_rows(monkeypatch, {}, {"HAS_CHUNK": 5000, "MENTIONS": 8000, "ABOUT": 900})
    graph = client.get("/project-summary").json()["graph"]
    assert graph["semantic_edges"] == {"MENTIONS": 8000, "ABOUT": 900}
    assert graph["deterministic_edges"] == {"HAS_CHUNK": 5000}


def test_a_down_graph_leaves_its_figures_null_and_never_zero(client, monkeypatch):
    """The assertion the endpoint exists for. Zero would say "this project has no
    concepts", which is a different fact with a different fix."""
    _catalog(monkeypatch, _FakeCatalog())

    def unreachable(*_, **__):
        raise GraphError("no route to host", kind="graph_unreachable")

    monkeypatch.setattr(main, "Graph", unreachable)
    body = client.get("/project-summary")
    assert body.status_code == 200
    graph = body.json()["graph"]
    assert graph["available"] is False
    assert graph["detail"]
    assert graph["nodes"] is None
    assert graph["semantic_edges"] is None
    assert graph["deterministic_edges"] is None
    # and the half that *was* readable is still there
    assert body.json()["catalog"]["documents"] == 41


def test_a_down_catalog_leaves_its_figures_null_and_never_zero(client, monkeypatch):
    def refused(*_, **__):
        raise OSError("connection refused")

    monkeypatch.setattr(main, "Catalog", refused)
    _graph_rows(monkeypatch, {"Concept": 700}, {})
    body = client.get("/project-summary")
    assert body.status_code == 200
    catalog = body.json()["catalog"]
    assert catalog["available"] is False
    assert catalog["documents"] is None
    assert catalog["indexed_versions"] is None
    # None, not []: "could not read" and "nothing has run yet" have different fixes.
    assert body.json()["recent_runs"] is None
    assert body.json()["graph"]["available"] is True


def test_pages_are_reported_unavailable_while_nothing_records_them(client, monkeypatch):
    """`page_count` is never written — `register_version` runs before extraction.
    Rendering 0 would read as "no pages", which is a claim about the corpus."""
    _catalog(monkeypatch, _FakeCatalog())
    _graph_rows(monkeypatch, {}, {})
    pages = client.get("/project-summary").json()["pages"]
    assert pages["available"] is False
    assert pages["pages"] is None
    assert pages["recorded"] == 0 and pages["of"] == 39


def test_pages_appear_by_themselves_once_something_records_them(client, monkeypatch):
    """The measurement, not a hardcoded absence: the figure starts working the
    day the column is filled in, with no change here."""
    import dataclasses

    _catalog(
        monkeypatch,
        _FakeCatalog(dataclasses.replace(TOTALS, versions_with_pages=39, pages=6120)),
    )
    _graph_rows(monkeypatch, {}, {})
    pages = client.get("/project-summary").json()["pages"]
    assert pages["available"] is True
    assert pages["pages"] == 6120


def test_a_run_outlives_the_document_it_was_spent_on(client, monkeypatch):
    """`run.document_id` is ON DELETE SET NULL on purpose. A summary that dropped
    those rows would hide exactly the history removal kept."""
    from datetime import datetime, timezone

    orphan = RunSummary(
        id="run_1",
        workflow_id="wf_1",
        kind="index",
        state="succeeded",
        stage=None,
        started_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        finished_at=None,
        error_kind=None,
        error_detail=None,
        title=None,
        library_id=None,
        label=None,
        document_id=None,
        version_id=None,
        usd_so_far=0.75,
    )
    _catalog(monkeypatch, _FakeCatalog(runs=[orphan]))
    _graph_rows(monkeypatch, {}, {})
    runs = client.get("/project-summary").json()["recent_runs"]
    assert len(runs) == 1
    assert runs[0]["workflow_id"] == "wf_1"
    assert runs[0]["title"] is None
    # The spend outlives the document too — that is the half of this the
    # ON DELETE SET NULL was chosen for, and it is what a person looks for when
    # asking why a bill is what it is.
    assert runs[0]["usd_so_far"] == 0.75


def test_a_run_that_has_not_been_billed_reports_no_spend_rather_than_zero(
    client, monkeypatch
):
    """None and 0 are different claims: nothing priced yet, against free.

    A run still inside its free stages has no `cost_entry` row at all, and the
    approval gate exists precisely so that state is common. Rendering it as
    `0.00` would say the run *is* free, which is the opposite of what it means.
    Same rule `Cost.usd` follows for a model with no known price.
    """
    from datetime import datetime, timezone

    unbilled = RunSummary(
        id="run_2",
        workflow_id="wf_2",
        kind="index",
        state="awaiting_approval",
        stage="awaiting_approval",
        started_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        finished_at=None,
        error_kind=None,
        error_detail=None,
        title="Un libro",
        library_id="lib_teologia",
        label=None,
        document_id="doc_1",
        version_id=None,
        usd_so_far=None,
    )
    _catalog(monkeypatch, _FakeCatalog(runs=[unbilled]))
    _graph_rows(monkeypatch, {}, {})
    runs = client.get("/project-summary").json()["recent_runs"]
    assert runs[0]["usd_so_far"] is None


def test_the_degree_filter_reaches_the_query_and_is_echoed(client, called):
    """`min_documents` is the volume control, so a caller must be able to see
    which one it got — a canvas showing 1,719 of 10,835 concepts has to say so."""
    body = client.get(f"/libraries/{LIB}/graph", params={"min_documents": 5}).json()
    assert dict(called)["library_mentions"]["min_documents"] == 5
    assert body["min_documents"] == 5


def test_no_filter_is_expressed_as_one_rather_than_refused(client, called):
    """0 and -1 both mean "every concept", which 1 already says. A 400 for it
    would be pedantry about a request whose intent is unambiguous."""
    body = client.get(f"/libraries/{LIB}/graph", params={"min_documents": 0}).json()
    assert dict(called)["library_mentions"]["min_documents"] == 1
    assert body["min_documents"] == 1


def test_a_concepts_degree_is_the_one_the_database_counted(client, called):
    """Not the number of rows that survived the cap. A truncated response would
    otherwise report a concept as narrower than it is — and its degree is exactly
    what a reader uses to decide whether it joins two books."""
    body = client.get(f"/libraries/{LIB}/graph").json()
    gracia = next(c for c in body["concepts"] if c["id"] == "con_a")
    assert gracia["documents"] == 2


# ---------------------------------------------------------------------------
# `/libraries`
#
# It had no test at all, and it broke: `Catalog.libraries` gained a required
# `tenant_id` and this call site was missed, so the route 500ed while the whole
# suite stayed green. The double's signature is the real one on purpose — a
# double more permissive than the function it stands for cannot catch this.
# ---------------------------------------------------------------------------


def test_the_library_list_is_served(client, monkeypatch):
    _catalog(monkeypatch, _FakeCatalog())
    r = client.get("/libraries")
    assert r.status_code == 200
    assert [row["id"] for row in r.json()["libraries"]] == ["lib_1"]


def test_the_free_plane_asks_as_the_legacy_organisation(client, monkeypatch):
    """This plane is single-tenant. Asking without naming an organisation would
    list every customer's libraries to a local install sharing one catalog."""
    catalog = _FakeCatalog()
    _catalog(monkeypatch, catalog)
    client.get("/libraries")
    assert catalog.asked_as == LEGACY_TENANT_ID
    client.get("/project-summary")
    assert catalog.asked_as == LEGACY_TENANT_ID
