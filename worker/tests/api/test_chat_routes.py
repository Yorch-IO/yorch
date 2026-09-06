"""The conversation routes, against a faked catalog and a faked Temporal.

What is worth asserting here is the shape of the contract two planes have to
agree on, and the two behaviours that only exist at this layer: the turn number
being claimed before the signal is sent, and the stream ending on the turn's own
row rather than on the relay going quiet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from brainworker.api import main
from brainworker.chat.types import MAX_MESSAGE_CHARS, WINDOW_TURNS
from brainworker.graph.schema import LEGACY_TENANT_ID

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


@dataclass
class Row:
    id: str = "cnv_1"
    library_id: str = "lib_teologia"
    title: str = "Primera"
    title_generated: bool = False
    created_at: datetime = NOW
    last_message_at: datetime = NOW
    turns: int = 0


@dataclass
class Turn:
    seq: int = 1
    question: str = "¿Quién fue Jesucristo?"
    searched: str | None = None
    answer: str = ""
    state: str = "running"
    effort: str = "standard"
    style_effort: str | None = None
    citations: list = field(default_factory=list)
    cited_evidence: list = field(default_factory=list)
    error: dict | None = None
    asked_at: datetime = NOW
    answered_at: datetime | None = None


class _FakeCatalog:
    """Only what these routes call."""

    def __init__(self) -> None:
        self.rows: dict[str, Row] = {}
        self.turn_rows: list[Turn] = []
        self.deltas_by_turn: dict[int, list[tuple[int, str]]] = {}
        self.opened: list[tuple[str, str, str]] = []
        self.titles: list[tuple[str, bool]] = []
        self.next_seq = 1
        self.deleted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def start_conversation(self, cid, *, tenant_id, library_id, title):
        self.rows[cid] = Row(id=cid, library_id=library_id, title=title)

    def conversations(self, *, tenant_id, limit=50):
        assert tenant_id == LEGACY_TENANT_ID
        return list(self.rows.values())[:limit]

    def conversation(self, cid, *, tenant_id):
        assert tenant_id == LEGACY_TENANT_ID
        return self.rows.get(cid)

    def turns(self, cid, *, tenant_id, limit=None):
        rows = [t for t in self.turn_rows]
        return rows[-limit:] if limit else rows

    def open_turn(self, cid, *, tenant_id, question, effort):
        if cid not in self.rows:
            return None
        self.opened.append((cid, question, effort))
        seq, self.next_seq = self.next_seq, self.next_seq + 1
        self.turn_rows.append(Turn(seq=seq, question=question, effort=effort))
        return seq

    def set_conversation_title(self, cid, title, *, tenant_id, generated=True):
        self.titles.append((title, generated))
        self.rows[cid].title = title

    def delete_conversation(self, cid, *, tenant_id):
        self.deleted.append(cid)
        return self.rows.pop(cid, None) is not None

    def relay(self, cid, turn_seq, *, tenant_id, since=0):
        return [
            {"chunk_seq": n, "kind": kind, "text": text, "detail": detail}
            for (n, kind, text, detail) in self.deltas_by_turn.get(turn_seq, [])
            if n > since
        ]


class _FakeHandle:
    id = "chat-cnv_1"


class _FakeClient:
    def __init__(self) -> None:
        self.started: list[dict] = []

    async def start_workflow(self, _run, arg, **kw):
        self.started.append({"start": arg, **kw})
        return _FakeHandle()


@pytest.fixture
def catalog(monkeypatch):
    fake = _FakeCatalog()
    monkeypatch.setattr(main, "Catalog", lambda *_, **__: fake)
    return fake


@pytest.fixture
def temporal(monkeypatch):
    fake = _FakeClient()

    async def _temporal():
        return fake

    monkeypatch.setattr(main, "temporal", _temporal)
    return fake


@pytest.fixture
def client(monkeypatch):
    class _G:
        configured = True

    class _S:
        gemini = _G()
        database_url = "postgresql://x/y"
        task_queue = "brain"

    monkeypatch.setattr(main, "settings", lambda: _S())
    return TestClient(main.app)


# -- the record -------------------------------------------------------------


def test_a_conversation_must_be_created_before_a_turn_can_be_signalled(
    client, catalog, temporal
):
    """The turn and delta tables derive their tenant from this row, so a turn
    naming a conversation that does not exist would insert nothing at all."""
    got = client.post("/chat/cnv_ausente/turn", json={"text": "¿?"})
    assert got.status_code == 404
    assert got.json()["detail"]["kind"] == "conversation_not_found"
    assert temporal.started == [], "a workflow was started for nothing"


def test_creating_returns_an_id_the_client_can_use(client, catalog):
    body = client.post("/chat", json={"library_id": "lib_teologia"}).json()
    assert body["conversation_id"].startswith("cnv_")
    assert body["library_id"] == "lib_teologia"


def test_the_transcript_comes_from_the_catalog_not_from_temporal(client, catalog):
    """Which is what makes a conversation outlive Temporal's retention."""
    catalog.start_conversation(
        "cnv_1", tenant_id=LEGACY_TENANT_ID, library_id="lib_a", title="T"
    )
    catalog.turn_rows.append(
        Turn(seq=1, answer="Fue…", state="answered", searched="¿Quién fue Jesucristo?")
    )
    body = client.get("/chat/cnv_1").json()
    assert body["title"] == "T"
    assert [t["seq"] for t in body["turns_detail"]] == [1]
    assert body["turns_detail"][0]["searched"] == "¿Quién fue Jesucristo?"


