"""What one turn does, and what it deliberately does not do."""

from __future__ import annotations

import pytest

from brainworker.answering.types import Answer, Question
from brainworker.chat import service
from brainworker.chat.types import ChatTurn, TurnRecord
from brainworker.pipeline import Spend


class Settings:
    """Only what `turn` reads. `Provider` is patched out, so `gemini` is a token."""

    gemini = object()


def request(**kw) -> ChatTurn:
    base = dict(
        conversation_id="cnv_1",
        turn_seq=2,
        text="¿y su muerte?",
        library_id="lib_teologia",
        tenant_id="tnt_" + "b" * 24,
    )
    return ChatTurn(**{**base, **kw})


@pytest.fixture
def spy(monkeypatch):
    """Replace the one-shot path with a recorder; keep the real rewrite seam."""
    seen: dict = {}

    def fake_ask(settings, question: Question, on_delta=None, on_stage=None):
        seen["question"] = question
        seen["on_delta"] = on_delta
        seen["on_stage"] = on_stage
        if on_delta is not None:
            on_delta("respu")
            on_delta("esta")
        return Answer(state="answered", text="respuesta", spend=[Spend("answering", "m")])

    monkeypatch.setattr(service, "ask", fake_ask)
    monkeypatch.setattr(service, "Provider", lambda _s: object())
    return seen


def rewrite_to(monkeypatch, text, spend=None):
    monkeypatch.setattr(service.rewrite_mod, "standalone", lambda *_a, **_k: (text, spend))


def test_the_rewritten_question_is_what_gets_searched(monkeypatch, spy):
    rewrite_to(monkeypatch, "¿Qué dice el corpus sobre la muerte de Jesucristo?")
    answer, searched = service.turn(Settings(), request(), [TurnRecord("q", "a")])
    assert spy["question"].text == "¿Qué dice el corpus sobre la muerte de Jesucristo?"
    assert searched == "¿Qué dice el corpus sobre la muerte de Jesucristo?"
    assert answer.state == "answered"


def test_the_tenant_and_library_come_from_the_signal_not_from_the_history(monkeypatch, spy):
    """A conversation is long-lived, so a boundary taken once and reused is a
    boundary that erodes. Every turn carries its own."""
    rewrite_to(monkeypatch, "x")
    service.turn(Settings(), request(), [])
    assert spy["question"].tenant_id == "tnt_" + "b" * 24
    assert spy["question"].library_id == "lib_teologia"


def test_the_effort_level_travels_with_the_turn(monkeypatch, spy):
    rewrite_to(monkeypatch, "x")
    service.turn(Settings(), request(effort="thorough"), [])
    assert spy["question"].effort == "thorough"


def test_the_rewrite_charge_is_prepended_to_the_turns_bill(monkeypatch, spy):
    rewrite_to(monkeypatch, "x", Spend("chat-rewrite", "m", 100, 10, 0.0001))
    answer, _ = service.turn(Settings(), request(), [TurnRecord("q", "a")])
    assert [s.stage for s in answer.spend] == ["chat-rewrite", "answering"]


def test_a_first_turn_bills_only_what_the_question_path_billed(monkeypatch, spy):
    rewrite_to(monkeypatch, "¿Quién fue Jesucristo?", None)
    answer, _ = service.turn(Settings(), request(turn_seq=1), [])
    assert [s.stage for s in answer.spend] == ["answering"]


def test_the_delta_callback_reaches_the_answering_call(monkeypatch, spy):
    rewrite_to(monkeypatch, "x")
    sink: list[str] = []
    service.turn(Settings(), request(), [], on_delta=sink.append)
    assert sink == ["respu", "esta"]


def test_the_stage_callback_reaches_the_answering_call(monkeypatch, spy):
    rewrite_to(monkeypatch, "x")
    seen: list = []
    service.turn(Settings(), request(), [], on_stage=lambda *a: seen.append(a))
    assert spy["on_stage"] is not None


def test_a_follow_up_announces_that_it_is_rewriting(monkeypatch, spy):
    """The first thing a follow-up does, and it is a network call."""
    rewrite_to(monkeypatch, "x")
    seen: list = []
    service.turn(
        Settings(), request(), [TurnRecord("q", "a")],
        on_stage=lambda name, detail=None: seen.append(name),
    )
    assert seen[0] == "rewriting"


def test_a_first_turn_announces_no_rewrite_because_it_does_none(monkeypatch, spy):
    """Announcing a stage that does not happen is a progress display that lies —
    and it lies in the direction of looking slower than it is."""
    rewrite_to(monkeypatch, "x")
    seen: list = []
    service.turn(
        Settings(), request(turn_seq=1), [],
        on_stage=lambda name, detail=None: seen.append(name),
    )
    assert "rewriting" not in seen


def test_a_turn_with_no_callback_still_answers(monkeypatch, spy):
    """Streaming is a delivery choice, not a mode. The paid plane's collect path
    and every test in this file take the same route with it absent."""
    rewrite_to(monkeypatch, "x")
    answer, _ = service.turn(Settings(), request(), [])
    assert spy["on_delta"] is None
    assert answer.state == "answered"


def test_the_conversation_never_reaches_the_answering_call(monkeypatch, spy):
    """The history stops at the rewrite, on purpose.

    Threading it into the answer prompt would put a transcript in the most
    expensive prompt in the product, competing with `answer.SYSTEM`'s first rule
    — answer only from the fragments — using text that is not fragments.
    """
    rewrite_to(monkeypatch, "x")
    service.turn(Settings(), request(), [TurnRecord("previa", "anterior")])
    question = spy["question"]
    assert "previa" not in question.text and "anterior" not in question.text
    assert not hasattr(question, "history")


# -- the window -------------------------------------------------------------


def test_the_window_keeps_the_most_recent_exchanges():
    """Which end is dropped decides whether the rewrite sees the turn the
    pronoun refers to or the opening of a long conversation."""
    records = [TurnRecord(f"q{i}", f"a{i}") for i in range(10)]
    kept = service.window_from(records, 3)
    assert [r.question for r in kept] == ["q7", "q8", "q9"]


def test_a_short_conversation_keeps_all_of_it():
    records = [TurnRecord("q0", "a0")]
    assert service.window_from(records, 6) == records


def test_a_window_of_nothing_is_empty_rather_than_everything():
    """`records[-0:]` is the whole list, which is the bug this exists to avoid."""
    records = [TurnRecord(f"q{i}", f"a{i}") for i in range(3)]
    assert service.window_from(records, 0) == []
