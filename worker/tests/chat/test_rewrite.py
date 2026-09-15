"""Turning a follow-up into something the index can be searched with.

The rewrite is the entire multi-turn mechanism, and its failure modes are quiet:
a bad rewrite retrieves the wrong chunks and the answer that follows is fluent,
cited, verifiable and about the wrong thing. So these tests are mostly about
what it must *not* do.
"""

from __future__ import annotations

import json

import pytest

from brainworker.chat import rewrite
from brainworker.chat.types import TurnRecord
from brainworker.providers.gemini import Generation, Usage


class FakeProvider:
    def __init__(self, payload=None, *, explode: bool = False) -> None:
        self.payload = payload
        self.explode = explode
        self.prompts: list[str] = []
        self.stages: list[str | None] = []

        class _S:
            model = "gemini-3.6-flash"
            stage_thinking: dict[str, int | None] = {}
            thinking_budget: int | None = None

        self.settings = _S()

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None,
                 thinking_budget=None):
        self.prompts.append(prompt)
        self.stages.append(stage)
        if self.explode:
            raise RuntimeError("quota")
        return Generation(
            text=json.dumps(self.payload, ensure_ascii=False),
            usage=Usage(120, 20, 0, 1),
        )


def history() -> list[TurnRecord]:
    return [TurnRecord(question="¿Quién fue Jesucristo?", answer="Fue presentado como…")]


def test_a_first_turn_is_not_rewritten_and_costs_nothing():
    """No history means nothing to resolve, and a call to establish that would
    be a charge on every new conversation for a foregone conclusion."""
    p = FakeProvider()
    text, spend = rewrite.standalone(p, [], "¿Quién fue Jesucristo?")
    assert text == "¿Quién fue Jesucristo?"
    assert spend is None
    assert p.prompts == [], "the first turn reached the model"


def test_a_follow_up_is_rewritten_and_the_conversation_is_what_it_reads():
    p = FakeProvider({"pregunta": "¿Qué dice el corpus sobre la muerte de Jesucristo?"})
    text, spend = rewrite.standalone(p, history(), "¿y su muerte?")
    assert text == "¿Qué dice el corpus sobre la muerte de Jesucristo?"
    sent = json.loads(p.prompts[0])
    assert sent["mensaje"] == "¿y su muerte?"
    assert sent["conversacion"][0]["usuario"] == "¿Quién fue Jesucristo?"
    assert spend is not None and spend.stage == "chat-rewrite"


def test_the_rewrite_runs_with_reasoning_off():
    """Substitution, not judgement — and it sits between the keypress and the
    first token, so reasoning there is paid for in latency as well as tokens."""
    from brainworker.config import Gemini

    assert Gemini(project_id="p").thinking_for(rewrite.STAGE) == 0


def test_a_failed_rewrite_searches_the_raw_message_rather_than_failing():
    """One badly-retrieved answer beats a conversation that stops working
    because a cheap preprocessing call was rate-limited."""
    p = FakeProvider(explode=True)
    text, spend = rewrite.standalone(p, history(), "¿y su muerte?")
    assert text == "¿y su muerte?"
    assert spend is not None, "a call that failed after burning input tokens still spent"


def test_an_empty_rewrite_falls_back_to_the_message():
    p = FakeProvider({"pregunta": "   "})
    text, _ = rewrite.standalone(p, history(), "¿y su muerte?")
    assert text == "¿y su muerte?"


def test_the_spend_names_its_own_stage_so_the_ledger_can_separate_it():
    p = FakeProvider({"pregunta": "x"})
    _, spend = rewrite.standalone(p, history(), "¿y?")
    assert spend.stage == "chat-rewrite"
    assert spend.input_tokens == 120 and spend.output_tokens == 20


def test_the_prompt_forbids_answering():
    """A model asked for 'the question' will supply the answer instead, and the
    answer would then be embedded and searched for."""
    assert "NO respondas" in rewrite.SYSTEM
    assert list(rewrite.SCHEMA["properties"]) == ["pregunta"]