def test_an_unknown_conversation_is_a_kind_the_client_can_key_on(client, catalog):
    got = client.get("/chat/cnv_ausente")
    assert got.status_code == 404
    assert got.json()["detail"]["kind"] == "conversation_not_found"


def test_deleting_reports_a_missing_one_rather_than_pretending(client, catalog):
    assert client.delete("/chat/cnv_ausente").status_code == 404


# -- adding a turn ----------------------------------------------------------


def _open(catalog, **kw):
    catalog.start_conversation(
        "cnv_1", tenant_id=LEGACY_TENANT_ID, library_id="lib_teologia", title=""
    )
    for k, v in kw.items():
        setattr(catalog.rows["cnv_1"], k, v)


def test_the_turn_number_is_claimed_before_the_signal_is_sent(
    client, catalog, temporal
):
    """It is the workflow's idempotency key, so it has to exist first."""
    _open(catalog)
    body = client.post("/chat/cnv_1/turn", json={"text": "¿Quién fue Jesucristo?"}).json()
    assert body["turn_seq"] == 1
    started = temporal.started[0]
    assert started["start_signal"] == "ask"
    assert started["start_signal_args"][0].turn_seq == 1


def test_the_signal_carries_the_tenant_and_the_library_from_the_server(
    client, catalog, temporal
):
    """Never from the request body. A model — or a client — must not be able to
    name either."""
    _open(catalog)
    client.post("/chat/cnv_1/turn", json={"text": "¿?", "library_id": "otra"})
    turn = temporal.started[0]["start_signal_args"][0]
    assert turn.tenant_id == LEGACY_TENANT_ID
    assert turn.library_id == "lib_teologia"


def test_the_workflow_id_is_the_conversation_so_a_second_turn_signals_the_first(
    client, catalog, temporal
):
    _open(catalog)
    client.post("/chat/cnv_1/turn", json={"text": "una"})
    client.post("/chat/cnv_1/turn", json={"text": "dos"})
    assert {s["id"] for s in temporal.started} == {"chat-cnv_1"}
    assert [s["start_signal_args"][0].turn_seq for s in temporal.started] == [1, 2]


def test_a_resumed_session_is_seeded_with_the_window_from_the_catalog(
    client, catalog, temporal
):
    """A conversation whose workflow aged out is restarted from the record — the
    whole reason the record is not in Temporal."""
    _open(catalog, turns=3)
    for i in range(1, 4):
        catalog.turn_rows.append(
            Turn(seq=i, question=f"q{i}", answer=f"a{i}", state="answered")
        )
    catalog.next_seq = 4
    client.post("/chat/cnv_1/turn", json={"text": "¿y su muerte?"})
    start = temporal.started[0]["start"]
    assert [w.question for w in start.window] == ["q1", "q2", "q3"]
    assert start.answered == 3


def test_a_turn_still_running_is_not_offered_to_the_rewrite(
    client, catalog, temporal
):
    """It has no answer to resolve a pronoun against."""
    _open(catalog, turns=1)
    catalog.turn_rows.append(Turn(seq=1, question="q1", answer="", state="running"))
    catalog.next_seq = 2
    client.post("/chat/cnv_1/turn", json={"text": "¿y?"})
    assert temporal.started[0]["start"].window == []


