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
    SHARED_CONVENTIONS,
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


@pytest.mark.parametrize("name", GENRE_NAMES)
@pytest.mark.parametrize("mode", MODES)
def test_every_genre_is_told_to_write_a_reference_rather_than_say_it(name, mode):
    """Reported from a real recast: a sermon expanded «Juan 10:10» into «abran
    sus Biblias en el Evangelio según San Juan, en el capítulo diez, versículo
    diez».

    Five of the eleven invite exactly that — the two spoken genres and the three
    that carry no apparatus — so the rule binds all of them from one place
    rather than being copied into five prompts, which is the duplication
    `genres/base.py` warns against.
    """
    composed = compose_transform_system(GENRES[name], mode)
    assert "A REFERENCE IS WRITTEN, NOT SPELLED OUT" in composed
    assert "Juan 10:10" in composed
    assert "capítulo diez, versículo diez" in composed, (
        "the rule quotes the failure it was written for, so the model sees both"
    )


@pytest.mark.parametrize("name", GENRE_NAMES)
def test_the_conventions_sit_below_the_rules_and_above_the_genre(name):
    """Order is precedence here as everywhere else in this prompt."""
    composed = compose_transform_system(GENRES[name], "faithful")
    assert (
        composed.index(TRANSFORM_RULES[:60])
        < composed.index(SHARED_CONVENTIONS[:40])
        < composed.index(GENRES[name].system.strip()[:60])
    )


def test_a_genre_may_not_undo_a_shared_convention():
    assert "not the genre's to undo" in compose_transform_system(
        GENRES["novel"], "adaptive"
    )


def test_the_conventions_are_not_smuggled_into_the_six_rules():
    """The six draw their force from being short and from being about
    substance. A convention on how to print a locator is not of that kind, and
    putting it there would dilute the list that must never be argued with."""
    assert "A REFERENCE IS WRITTEN" not in TRANSFORM_RULES
    assert "6. NO IDENTIFIERS" in TRANSFORM_RULES
    assert "7." not in TRANSFORM_RULES


def test_the_shared_conventions_stay_domain_agnostic():
    """Sermon must not implicitly mean religious, and now that the rule binds
    every genre the point is sharper: a treatise recasting a statute reads it
    too."""
    for other in ("art. 14.2", "s. 3(1)(b)", "Fig. 4", "p. 212"):
        assert other in SHARED_CONVENTIONS
