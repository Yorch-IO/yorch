"""One version's statistics, and the rule every leg of it is built on.

The route reads three stores and two artifacts, and **any of them can be down**.
The property under test throughout is `/project-summary`'s: a leg that could not
answer says so and carries *no figures at all*, so nothing it would have
reported can be quoted by accident. A stopped Memgraph is not a version with no
concepts; a run that never measured is not a recall of zero; an unpriced charge
is not a free one.

The second property is the ownership predicate. A salted id is not
authorization — `ver_` is `digest(tenant, content)` and a tenant id is a value
its own members hold — so a version reachable by id must still be refused when
no document in *this* library holds it, with 404 and never 403.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker.api import main  # noqa: E402
from brainworker.catalog.repo import Cost, Document, RunSummary, Version  # noqa: E402
from brainworker.graph.schema import LEGACY_TENANT_ID  # noqa: E402

T0 = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
LIB = "lib_teologia"
VER = "ver_" + "a" * 24
DOC = "doc_" + "b" * 24
RUN = "ingest-1787000000000-abcdef01"


def _document(**kw) -> Document:
    base = dict(
        id=DOC, library_id=LIB, folder_id=None, source_key="libros/x.pdf",
        title="Institución", author=None, format="pdf", present=True,
        absent_since=None, tags=[], created_at=T0, updated_at=T0,
        source_path="/workspace/inbox/x.pdf", tenant_id=LEGACY_TENANT_ID,
    )
    base.update(kw)
    return Document(**base)


def _version(**kw) -> Version:
    base = dict(
        id=VER, content_sha256="f" * 64, byte_size=1234, page_count=None,
        state="indexed", activated_at=T0, failed_reason=None, created_at=T0,
    )
    base.update(kw)
    return Version(**base)


def _run(**kw) -> RunSummary:
    base = dict(
        id=RUN, workflow_id=RUN, kind="index", state="succeeded", stage="done",
        started_at=T0, finished_at=T0, error_kind=None, error_detail=None,
        title="Institución", library_id=LIB, label=None, document_id=DOC,
        version_id=VER, usd_so_far=0.05,
    )
    base.update(kw)
    return RunSummary(**base)


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    class _Settings:
        database_url = "postgresql://nowhere/none"
        workspace = tmp_path
        memgraph_url = "bolt://127.0.0.1:9"
        qdrant_url = "http://127.0.0.1:9"
        qdrant_collection = "brain_test"

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    return _Settings


class _FakeCatalog:
    """Only what this route calls. A real `Catalog` needs Postgres."""

    def __init__(self, *, holders=(DOC,), document=None, versions=None,
                 runs=(), costs=(), artifacts=(), warnings=(), latest=None):
        self._holders = list(holders)
        self._document = _document() if document is None else document
        self._versions = [_version()] if versions is None else list(versions)
        self._runs, self._costs = list(runs), list(costs)
        self._artifacts, self._warnings = list(artifacts), list(warnings)
        self._latest = latest or {}
        self.asked: dict[str, object] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def documents_holding(self, version_id):
        self.asked["holding"] = version_id
        return list(self._holders)

    def document(self, document_id, *, library_id=None, tenant_id=None):
        self.asked["document"] = (document_id, library_id, tenant_id)
        if self._document is None:
            return None
        if library_id and self._document.library_id != library_id:
            return None
        return self._document

    def versions_of(self, document_id):
        return list(self._versions)

    def active_version(self, document_id):
        return VER

    def runs(self, *, tenant_id, version_id=None, limit=25, **_):
        self.asked["runs"] = (tenant_id, version_id, limit)
        return list(self._runs)

    def latest_run_with_artifact(self, version_id, kind):
        return self._latest.get(kind)

    def artifacts(self, run_id):
        return list(self._artifacts)

    def costs(self, run_id, *, tenant_id):
        return [c for r, c in self._costs if r == run_id]

    def open_profile_warnings(self, version_id):
        return list(self._warnings)


@pytest.fixture
def catalog(monkeypatch):
    made = {}

    def install(cat):
        made["cat"] = cat
        monkeypatch.setattr(main, "Catalog", lambda *a, **k: cat)
        return cat

    return install


@pytest.fixture(autouse=True)
def no_graph(monkeypatch):
    """Every test that wants the graph up says so; the default is down."""
    monkeypatch.setattr(
        main, "_statistics_counts", lambda *a, **k: (None, "bolt://nowhere:9: refused")
    )


def _get(client, version=VER, library=LIB):
    return client.get(f"/libraries/{library}/versions/{version}/statistics")


# --- the boundary ---------------------------------------------------------


def test_a_version_no_document_in_this_library_holds_is_not_found(client, catalog):
    """An id is not authorization. The lookup is what refuses, and it refuses
    with 404 rather than 403: which organisations exist is not this caller's
    business either."""
    catalog(_FakeCatalog(holders=[]))
    r = _get(client)
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "version_not_found"


def test_another_librarys_version_is_not_found_rather_than_forbidden(client, catalog):
    catalog(_FakeCatalog(document=_document(library_id="lib_otra")))
    r = _get(client)
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "version_not_found"


def test_the_ownership_lookup_names_the_library_and_the_tenant(client, catalog):
    cat = catalog(_FakeCatalog())
    assert _get(client).status_code == 200
    assert cat.asked["document"] == (DOC, LIB, LEGACY_TENANT_ID)


def test_the_runs_are_read_for_this_version_and_this_tenant(client, catalog):
    cat = catalog(_FakeCatalog())
    assert _get(client).status_code == 200
    tenant, version, _ = cat.asked["runs"]
    assert (tenant, version) == (LEGACY_TENANT_ID, VER)


# --- a leg that could not answer carries no figures -----------------------


def test_a_stopped_graph_is_not_a_version_with_no_concepts(client, catalog):
    catalog(_FakeCatalog())
    body = _get(client).json()
    assert body["semantics"]["available"] is False
    assert "in_store" not in body["semantics"]
    assert "refused" in body["semantics"]["detail"]


def test_a_stopped_graph_names_the_url_it_tried(client, catalog):
    catalog(_FakeCatalog())
    body = _get(client).json()
    assert "bolt://" in body["structure"]["graph"]["detail"]


def test_a_stopped_qdrant_degrades_rather_than_failing_the_request(client, catalog):
    catalog(_FakeCatalog())
    r = _get(client)
    assert r.status_code == 200
    qdrant = r.json()["structure"]["qdrant"]
    assert qdrant["available"] is False
    assert "points" not in qdrant


def test_counts_agree_is_null_when_a_store_could_not_answer(client, catalog):
    """`None` is "could not compare", which is not the claim "they disagree"."""
    catalog(_FakeCatalog())
    assert _get(client).json()["structure"]["counts_agree"] is None


def test_a_version_nobody_measured_is_not_a_recall_of_zero(client, catalog):
    catalog(_FakeCatalog())
    retrieval = _get(client).json()["retrieval"]
    assert retrieval["available"] is False
    assert "scores" not in retrieval


def test_no_chunks_artifact_reports_why_rather_than_zero_spans(client, catalog):
    catalog(_FakeCatalog())
    artifacts = _get(client).json()["structure"]["artifacts"]
    assert artifacts["available"] is False
    assert "spans" not in artifacts
    assert "chunks.jsonl" in artifacts["detail"]


# --- the catalog leg ------------------------------------------------------


def test_the_page_count_is_reported_absent_rather_than_zero(client, catalog):
    """`document_version.page_count` is a column nothing writes:
    `register_version` runs before extraction. `null` is the measurement, and it
    starts working by itself the day something fills it."""
    catalog(_FakeCatalog())
    assert _get(client).json()["catalog"]["page_count"] is None


def test_a_profile_warning_says_whether_its_similarity_is_comparable(client, catalog):
    """`_topical_overlap` returns 0.0 by construction for a plain-text document,
    and 0.0 is documented as the *most dangerous* case — same structure,
    unrelated subject matter. A figure nobody can act on must not be printed as
    though it were measured."""
    catalog(
        _FakeCatalog(
            warnings=[{"id": 1, "profile_id": "p", "collides_with": "otro",
                       "similarity": 0.0, "detail": "…"}]
        )
    )
    warning = _get(client).json()["catalog"]["profile_warnings"][0]
    assert warning["comparable"] is False


# --- the ledger -----------------------------------------------------------


def _cost(run, stage, usd):
    return (run, Cost(stage=stage, provider="vertex", model="m",
                      input_tokens=1, output_tokens=1, usd=usd))


def test_a_version_bill_spans_every_run_that_touched_it(client, catalog):
    """The finding this leg exists for: on `ver_0cde…` the eval set was
    generated twice, the first time inside a run that was cancelled — $1.1965 of
    that document's $3.7572. Grouping by run answers only half of it."""
    cancelled = _run(id="run-a", state="cancelled")
    ok = _run(id="run-b", state="succeeded")
    catalog(
        _FakeCatalog(
            runs=[ok, cancelled],
            costs=[_cost("run-a", "evalset", 0.5753), _cost("run-b", "evalset", 0.5710)],
        )
    )
    ledger = _get(client).json()["ledger"]
    assert ledger["charged_in_more_than_one_run"] == ["evalset"]
    assert ledger["total_usd"] == pytest.approx(1.1463)
    assert ledger["usd_by_run_state"]["cancelled"] == pytest.approx(0.5753)