def test_the_window_handed_to_a_new_session_is_bounded(client, catalog, temporal):
    _open(catalog, turns=30)
    for i in range(1, 31):
        catalog.turn_rows.append(
            Turn(seq=i, question=f"q{i}", answer=f"a{i}", state="answered")
        )
    catalog.next_seq = 31
    client.post("/chat/cnv_1/turn", json={"text": "otra"})
    assert len(temporal.started[0]["start"].window) == WINDOW_TURNS


def test_the_first_question_names_the_conversation_provisionally(
    client, catalog, temporal
):
    """`generated=False`, or the model would never get to name it."""
    _open(catalog)
    client.post("/chat/cnv_1/turn", json={"text": "¿Quién fue Jesucristo?"})
    assert catalog.titles == [("¿Quién fue Jesucristo?", False)]


def test_a_later_turn_does_not_rename_the_conversation(client, catalog, temporal):
    _open(catalog)
    client.post("/chat/cnv_1/turn", json={"text": "una"})
    catalog.titles.clear()
    client.post("/chat/cnv_1/turn", json={"text": "dos"})
    assert catalog.titles == []


def test_an_oversized_message_is_refused_by_the_field_not_by_hand(
    client, catalog, temporal
):
    """422-with-a-list, which is what the paid plane's class-validator failures
    render as. A hand-raised 400 here would make the two planes answer one bad
    request two different ways."""
    _open(catalog)
    got = client.post("/chat/cnv_1/turn", json={"text": "x" * (MAX_MESSAGE_CHARS + 1)})
    assert got.status_code == 422
    assert isinstance(got.json()["detail"], list)


def test_an_unknown_effort_level_is_refused_the_same_way(client, catalog, temporal):
    _open(catalog)
    got = client.post("/chat/cnv_1/turn", json={"text": "¿?", "effort": "exhaustivo"})
    assert got.status_code == 422
    assert isinstance(got.json()["detail"], list)


def test_a_turn_is_refused_when_no_project_is_configured(client, catalog, monkeypatch):
    class _G:
        configured = False

    class _S:
        gemini = _G()
        database_url = "postgresql://x/y"
        task_queue = "brain"

    monkeypatch.setattr(main, "settings", lambda: _S())
    _open(catalog)
    got = client.post("/chat/cnv_1/turn", json={"text": "¿?"})
    assert got.status_code == 503
    assert got.json()["detail"]["kind"] == "provider_unconfigured"


# -- the stream -------------------------------------------------------------


def _events(raw: str) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in raw.splitlines()
        if line.startswith("data: ")
    ]


def test_the_stream_replays_what_was_already_written_then_ends_on_the_turn(
    client, catalog
):
    _open(catalog)
    catalog.turn_rows.append(
        Turn(seq=1, answer="La gente es feliz", state="answered",
             citations=[{"chunk_id": "chk_1"}])
    )
    catalog.deltas_by_turn[1] = [(1, "token", "La gente ", None), (2, "token", "es feliz", None)]
    events = _events(client.get("/chat/cnv_1/turn/1/stream").text)
    assert [e["type"] for e in events] == ["token", "token", "done"]
    assert "".join(e["text"] for e in events if e["type"] == "token") == "La gente es feliz"
    assert events[-1]["turn"]["state"] == "answered"


def test_since_is_the_resume_point_that_makes_a_reconnect_free(client, catalog):
    _open(catalog)
    catalog.turn_rows.append(Turn(seq=1, answer="ab", state="answered"))
    catalog.deltas_by_turn[1] = [(1, "token", "a", None), (2, "token", "b", None)]
    events = _events(client.get("/chat/cnv_1/turn/1/stream?since=1").text)
    assert [e["text"] for e in events if e["type"] == "token"] == ["b"]


def test_the_done_event_is_authoritative_when_the_draft_was_withdrawn(client, catalog):
    """A turn can stream fluent prose and still be refused: `citas` arrives last
    in the envelope, so verification cannot run until it closes. The client has
    to replace what it showed, and this is the event that tells it to."""
    _open(catalog)
    catalog.turn_rows.append(
        Turn(seq=1, answer="", state="insufficient_evidence",
             error=None, citations=[])
    )
    catalog.deltas_by_turn[1] = [(1, "token", "La felicidad procede de la virtud", None)]
    events = _events(client.get("/chat/cnv_1/turn/1/stream").text)
    assert events[0]["text"] == "La felicidad procede de la virtud"
    assert events[-1]["type"] == "done"
    assert events[-1]["turn"]["state"] == "insufficient_evidence"
    assert events[-1]["turn"]["answer"] == ""


