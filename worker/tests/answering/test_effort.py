"""What each effort level promises, and what it must never reach.

The properties here are the ones that make the level safe to expose in a UI: the
middle level is the behaviour that predates it, an unrecognised level answers
rather than crashes, and the two knobs that are floors rather than volumes stay
out of the table.
"""

from __future__ import annotations

import typing

import pytest

from brainworker.answering import retrieve
from brainworker.answering.effort import (
    BUDGETS,
    DEFAULT_EFFORT,
    EFFORT_LEVELS,
    MAX_TOP_K,
    MAX_STYLE_CHARS,
    Budget,
    budget_for,
    compose_system,
    effective_style_level,
    resolve_top_k,
)
from brainworker.answering.types import Question


def test_standard_is_exactly_what_the_product_served_before_effort_existed():
    """The whole reason adopting this module was safe.

    Deliberately literal on this side. These six numbers are a fact about what
    shipped, not a restatement of the table — asserting `BUDGETS["standard"]`
    against itself would pass no matter what anybody edited. What makes it a
    real check is that these were read out of `retrieve.py` and `types.py` at
    the commit before levels existed.
    """
    standard = budget_for("standard")
    assert standard.top_k == 8
    assert standard.candidate_limit == 40
    assert standard.prefetch_limit == 50
    assert standard.claims_per_chunk == 3
    assert standard.thinking_override is None


def test_the_default_level_is_the_one_that_changes_nothing():
    assert DEFAULT_EFFORT == "standard"
    assert budget_for(None) == budget_for("standard")


def test_the_standard_prefetch_is_the_width_the_engine_would_have_used():
    """The one number in the table that duplicates an engine constant.

    No production caller passed `prefetch_limit` before this module, so the
    engine's own default *was* the served value. Now that a caller passes one,
    the two can drift apart silently — a narrower prefetch is not an error, it
    just quietly retrieves less. This is what notices.
    """
    from docagent.qdrant import PREFETCH_LIMIT

    assert budget_for("standard").prefetch_limit == PREFETCH_LIMIT


def test_the_module_constants_still_describe_the_default_level():
    """`scripts/probe_retrieval.py` imports these to explain a real retrieval.

    A probe carrying figures that no longer match what production serves would
    confidently explain a retrieval that never happened — which is the reason
    that script imports them from here rather than restating them.
    """
    standard = budget_for(DEFAULT_EFFORT)
    assert retrieve.CANDIDATE_LIMIT == standard.candidate_limit
    assert retrieve.CLAIMS_PER_CHUNK == standard.claims_per_chunk


@pytest.mark.parametrize("value", ["", "  ", "THOROUGH", "nonsense", None])
def test_an_unrecognised_level_answers_rather_than_raising(value):
    """Totality is the second of two guards, and this is the second one.

    Both control planes refuse an unknown level at the edge, where the caller
    can be told what was wrong. Anything still holding one by the time it
    reaches here arrived by some other route, and the useful behaviour then is a
    good answer rather than a workflow that dies mid-question.
    """
    assert budget_for(value) == BUDGETS[DEFAULT_EFFORT]


def test_the_literal_and_the_level_tuple_cannot_drift():
    """Two copies of the level names exist, and this is why that is safe.

    `Question.effort` is spelled as a `Literal` so FastAPI refuses an unknown
    level itself; `Literal` needs its values at type-check time, so it cannot be
    built from `EFFORT_LEVELS`. That leaves two lists that must agree, and
    nothing but this asserts that they do.
    """
    hints = typing.get_type_hints(Question)
    assert typing.get_args(hints["effort"]) == EFFORT_LEVELS


def test_every_level_in_the_tuple_has_a_budget_and_vice_versa():
    assert tuple(BUDGETS) == EFFORT_LEVELS
    assert DEFAULT_EFFORT in EFFORT_LEVELS


def test_the_ladder_rises_and_never_doubles_back():
    """Effort is an order, not a set. A UI draws it as a ladder."""
    ladder = [BUDGETS[name] for name in EFFORT_LEVELS]
    for lower, higher in zip(ladder, ladder[1:]):
        assert higher.top_k > lower.top_k
        assert higher.candidate_limit > lower.candidate_limit
        assert higher.claims_per_chunk >= lower.claims_per_chunk
        assert higher.prefetch_limit >= lower.prefetch_limit


def test_no_level_can_reach_the_dense_floor_or_the_section_cap():
    """The two knobs effort is forbidden to scale, asserted structurally.

    `MIN_SCORE` is the topicality floor: a sweep measured 0.50 scoring best of
    everything tried and being wrong, because that index's noise floor is 0.5153
    — the value a *wrong* chunk scores. `PER_SECTION` is a diversity policy
    rather than a volume one (`diversify` backfills, so a wider `top_k` fills
    either way) and it already has a per-version claimant in a profile's
    `retrieval` block.

    Asserted on the dataclass rather than on a call, because the failure this
    guards against is somebody adding the field — at which point every caller
    starts honouring it and no behavioural test would obviously break.
    """
    fields = set(Budget.__dataclass_fields__)
    assert "min_score" not in fields
    assert "per_section" not in fields


def test_no_level_reasons_less_than_the_configured_default():
    """No rung of the ladder is *less* careful than the product was.

    Answering reasons by default against a neutral measurement — a deliberate
    bias toward caution in the stage that either cites or fabricates. Effort may
    raise that and may leave it alone; a level that named a budget below the
    others would be lowering it, and `None` is the only way to say "leave it".
    """
    for name in EFFORT_LEVELS:
        override = BUDGETS[name].thinking_override
        assert override is None or override > 0


