"""Asking is started by one request and collected by another.

What is under test is the handover, not the answering: `ask` itself is replaced
throughout. The property that matters is that an answer computed after the
asking request has gone is still there to be collected — that is the whole
reason the route was split, and the failure it fixes threw away an answer the
API had already produced and already paid for.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker.api import main  # noqa: E402
from brainworker.answering.types import Answer  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    """A configured provider and an empty question store."""

    class _Gemini:
        configured = True

    class _Settings:
        gemini = _Gemini()

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    main._ASKED.clear()
    yield TestClient(main.app)
    main._ASKED.clear()


def answered() -> Answer:
    return Answer(state="answered", text="la respuesta", reason="")


def start(client, text: str = "¿historia de la iglesia?") -> str:
    response = client.post("/ask", json={"library_id": "lib_a", "text": text})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "running"
    return body["question_id"]


def test_asking_returns_an_id_without_waiting(client, monkeypatch):
    # The asking request must not be the one that waits. It used to be, and a
    # question slower than the desktop client's timeout was lost because of it.
    monkeypatch.setattr(main, "ask", lambda s, q: answered())
    question_id = start(client)
    assert question_id.startswith("q_")


def test_the_answer_is_collected_by_polling(client, monkeypatch):
    monkeypatch.setattr(main, "ask", lambda s, q: answered())
    question_id = start(client)

    body = client.get(f"/ask/{question_id}").json()
    assert body["state"] == "done"
    assert body["answer"]["text"] == "la respuesta"
    assert body["error"] is None


def test_an_answer_survives_being_collected_twice(client, monkeypatch):
    # The window can be closed and reopened; collecting must not consume.
    monkeypatch.setattr(main, "ask", lambda s, q: answered())
    question_id = start(client)

    assert client.get(f"/ask/{question_id}").json()["state"] == "done"
    assert client.get(f"/ask/{question_id}").json()["answer"]["text"] == "la respuesta"


def test_a_question_still_running_reports_running(client, monkeypatch):
    release = threading.Event()

    def slow(s, q):
        release.wait(5)
        return answered()

    monkeypatch.setattr(main, "ask", slow)
    question_id = start(client)
    try:
        assert client.get(f"/ask/{question_id}").json()["state"] == "running"
    finally:
        release.set()


def test_a_failure_is_reported_rather_than_lost(client, monkeypatch):
    def boom(s, q):
        raise RuntimeError("memgraph is down")

    monkeypatch.setattr(main, "ask", boom)
    question_id = start(client)

    body = client.get(f"/ask/{question_id}").json()
    assert body["state"] == "failed"
    assert body["answer"] is None
    assert "memgraph is down" in body["error"]["message"]


def test_an_unknown_id_says_so_with_a_kind(client):
    # The UI offers a fix from the kind, not from the message: this one means
    # the API restarted and the question has to be asked again.
    response = client.get("/ask/q_" + "0" * 32)
    assert response.status_code == 404
    assert response.json()["detail"]["kind"] == "question_not_found"


def test_asking_without_a_provider_refuses_before_starting(client, monkeypatch):
    class _Gemini:
        configured = False

    class _Settings:
        gemini = _Gemini()

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    response = client.post("/ask", json={"library_id": "lib_a", "text": "¿?"})

    assert response.status_code == 503
    assert response.json()["detail"]["kind"] == "provider_unconfigured"
    assert not main._ASKED


def test_the_store_is_bounded_and_never_evicts_a_running_question(monkeypatch):
    main._ASKED.clear()
    main._ASKED["q_running"] = main._Asked(state="running", started=0.0)
    for i in range(main._ASKED_LIMIT + 5):
        main._ASKED[f"q_{i}"] = main._Asked(state="done", started=0.0)

    main._reap(now=0.0)

    assert len(main._ASKED) <= main._ASKED_LIMIT
    assert "q_running" in main._ASKED
    main._ASKED.clear()


def test_finished_questions_are_reaped_once_they_are_old(monkeypatch):
    main._ASKED.clear()
    main._ASKED["q_old"] = main._Asked(state="done", started=0.0)
    main._ASKED["q_slow"] = main._Asked(state="running", started=0.0)

    main._reap(now=main._ASKED_TTL + 1)

    assert "q_old" not in main._ASKED
    # Still running, however long it has been. Reaping it would turn a slow
    # answer into a 404 telling the user to ask again — and pay again.
    assert "q_slow" in main._ASKED
    main._ASKED.clear()