def test_a_failed_turn_ends_the_stream_rather_than_hanging(client, catalog):
    _open(catalog)
    catalog.turn_rows.append(
        Turn(seq=1, state="failed", error={"kind": "provider_quota", "message": "sin cuota"})
    )
    events = _events(client.get("/chat/cnv_1/turn/1/stream").text)
    assert events[-1]["type"] == "done"
    assert events[-1]["turn"]["error"]["kind"] == "provider_quota"


def test_the_stream_announces_its_media_type_and_disables_buffering(client, catalog):
    _open(catalog)
    catalog.turn_rows.append(Turn(seq=1, state="answered"))
    got = client.get("/chat/cnv_1/turn/1/stream")
    assert got.headers["content-type"].startswith("text/event-stream")
    assert got.headers["cache-control"] == "no-cache"
    assert got.headers["x-accel-buffering"] == "no"


# -- stage events -----------------------------------------------------------
#
# The half of this feature the latency measurement argued for: streamed prose
# covers about 8% of a turn's wait, and the rest was one unchanging line over
# four things that can each take seconds.


def test_stages_and_prose_arrive_in_one_ordered_stream(client, catalog):
    """One sequence, two kinds. A stage row placed in `chunk_seq` order
    interleaves with the prose for free, and the route still makes one read."""
    _open(catalog)
    catalog.turn_rows.append(Turn(seq=1, answer="hola", state="answered"))
    catalog.deltas_by_turn[1] = [
        (1, "rewriting", "", None),
        (2, "planning", "", None),
        (3, "retrieving", "", None),
        (4, "evidence", "", {"chunks": 48, "dense": 27}),
        (5, "generating", "", None),
        (6, "token", "hola", None),
    ]
    events = _events(client.get("/chat/cnv_1/turn/1/stream").text)
    assert [e["type"] for e in events] == [
        "stage", "stage", "stage", "stage", "stage", "token", "done",
    ]
    assert [e["stage"] for e in events if e["type"] == "stage"] == [
        "rewriting", "planning", "retrieving", "evidence", "generating",
    ]


def test_the_evidence_stage_carries_both_counts(client, catalog):
    """They answer different questions: how much reached the prompt, and how
    much of it cleared the dense floor. The second is what tells a narrow
    question from one the corpus supports well."""
    _open(catalog)
    catalog.turn_rows.append(Turn(seq=1, state="answered"))
    catalog.deltas_by_turn[1] = [(1, "evidence", "", {"chunks": 48, "dense": 27})]
    (stage, _done) = _events(client.get("/chat/cnv_1/turn/1/stream").text)
    assert stage["stage"] == "evidence"
    assert stage["chunks"] == 48 and stage["dense"] == 27


def test_a_stage_carries_no_text_so_it_cannot_land_inside_an_answer(client, catalog):
    """`text` means exactly "prose the reader sees". A stage name squeezed into
    it would be concatenated into the middle of an answer by any client that
    appends deltas without checking the kind."""
    _open(catalog)
    catalog.turn_rows.append(Turn(seq=1, answer="ab", state="answered"))
    catalog.deltas_by_turn[1] = [
        (1, "token", "a", None),
        (2, "generating", "", None),
        (3, "token", "b", None),
    ]
    events = _events(client.get("/chat/cnv_1/turn/1/stream").text)
    assert "".join(e.get("text", "") for e in events if e["type"] == "token") == "ab"
    assert all("text" not in e for e in events if e["type"] == "stage")


def test_a_reconnecting_reader_is_sent_the_stages_it_missed_from_since(client, catalog):
    """Stage rows come back through `since` too, so resuming is not just "what
    prose did I miss" but "where has it got to".

    The turn is *settled* here, and it has to be: the route ends on the turn's
    own row, so a running turn would hold this generator open to the twenty
    minute backstop. That is the route working — a model pausing mid-sentence is
    not a model that has finished — and it is why a mid-flight read cannot be
    asserted synchronously. `tests/catalog/test_conversations.py` covers the
    resume against the real table instead.
    """
    _open(catalog)
    catalog.turn_rows.append(Turn(seq=1, answer="ya", state="answered"))
    catalog.deltas_by_turn[1] = [
        (1, "planning", "", None),
        (2, "retrieving", "", None),
    ]
    events = _events(client.get("/chat/cnv_1/turn/1/stream?since=1").text)
    assert [e["stage"] for e in events if e["type"] == "stage"] == ["retrieving"]
