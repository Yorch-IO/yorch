"""Naming a conversation, and what happens when the model will not."""

from __future__ import annotations

import json

import pytest

from brainworker.chat import title
from brainworker.chat.types import FALLBACK_TITLE_CHARS
from tests.chat.test_rewrite import FakeProvider


def test_the_fallback_is_the_question_itself_when_it_is_short():
    assert title.fallback("¿Quién fue Jesucristo?") == "¿Quién fue Jesucristo?"


def test_a_long_question_is_truncated_rather_than_dropped():
    """A row in the list is never blank and never says "Sin título": a truncated
    question is a worse title than a generated one and a much better one than
    nothing."""
    out = title.fallback("¿" + "a" * 300 + "?")
    assert len(out) <= FALLBACK_TITLE_CHARS
    assert out.endswith("…")


def test_the_fallback_collapses_the_whitespace_a_textarea_lets_through():
    assert title.fallback("  ¿Quién\n\n fue   Jesucristo? ") == "¿Quién fue Jesucristo?"


def test_a_generated_title_is_stripped_of_the_punctuation_models_add():
    p = FakeProvider({"titulo": '"La divinidad de Cristo."'})
    got, spend = title.title_for(p, "¿Quién fue?", "Fue…")
    assert got == "La divinidad de Cristo"
    assert spend.stage == "chat-title"


def test_a_failed_title_call_leaves_the_existing_name_alone():
    """`None`, not the fallback: overwriting a good title with a truncation
    because a cheap call was rate-limited is a worse outcome than doing nothing.
    """
    got, spend = title.title_for(FakeProvider(explode=True), "¿Quién fue?", "Fue…")
    assert got is None
    assert spend is not None, "a failed call still burned input tokens"


def test_an_empty_title_is_declined_rather_than_written():
    got, _ = title.title_for(FakeProvider({"titulo": "   "}), "q", "a")
    assert got is None


def test_naming_runs_with_reasoning_off():
    from brainworker.config import Gemini

    assert Gemini(project_id="p").thinking_for(title.STAGE) == 0