class TestResolveTopK:
    def test_no_request_means_the_level_decides(self):
        for name in EFFORT_LEVELS:
            budget = BUDGETS[name]
            assert resolve_top_k(None, budget) == budget.top_k

    def test_an_explicit_request_still_wins(self):
        """The API has accepted `top_k` since before levels existed."""
        assert resolve_top_k(3, budget_for("standard")) == 3

    def test_an_oversized_request_is_clamped_rather_than_refused(self):
        assert resolve_top_k(10_000_000, budget_for("brief")) == MAX_TOP_K

    @pytest.mark.parametrize("bad", [0, -1, -10_000])
    def test_a_zero_or_negative_request_cannot_widen_the_search(self, bad):
        """The sharp edge under this clamp, and it is not the obvious one.

        `diversify` compares `len(out) == top_k` *after* appending, so with
        `top_k = 0` the equality is never reached, the backfill loop's break
        never fires, and it returns **every** fused candidate — 40 of them at
        the default. A zero therefore asked for more than the default, not less,
        and reached the answering prompt as ~40 chunks on a paid call.
        """
        assert resolve_top_k(bad, budget_for("standard")) == 1


def test_a_zero_top_k_no_longer_reaches_the_engines_sharp_edge():
    """The clamp above, checked against the engine behaviour that motivates it.

    Pinned as a real call rather than an argument, because the edge lives in
    `docagent` and this is the only thing standing between it and a request
    body. If `resolve_top_k`'s floor is removed, this returns 40.
    """
    from docagent.qdrant import Hit, diversify

    hits = [Hit(1.0 - i / 100, {"breadcrumb": f"S{i % 3}"}) for i in range(40)]
    assert len(diversify(hits, 2, 0)) == 40, "the engine edge this guards"

    resolved = resolve_top_k(0, budget_for("standard"))
    assert len(diversify(hits, retrieve.PER_SECTION, resolved)) == 1


# --- the style block, and stepping down to it -------------------------------


class TestEffectiveStyleLevel:
    """Which level's *wording* an answer gets, given what retrieval found.

    The search has already run and been paid for by the time this is asked, so
    what steps down is how developed the answer is, never how hard it looked.
    """

    def test_a_level_that_got_what_it_asked_for_keeps_its_own_voice(self):
        for name in EFFORT_LEVELS:
            assert effective_style_level(name, BUDGETS[name].top_k) == name

    def test_the_widest_level_on_thin_evidence_answers_briefly(self):
        """The case this exists for.

        `thorough` asks for a paragraph per distinct point. Given five chunks it
        would have to pad, and padding is prose no citation backs — the one
        surface `answer._verify` cannot check. Stepping down removes the
        occasion rather than forbidding it in wording the model may not honour.
        """
        assert effective_style_level("thorough", 5) == "brief"
        assert effective_style_level("thorough", 8) == "standard"
        assert effective_style_level("thorough", 16) == "thorough"

    def test_it_only_ever_narrows(self):
        """Receiving sixteen chunks is not a reason to write an essay somebody
        asked not to have."""
        assert effective_style_level("brief", 16) == "brief"
        assert effective_style_level("standard", 999) == "standard"

    @pytest.mark.parametrize("count", [0, 1, 3])
    def test_less_than_the_narrowest_level_still_answers(self, count):
        assert effective_style_level("thorough", count) == EFFORT_LEVELS[0]

    def test_an_unknown_level_falls_back_like_everything_else(self):
        assert effective_style_level("exhaustivo", 16) == DEFAULT_EFFORT


class TestComposeSystem:
    def test_the_rules_come_first_and_the_style_after(self):
        out = compose_system("REGLAS", "en verso")
        assert out.index("REGLAS") < out.index("en verso")

    def test_it_states_which_wins(self):
        """The sentence that makes the block safe to hand to a user.

        `style` is the one part of this prompt an organisation may rewrite. An
        edit that weakened "no uses conocimiento general" would produce a fuller
        answer that is worse grounded — the failure that reads as success — so
        the assembled prompt says the rules outrank the style rather than
        leaving their order to imply it.
        """
        out = compose_system("REGLAS", "en verso")
        assert "mandan las reglas" in out

    def test_no_style_leaves_the_rules_exactly_as_they_were(self):
        """A level with nothing to add must not change the prompt at all —
        otherwise every question pays for a paragraph that says nothing."""
        assert compose_system("REGLAS", "") == "REGLAS"
        assert compose_system("REGLAS", "   \n ") == "REGLAS"


def test_every_level_ships_a_default_style():
    """An absent row means "use the default", so a level with no default would
    silently answer with no style guidance at all."""
    for name in EFFORT_LEVELS:
        assert BUDGETS[name].style.strip(), name
        assert len(BUDGETS[name].style) <= MAX_STYLE_CHARS


def test_the_style_level_survives_serialisation():
    """The bug this exists for was invisible in every other way.

    `service.ask` set `style_effort` as a loose attribute on the `Answer`
    instead of the dataclass declaring it. Python allows that, so the answered
    path worked and every test passed — while `asdict()` dropped it from the
    API response, because it serialises declared fields and nothing else. There
    was no error anywhere; there was just a field the UI could never see.

    Asserted through `asdict` rather than on the object, because reading the
    attribute directly is exactly what did *not* catch it.
    """
    import dataclasses

    from brainworker.answering.types import Answer

    payload = dataclasses.asdict(Answer(state="answered", style_effort="brief"))
    assert payload["style_effort"] == "brief"
    assert payload["effort"] == ""


def test_every_answer_the_service_can_return_names_a_style_level():
    """Including the off-corpus one, which returns before a style is chosen and
    is therefore the path the assignment cannot reach."""
    import inspect

    from brainworker.answering import service

    source = inspect.getsource(service)
    # One `Answer(` construction in this module, and it must carry the field.
    assert source.count("Answer(") == 1
    assert "style_effort=" in source
