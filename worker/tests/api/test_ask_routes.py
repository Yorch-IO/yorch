"""Asking is started by one request and collected by another.

What is under test is the handover, not the answering: the workflow is faked
throughout. The property that matters is that an answer computed after the
asking request has gone is still there to be collected — that is the whole
reason the route was split, and the failure it fixes threw away an answer the
API had already produced and already paid for.

**Two tests were removed here rather than ported**, and the reason belongs in
the record: they asserted that the in-process store was bounded at 64 entries
and reaped after an hour, and that a *running* question was never evicted to
make room. That store is gone. The state is the workflow's now, so what bounds
it is the namespace's retention rather than a cap this code enforces, and there
is nothing left for those tests to hold. The behaviour they protected — a
running question surviving until it is collected — is now structural.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker.answering.types import Answer  # noqa: E402
from brainworker.api import main  # noqa: E402
from brainworker.workflows.ask import AskOutcome  # noqa: E402


@dataclass
class _Handle:
    id: str
    outcome: AskOutcome | None

    async def query(self, _fn):
        if self.outcome is None:
            raise RuntimeError("workflow not found")
        return self.outcome

    async def describe(self):
        raise RuntimeError("not asked for on this route")


class _Client:
    """Just enough Temporal to exercise the two routes."""

    def __init__(self, outcome: AskOutcome | None):
        self.outcome = outcome
        self.started: list[object] = []
        self.memos: list[object] = []

    async def start_workflow(self, _run, question, *, id, task_queue, memo):  # noqa: A002
        self.started.append(question)
        # `memo` is keyword-only and required here on purpose. The paid plane
        # reads it to decide whether a caller may collect an answer at all, and
        # a question writes no catalog row to fall back on — so a start site
        # that stopped stamping it would open a cross-organisation read with
        # nothing failing. This double is what makes that a test failure.
        self.memos.append(memo)
        return _Handle(id=id, outcome=self.outcome)

    def get_workflow_handle(self, workflow_id: str):
        return _Handle(id=workflow_id, outcome=self.outcome)


@pytest.fixture
def configured(monkeypatch):
    class _Gemini:
        configured = True

    class _Settings:
        gemini = _Gemini()
        task_queue = "brain-ingest"

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    return monkeypatch


def with_outcome(monkeypatch, outcome: AskOutcome | None) -> _Client:
    client = _Client(outcome)

    async def _temporal():
        return client

    monkeypatch.setattr(main, "temporal", _temporal)
    return client


def start(api, text: str = "¿historia de la iglesia?") -> str:
    response = api.post("/ask", json={"library_id": "lib_a", "text": text})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "running"
    return body["question_id"]


def test_asking_returns_an_id_without_waiting(configured):
    # The asking request must not be the one that waits. It used to be, and a
    # question slower than the desktop client's timeout was lost because of it.
    fake = with_outcome(configured, AskOutcome(state="running"))
    api = TestClient(main.app)
    question_id = start(api)
    # The id *is* the workflow id — that is what makes the answer collectable by
    # either control plane rather than only by the process that started it.
    assert question_id.startswith("ask-")
    assert len(fake.started) == 1


def test_the_question_reaches_the_workflow_intact(configured):
    fake = with_outcome(configured, AskOutcome(state="running"))
    api = TestClient(main.app)
    api.post("/ask", json={"library_id": "lib_a", "text": "¿y el arrianismo?", "top_k": 3})
    question = fake.started[0]
    assert question.library_id == "lib_a"
    assert question.text == "¿y el arrianismo?"
    assert question.top_k == 3


def test_a_question_still_running_reports_running(configured):
    with_outcome(configured, AskOutcome(state="running"))
    api = TestClient(main.app)
    body = api.get(f"/ask/{start(api)}").json()
    assert body["state"] == "running"
    assert body["answer"] is None and body["error"] is None


def test_the_answer_is_collected_by_polling(configured):
    answer = Answer(state="answered", text="la respuesta", reason="")
    with_outcome(configured, AskOutcome(state="done", answer=answer))
    api = TestClient(main.app)
    body = api.get(f"/ask/{start(api)}").json()
    assert body["state"] == "done"
    assert body["answer"]["text"] == "la respuesta"
    # `grounded` is a property and therefore absent from the payload. The Rust
    # struct does not declare it either; adding it would be a contract change.
    assert "grounded" not in body["answer"]


def test_an_answer_survives_being_collected_twice(configured):
    """Collecting is a read. A UI that re-polls after a reload must not lose it."""
    answer = Answer(state="answered", text="la respuesta", reason="")
    with_outcome(configured, AskOutcome(state="done", answer=answer))
    api = TestClient(main.app)
    question_id = start(api)
    assert api.get(f"/ask/{question_id}").json() == api.get(f"/ask/{question_id}").json()


def test_a_failure_is_reported_rather_than_lost(configured):
    """A failed question is a 200 carrying `state: failed`, not an HTTP error.

    The caller asked a question and deserves an answer about it either way, and
    the `kind` is what the desktop app keys its advice on.
    """
    with_outcome(
        configured,
        AskOutcome(state="failed", error={"kind": "provider_quota", "message": "sin cuota"}),
    )
    api = TestClient(main.app)
    response = api.get(f"/ask/{start(api)}")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "failed"
    assert body["error"]["kind"] == "provider_quota"
    assert body["answer"] is None


def test_an_unknown_id_says_so_with_a_kind(configured):
    with_outcome(configured, None)
    api = TestClient(main.app)
    response = api.get("/ask/ask-0000000000000-deadbeef")
    assert response.status_code == 404
    assert response.json()["detail"]["kind"] == "question_not_found"


def test_asking_without_a_provider_refuses_before_starting(monkeypatch):
    """Refused in the request that asked, rather than three stages into a run."""

    class _Gemini:
        configured = False

    class _Settings:
        gemini = _Gemini()
        task_queue = "brain-ingest"

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    fake = with_outcome(monkeypatch, AskOutcome(state="running"))
    api = TestClient(main.app)
    response = api.post("/ask", json={"library_id": "lib_a", "text": "¿?"})
    assert response.status_code == 503
    assert response.json()["detail"]["kind"] == "provider_unconfigured"
    assert fake.started == []
