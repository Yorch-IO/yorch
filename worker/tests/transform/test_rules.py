"""The three prompt layers, and the order they go in.

`effort.compose_system` is the precedent and the reason it is worth a test of its
own is recorded there: the variable part is the one an author can rewrite, and
*"an edit that weakened 'do not use general knowledge' would produce a fuller
answer that is worse grounded, which is the failure that reads as success."*
Here the variable part is a genre whose whole vocabulary is about voice, scene
and cadence, which is exactly the prose that reads as permission to embellish.
"""

from __future__ import annotations

import pytest

from brainworker.transform.genres import GENRE_NAMES, GENRES
from brainworker.transform.rules import (
    MODE_RULES,
    PURPOSE_RULES,
    TRANSFORM_RULES,
    compose_transform_system,
)
from brainworker.transform.types import MODES, PURPOSES


@pytest.mark.parametrize("name", GENRE_NAMES)
@pytest.mark.parametrize("mode", MODES)
def test_the_rules_come_first_and_the_genre_last(name: str, mode: str):
    composed = compose_transform_system(GENRES[name], mode)
    rules_at = composed.index(TRANSFORM_RULES[:60])
    mode_at = composed.index(MODE_RULES[mode][:40])
    genre_at = composed.index(GENRES[name].system.strip()[:60])
    assert rules_at < mode_at < genre_at


def test_the_precedence_is_stated_between_the_layers():
    """Order alone is not precedence. A reader of the middle layer has to be
    told that the layer above it wins, because they are reading it in isolation.
    """
    composed = compose_transform_system(GENRES["novel"], "adaptive")
    assert "The six rules above govern everything below" in composed
    assert "the convention gives way" in composed


def test_only_the_enabled_purposes_reach_the_prompt():
    composed = compose_transform_system(GENRES["study"], "faithful", ["contradiction"])
    assert PURPOSE_RULES["contradiction"][:40] in composed
    for other in ("context", "verification", "completion"):
        assert PURPOSE_RULES[other][:40] not in composed


def test_no_purposes_leaves_the_prompt_without_a_research_section():
    composed = compose_transform_system(GENRES["essay"], "faithful", [])
    for rule in PURPOSE_RULES.values():
        assert rule[:40] not in composed


def test_an_unknown_mode_gets_the_stricter_one():
    """The safe direction. A caller's mistake producing a conservative work is
    recoverable; the reverse produces an invented one and reads as success.
    """
    composed = compose_transform_system(GENRES["novel"], "whatever")
    assert MODE_RULES["faithful"][:40] in composed
    assert MODE_RULES["adaptive"][:40] not in composed


@pytest.mark.parametrize("purpose", PURPOSES)
def test_every_purpose_has_a_rule(purpose: str):
    """A purpose a client can enable and the prompt never mentions is a switch
    that does nothing — the shape `SPEECH_RATE_SPREAD` and `ChunkNode.sheet` both
    had, found by auditing a real run rather than by reading.
    """
    assert purpose in PURPOSE_RULES
    assert len(PURPOSE_RULES[purpose]) > 100


def test_the_invariants_name_every_promise_this_feature_makes():
    body = TRANSFORM_RULES.lower()
    for phrase in (
        "never invent a source",
        "chunk_ids",
        "own language",
        "emit only the work",
    ):
        assert phrase in body


def test_the_adaptive_licence_says_what_it_is_not():
    """Read on its own — which is how a model reads it — a licence to
    reorganise, expand and reinterpret is the most invitation-shaped text in
    this feature. Its own last paragraph has to close it.
    """
    adaptive = MODE_RULES["adaptive"].lower()
    assert "not a licence to assert" in adaptive
    assert "does not mean adding material" in adaptive
