"""The second gate's own route, and the panel it stops lying to.

`IngestWorkflow._report` is assigned once, before the *first* gate, and never
cleared — so `/runs/{id}/gate` keeps serving the pre-correction preview and
estimate for the rest of the run. At the second gate that is not merely stale:
seen in the real window on a run parked at `awaiting_correction_review` with
**$0.5834 of correction already billed**, under a panel reading "nothing has
been paid for yet". The query that should have been shown instead had existed
since the second gate did and had **zero callers** anywhere.

Temporal is faked. What is under test is the routing decision — that the two
gates answer from two different places, so one cannot serve the other's numbers.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker.api import main  # noqa: E402
from brainworker.artifacts import ArtifactRef  # noqa: E402
from brainworker.pipeline import Correction, Spend  # noqa: E402

REF = ArtifactRef(kind="corrected_text", path="runs/r/corrected.txt",
                  sha256="a" * 64, bytes=10)
DONE = Correction(
    text=REF,
    report=ArtifactRef(kind="correction_report",
                       path="runs/r/correction-report.json",
                       sha256="b" * 64, bytes=10),
    paragraphs=107, changed=85, rejected=22, missing=0, cache_hits=85,
    spend=Spend(stage="correction", model="gemini-3.6-flash",
                input_tokens=1000, output_tokens=2000, usd=0.5834),
)


class FakeHandle:
    """Answers each query by name, the way a real handle dispatches on the
    method object — so a route querying the *wrong* one reads the wrong field
    here too rather than silently getting the right answer."""

    def __init__(self, *, correction=None, report=None, stage="correcting",
                 missing=False):
        self._by_name = {"correction": correction, "gate_report": report,
                         "stage": stage}
        self._missing = missing

    async def query(self, method, *a, **kw):
        if self._missing:
            raise RuntimeError("workflow not found")
        return self._by_name.get(getattr(method, "__name__", str(method)))

    async def describe(self):
        raise RuntimeError("no description")


@pytest.fixture
def handle(monkeypatch):
    holder: dict = {}

    class FakeClient:
        def get_workflow_handle(self, wid):
            return holder["handle"]

    async def _temporal():
        return FakeClient()

    monkeypatch.setattr(main, "temporal", _temporal)
    return holder


@pytest.fixture
def client():
    return TestClient(main.app)


def test_the_correction_route_reports_what_correction_did(handle, client):
    handle["handle"] = FakeHandle(correction=DONE)
    r = client.get("/runs/ingest-1/correction")
    assert r.status_code == 200
    body = r.json()
    assert body["paragraphs"] == 107
    assert body["changed"] == 85
    assert body["rejected"] == 22
    assert body["cache_hits"] == 85
    assert body["spend"]["usd"] == pytest.approx(0.5834)


def test_it_answers_from_the_correction_and_never_from_the_gate_report(handle, client):
    """The defect, pinned. A run at the second gate holds *both*: a stale
    `GateReport` from before any money was spent, and the correction that spent
    it. A route reading the first is what told a person nothing had been paid
    for over a bill of $0.58."""
    handle["handle"] = FakeHandle(correction=DONE, report={"estimate": "stale"})
    body = client.get("/runs/ingest-1/correction").json()
    assert "estimate" not in body
    assert body["changed"] == 85


def test_a_run_that_has_not_corrected_yet_says_keep_waiting(handle, client):
    """409 and not 404, in the shape `/gate` already uses: the Import screen
    polls on an interval and reads a 409 as "keep waiting". `run_state` rides
    along for the same recorded reason — a run that died before correcting
    would otherwise answer `None` forever and spin the screen."""
    handle["handle"] = FakeHandle(correction=None, stage="correcting")
    r = client.get("/runs/ingest-1/correction")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["kind"] == "correction_not_ready"
    assert detail["stage"] == "correcting"
    assert "run_state" in detail


def test_an_unknown_run_is_a_404_with_a_kind(handle, client):
    handle["handle"] = FakeHandle(missing=True)
    r = client.get("/runs/nope/correction")
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "run_not_found"
