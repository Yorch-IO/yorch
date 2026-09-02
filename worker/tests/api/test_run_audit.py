"""The queue, the durable ledger, and the raw history.

These three routes exist because `GET /runs/{id}` answers from Temporal, which
forgets a run when retention expires — at which point it reports `state: null`
and `stage: null` for a run whose every column is still in Postgres. The
properties under test are mostly about *not asserting more than is known*: a
free stage is `null` rather than `$0`, an aged-out history is `available: false`
rather than `[]`, and a charge no stage claims is still in the totals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker import auditlog  # noqa: E402
from brainworker.api import main  # noqa: E402
from brainworker.catalog.repo import Cost, RunEvent, RunSummary  # noqa: E402

T0 = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
RUN = "ingest-1787000000000-abcdef01"


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _run(**kw) -> RunSummary:
    base = dict(
        id=RUN, workflow_id=RUN, kind="index", state="succeeded", stage="done",
        started_at=T0, finished_at=_at(600), error_kind=None, error_detail=None,
        title="Institución", library_id="lib_teologia",
        document_id="doc_1", version_id="ver_1", usd_so_far=0.05,
    )
    base.update(kw)
    return RunSummary(**base)


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    class _Settings:
        database_url = "postgresql://nowhere/none"
        workspace = "/workspace"

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    return _Settings


class _FakeCatalog:
    """Only what these routes call. A real `Catalog` needs Postgres."""

    def __init__(self, run=None, events=(), costs=(), artifacts=(), warnings=(),
                 runs=()):
        self._run, self._events, self._costs = run, list(events), list(costs)
        self._artifacts, self._warnings, self._runs = (
            list(artifacts), list(warnings), list(runs)
        )
        self.asked = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def run(self, run_id, *, tenant_id):
        self.asked["run"] = tenant_id
        return self._run

    def run_events(self, run_id, *, tenant_id):
        self.asked["events"] = tenant_id
        return self._events

    def costs(self, run_id, *, tenant_id):
        self.asked["costs"] = tenant_id
        return self._costs

    def artifacts(self, run_id):
        return self._artifacts

    def open_profile_warnings(self, version_id):
        return self._warnings

    def runs(self, **kw):
        self.asked.update(kw)
        return self._runs


def _catalog(monkeypatch, catalog):
    monkeypatch.setattr(main, "Catalog", lambda *_, **__: catalog)


# -- the ledger, as a pure function ----------------------------------------


def test_a_stage_that_spends_nothing_reports_null_rather_than_zero():
    """"This stage does not spend" and "this stage's charge was not recorded"
    are different claims, and a zero renders as the first while sometimes
    meaning the second. The same rule `/runs/{id}` follows by omitting
    `semantics` rather than zeroing it."""
    ledger = auditlog.build(
        _run(),
        [RunEvent(1, T0, "chunking", None, None),
         RunEvent(2, _at(5), "done", "succeeded", None)],
        [], [],
    )
    assert ledger["stages"][0]["cost"] is None
    assert ledger["stages"][0]["seconds"] == 5.0


def test_a_charge_no_stage_claims_still_reaches_the_totals():
    """A ledger that dropped one would be a bill that does not add up.

    The three a question makes belong to no pipeline stage — asking has no gate
    and therefore no pipeline to name — so they land in a trailing group rather
    than being lost.
    """
    costs = [
        Cost("correction", "vertex", "gemini-3.6-flash", 1000, 500, 0.0334),
        Cost("answering", "vertex", "gemini-3.6-flash", 200, 100, 0.0102),
    ]
    ledger = auditlog.build(
        _run(),
        [RunEvent(1, T0, "correcting", None, None),
         RunEvent(2, _at(9), "done", "succeeded", None)],
        costs, [],
    )
    assert ledger["totals"]["usd"] == pytest.approx(0.0436)
    assert ledger["stages"][0]["stage"] == "correcting"
    assert ledger["stages"][0]["cost"]["usd"] == pytest.approx(0.0334)
    orphan = ledger["stages"][-1]
    assert orphan["stage"] is None
    assert orphan["cost"]["usd"] == pytest.approx(0.0102)


def test_an_unpriced_charge_is_counted_but_never_folded_in_as_zero():
    ledger = auditlog.build(
        _run(),
        [RunEvent(1, T0, "embedding", None, None)],
        [Cost("embedding", "vertex", "unreleased", 100, 0, None)],
        [],
    )
    assert ledger["totals"]["usd"] is None
    assert ledger["totals"]["unpriced_entries"] == 1


def test_the_last_row_is_the_outcome_and_names_the_stage_it_died_in():
    """"It failed" is half an answer; "it failed in `semantics`" is the whole
    one, and it is the half `run.state` cannot hold."""
    ledger = auditlog.build(
        _run(state="failed", stage="semantics", error_kind="activity_failed"),
        [RunEvent(1, T0, "semantics", None, None),
         RunEvent(2, _at(300), "semantics", "failed", "activity_failed")],
        [], [],
    )
    last = ledger["stages"][-1]
    assert (last["stage"], last["outcome"]) == ("semantics", "failed")
    assert ledger["stages"][0]["seconds"] == 300.0
    # The terminal row is an instant, not a stage with a length.
    assert last["seconds"] is None


def test_an_artifact_is_shown_against_the_stage_that_wrote_it():
    ledger = auditlog.build(
        _run(),
        [RunEvent(1, T0, "correcting", None, None),
         RunEvent(2, _at(1), "chunking", None, None)],
        [],
        [{"name": "corrected_text", "rel_path": "runs/x/corrected.txt",
          "sha256": "a" * 64, "size_bytes": 10},
         {"name": "chunks", "rel_path": "runs/x/chunks.jsonl",
          "sha256": "b" * 64, "size_bytes": 20}],
    )
    assert [a["name"] for a in ledger["stages"][0]["artifacts"]] == ["corrected_text"]
    assert [a["name"] for a in ledger["stages"][1]["artifacts"]] == ["chunks"]


def test_a_still_running_stage_has_no_end_and_no_outcome():
    ledger = auditlog.build(
        _run(state="running", stage="semantics", finished_at=None),
        [RunEvent(1, T0, "semantics", None, None)],
        [], [],
    )
    row = ledger["stages"][-1]
    assert row["ended_at"] is None and row["outcome"] is None


# -- the routes -------------------------------------------------------------


def test_the_audit_route_asks_the_catalog_as_one_organisation(client, monkeypatch):
    """Every reader takes a required tenant, and the route names it. An id is not
    authorization: a member of one organisation holding the same file as another
    can recompute a salted id, so only a predicate refuses a read."""
    from brainworker.graph.schema import LEGACY_TENANT_ID

    catalog = _FakeCatalog(run=_run(), events=[RunEvent(1, T0, "done", "succeeded", None)])
    _catalog(monkeypatch, catalog)
    body = client.get(f"/runs/{RUN}/audit").json()
    assert body["run"]["kind"] == "index"
    assert set(catalog.asked.values()) == {LEGACY_TENANT_ID}


def test_a_run_this_organisation_does_not_own_is_a_404_not_an_empty_ledger(
    client, monkeypatch
):
    _catalog(monkeypatch, _FakeCatalog(run=None))
    response = client.get(f"/runs/{RUN}/audit")
    assert response.status_code == 404
    assert response.json()["detail"]["kind"] == "run_not_found"


def test_the_queue_pages_by_a_cursor_and_says_when_there_is_more(client, monkeypatch):
    """Keyset, not OFFSET: runs are appended at the top continuously, and an
    OFFSET shifts under a list being appended to — which shows a row twice or
    skips one."""
    rows = [_run(id=f"r{i}", workflow_id=f"r{i}", started_at=_at(-i)) for i in range(4)]
    catalog = _FakeCatalog(runs=rows)
    _catalog(monkeypatch, catalog)

    body = client.get("/runs?limit=3").json()
    assert len(body["runs"]) == 3
    # One more than asked for is fetched, so "is there another page" costs no
    # count over the whole table.
    assert catalog.asked["limit"] == 4
    assert body["next_before"].endswith("|r2")

    catalog._runs = rows[:2]
    assert client.get("/runs?limit=3").json()["next_before"] is None


def test_an_unparseable_cursor_returns_the_first_page_rather_than_a_400(
    client, monkeypatch
):
    """A recoverable answer beats a refusal on a string the caller did not build."""
    catalog = _FakeCatalog(runs=[])
    _catalog(monkeypatch, catalog)
    assert client.get("/runs?before=not-a-timestamp").status_code == 200
    assert catalog.asked["before"] is None


def test_the_queue_passes_its_filters_through(client, monkeypatch):
    catalog = _FakeCatalog(runs=[])
    _catalog(monkeypatch, catalog)
    client.get("/runs?kinds=index,reindex&states=running&library_id=lib_1")
    assert catalog.asked["kinds"] == ["index", "reindex"]
    assert catalog.asked["states"] == ["running"]
    assert catalog.asked["library_id"] == "lib_1"


def test_a_history_temporal_has_forgotten_is_unavailable_not_empty(
    client, monkeypatch
):
    """"The history has aged out" and "this run did nothing" must not render the
    same. `/project-summary` already makes exactly this distinction with its own
    `available` flags, and for the same reason: a zero is a claim, and it sends a
    reader to look for a broken extractor instead of an expired retention."""

    class _Handle:
        def fetch_history_events(self):
            raise RuntimeError("workflow execution not found")

    class _Client:
        def get_workflow_handle(self, _):
            return _Handle()

    async def _temporal():
        return _Client()

    monkeypatch.setattr(main, "temporal", _temporal)
    body = client.get(f"/runs/{RUN}/events").json()
    assert body == {"available": False, "truncated": False, "events": []}


def test_a_run_that_predates_the_trail_still_accounts_for_its_charges():
    """Found against a real run, not written from the design.

    Every run indexed before `run_event` existed has costs and no events, so
    there is nowhere for a charge to attach — and it landed in `totals` and in no
    row at all. That is exactly the "bill that does not add up" the trailing
    group exists to prevent, arrived at from a direction the group did not cover:
    the charge *did* map to a stage, and the stage simply never appeared.
    """
    ledger = auditlog.build(
        _run(),
        [],  # a run older than the trail
        [
            Cost("embedding", "vertex", "gemini-embedding-2", 3000, 0, 0.00045),
            Cost("evaluation", "vertex", "gemini-3.6-flash", 900, 400, 0.0021),
        ],
        [{"name": "chunks", "rel_path": "runs/x/chunks.jsonl",
          "sha256": "a" * 64, "size_bytes": 10}],
    )
    assert len(ledger["stages"]) == 1
    orphan = ledger["stages"][0]
    assert orphan["stage"] is None
    assert orphan["cost"]["usd"] == pytest.approx(0.00255)
    assert [a["name"] for a in orphan["artifacts"]] == ["chunks"]
    # The invariant this function claims: what the rows account for is what the
    # run was billed.
    assert orphan["cost"]["usd"] == pytest.approx(ledger["totals"]["usd"])


def test_the_total_is_a_number_the_wire_can_carry():
    """`cost_entry.usd` is `numeric(12, 6)`, so psycopg hands back a `Decimal`.

    Serialising one row at a time hid it — that is why it sat in the defect list
    rather than being fixed. Summing them does not: the real total came out of
    the route as the JSON **string** `"0.000000"`, which the Rust client's
    `Option<f64>` refuses outright, so the audit view would have failed to load
    for every run that had ever been billed.
    """
    from decimal import Decimal

    ledger = auditlog.build(
        _run(),
        [RunEvent(1, T0, "embedding", None, None)],
        # What the reader used to hand back, before the cast moved into SQL.
        [Cost("embedding", "vertex", "m", 10, 0, Decimal("0.000450"))],  # type: ignore[arg-type]
        [],
    )
    # Sums cleanly and stays comparable against a float, which is the property
    # the annotation always claimed and did not have.
    assert float(ledger["totals"]["usd"]) == pytest.approx(0.00045)