def test_an_unpriced_charge_is_counted_and_never_totalled_as_zero(client, catalog):
    """A missing price means the model id is absent from the table, which
    under-reports the bill rather than describing a free call."""
    catalog(
        _FakeCatalog(runs=[_run()], costs=[_cost(RUN, "semantics", None)])
    )
    ledger = _get(client).json()["ledger"]
    assert ledger["by_stage"]["semantics"]["unpriced_entries"] == 1
    assert ledger["total_usd"] == 0.0


# --- the graph legs, with the graph up ------------------------------------


def _graph_rows(**over):
    rows = {
        "version_counts": [
            {"chunks": 631, "sections": 56, "citations": 631, "claims": 3055}
        ],
        "version_chunk_kinds": [
            {"kind": "cuerpo", "chunks": 600},
            {"kind": "nota", "chunks": 31},
        ],
        "version_section_levels": [
            {"level": 1, "sections": 10},
            {"level": 2, "sections": 46},
        ],
        "version_claim_shape": [
            {"status": "afirma", "claims": 3000, "with_quote": 2960},
            {"status": "niega", "claims": 55, "with_quote": 53},
        ],
        "version_concepts_reached": [{"concepts": 2517}],
    }
    rows.update(over)
    return rows


@pytest.fixture
def graph_up(monkeypatch):
    def install(rows=None):
        monkeypatch.setattr(
            main, "_statistics_counts", lambda *a, **k: (rows or _graph_rows(), "")
        )

    return install


