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
