"""The quote: a range whose ends are a chapter count, and a contract on the planner."""

from __future__ import annotations

import pytest

from brainworker.transform import estimate
from brainworker.transform.genres import GENRE_NAMES, GENRES
from brainworker.transform.types import MAX_CHAPTER_SOURCE_CHARS, MAX_CHAPTERS


@pytest.mark.parametrize("name", GENRE_NAMES)
def test_the_high_end_is_never_below_the_low_end(name: str):
    e = estimate.transform_estimate(
        characters=120_000, source_chapters=10, genre=GENRES[name],
        purposes=["context"], research_budget=12,
    )
    assert e.total_usd is not None and e.total_usd_high is not None
    assert e.total_usd_high >= e.total_usd
    for stage in e.stages:
        assert stage.output_tokens_high >= stage.output_tokens
        if stage.usd is not None and stage.usd_high is not None:
            assert stage.usd_high >= stage.usd


def test_the_projected_chapter_count_is_floored_by_what_one_call_can_write():
    """A document with one detected chapter and 200,000 characters is not a
    one-chapter work, and the fallback would split it whatever the ratio said."""
    projected = estimate.projected_chapters(1, 200_000, GENRES["essay"])
    assert projected >= 200_000 // MAX_CHAPTER_SOURCE_CHARS


def test_the_projection_is_capped():
    assert estimate.projected_chapters(500, 5_000_000, GENRES["commentary"]) <= MAX_CHAPTERS
    assert estimate.chapter_cap(MAX_CHAPTERS) <= MAX_CHAPTERS


def test_the_cap_is_above_the_projection_and_is_what_the_high_end_prices():
    projected = estimate.projected_chapters(10, 120_000, GENRES["treatise"])
    assert estimate.chapter_cap(projected) > projected


def test_a_run_with_no_research_quotes_no_research():
    e = estimate.transform_estimate(
        characters=50_000, source_chapters=5, genre=GENRES["essay"],
        purposes=[], research_budget=0,
    )
    assert "transform-research" not in {s.stage for s in e.stages}
    # The probe is quoted anyway: it ran, and a stage that cost almost nothing
    # and a stage that did not run are different facts.
    assert "transform-probe" in {s.stage for s in e.stages}


def test_every_stage_is_priced_or_named_as_unpriced():
    e = estimate.transform_estimate(
        characters=50_000, source_chapters=5, genre=GENRES["essay"],
        purposes=["context"], research_budget=6,
    )
    for stage in e.stages:
        assert stage.usd is not None or stage.stage in e.unpriced_stages


def test_composition_dominates_the_bill():
    """Where the gate sits rests on this. If it ever stops being true, the
    second gate is in the wrong place."""
    e = estimate.transform_estimate(
        characters=200_000, source_chapters=12, genre=GENRES["treatise"],
        purposes=["context", "verification"], research_budget=24,
    )
    by_stage = {s.stage: (s.usd or 0.0) for s in e.stages}
    compose = by_stage["transform-compose"]
    assert compose > sum(v for k, v in by_stage.items() if k != "transform-compose")


def test_a_longer_document_costs_more():
    def total(chars: int) -> float:
        return estimate.transform_estimate(
            characters=chars, source_chapters=10, genre=GENRES["essay"],
            purposes=[], research_budget=0,
        ).total_usd or 0.0

    assert total(400_000) > total(40_000)


def test_the_price_source_travels_with_the_quote():
    """Any surface showing a dollar figure has to repeat it: the prices are
    third-party multipliers over measured token counts, and they go stale."""
    e = estimate.transform_estimate(
        characters=1_000, source_chapters=1, genre=GENRES["essay"],
        purposes=[], research_budget=0,
    )
    assert "2026-08-20" in e.price_source


#: The first production transformation, 2026-09-18: a 1994 sermon transcript
#: recast as an essay in three chapters. One document and one genre, so this is
#: a measurement and not a corpus — but it is the only one there is, and the
#: quote that ran against it failed in **both** directions at once.
FIRST_RUN = {
    "characters": 61_646,
    "source_chapters": 1,
    "genre": "essay",
    "purposes": ["context", "verification"],
    "research_budget": 9,
    "billed": {
        "transform-probe": 0.000190,
        "transform-genre": 0.010413,
        "transform-plan": 0.057599,
        "transform-research": 0.000119,
        "transform-compose": 0.143997,
    },
}


def _first_run_estimate():
    return estimate.transform_estimate(
        characters=FIRST_RUN["characters"],
        source_chapters=FIRST_RUN["source_chapters"],
        genre=GENRES[FIRST_RUN["genre"]],
        purposes=FIRST_RUN["purposes"],
        research_budget=FIRST_RUN["research_budget"],
    )


def test_no_stage_of_the_first_real_run_comes_in_under_its_quote():
    """The rule this product states outright: an estimate must over-report.

    Three of the five stages broke it the first time this ran for real.
    `transform-genre` and `transform-plan` were quoted from their *visible*
    answers while reasoning is on for both — billed 1.4x and 1.9x the quote —
    and `transform-research` was priced with `EVAL_QUERY_TOKENS`, which is sized
    for a question somebody typed rather than for a slice of the source.
    """
    e = _first_run_estimate()
    quoted = {s.stage: (s.usd_high if s.usd_high is not None else s.usd) for s in e.stages}
    under = [
        stage
        for stage, billed in FIRST_RUN["billed"].items()
        if billed > (quoted.get(stage) or 0)
    ]
    assert under == [], f"quoted under the bill for {under}"


def test_the_first_real_run_is_not_over_reported_wildly_either():
    """The other half of the same rule, and the other half of the failure.

    The quote that ran was **$1.0371 – $1.6494** against a bill of
    **$0.212318** — 4.9x at the low end. Over-reporting misleads somebody into
    declining affordable work exactly as much as under-reporting misleads them
    into approving expensive work, and a five-fold quote on a run that cost
    twenty cents is the kind most likely to be met with "that is too much".
    """
    e = _first_run_estimate()
    billed = sum(FIRST_RUN["billed"].values())
    assert e.total_usd is not None and e.total_usd_high is not None
    assert e.total_usd > billed, "and still over-reports"
    assert e.total_usd / billed < 2.5, (
        f"low end is {e.total_usd / billed:.1f}x the bill"
    )
    assert e.total_usd_high / billed < 4.0


def test_the_stages_that_reason_carry_a_multiplier_and_the_one_that_does_not_does_not():
    """Reasoning is billed as output and excluded from `candidates_token_count`.

    `transform-compose` is deliberately absent: its ratio is measured against
    real output tokens and already contains the reasoning, and a multiplier on
    top would compound two margins — the mistake
    `THINKING_OUTPUT_MULTIPLIER["semantics"]` records against itself.
    """
    assert set(estimate.THINKING_OUTPUT_MULTIPLIER) == {
        "transform-genre",
        "transform-plan",
    }