def test_the_chunk_kinds_stay_spanish_on_the_wire(client, catalog, graph_up):
    """They are stored in Qdrant payloads and used in filters; renaming them
    breaks every existing collection. The UI maps them to localised labels."""
    graph_up()
    catalog(_FakeCatalog())
    kinds = _get(client).json()["structure"]["graph"]["kinds"]
    assert kinds == {"cuerpo": 600, "nota": 31}


def test_a_claim_with_no_status_is_never_folded_into_afirma(client, catalog, graph_up):
    """A text expounding the doctrine it is about to rebut enunciates it in the
    same words as one who holds it, so `sin_estado` must keep its own key."""
    graph_up(
        _graph_rows(
            version_claim_shape=[
                {"status": "afirma", "claims": 10, "with_quote": 10},
                {"status": None, "claims": 3, "with_quote": 0},
            ]
        )
    )
    catalog(_FakeCatalog())
    by_status = _get(client).json()["semantics"]["in_store"]["by_status"]
    assert by_status == {"afirma": 10, "sin_estado": 3}


def test_the_claims_that_can_be_checked_are_counted_separately(client, catalog, graph_up):
    """A claim nobody can check must not look like one that can."""
    graph_up()
    catalog(_FakeCatalog())
    in_store = _get(client).json()["semantics"]["in_store"]
    assert (in_store["claims"], in_store["with_a_quote"]) == (3055, 3013)


def test_no_semantics_artifact_reports_the_comparison_absent_not_converged(
    client, catalog, graph_up
):
    """The graph's own counts stand. What is missing is the *comparison*, and
    saying so is not the same as reporting that nothing was left behind."""
    graph_up()
    catalog(_FakeCatalog())
    semantics = _get(client).json()["semantics"]
    assert semantics["available"] is True
    assert semantics["in_store"]["concepts"] == 2517
    assert semantics["diff"]["available"] is False
    assert "claims" not in semantics["diff"]
