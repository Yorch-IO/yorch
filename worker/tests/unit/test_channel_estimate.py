"""What reading a channel is quoted at, before anything is spent.

Two properties are worth more than the arithmetic. The metadata half is priced
from the **literal string** the call will send, so it cannot drift from the call;
and the transcript half carries a **range on its input**, which no other
estimator in this product does, because its input is projected from a duration
through a constant that has a measured spread.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from brainworker import config, videosource
from brainworker.channel import estimate as est
from brainworker.channel import preselect, topics
from brainworker.youtube import ChannelVideo


def _settings(**gemini) -> config.Settings:
    return config.Settings(
        workspace=pathlib.Path("/tmp"),
        temporal_target="",
        temporal_namespace="default",
        task_queue="q",
        qdrant_url="",
        qdrant_collection="brain",
        memgraph_url="",
        database_url="",
        log_level="INFO",
        secrets_file=pathlib.Path("/nonexistent"),
        gemini=config.Gemini(project_id="p", **gemini),
    )


def _videos(n: int, duration_s: int = 2700) -> list[ChannelVideo]:
    return [
        ChannelVideo(
            video_id=f"v{i:010d}"[:11],
            title=f"Prédica {i}",
            description="d" * 200,
            published_at="2026-01-01T00:00:00Z",
            duration_s=duration_s,
        )
        for i in range(n)
    ]


def _row(estimate, stage):
    return next((s for s in estimate.stages if s.stage == stage), None)


# --- what a discovery would do -----------------------------------------------


def test_the_plan_judges_the_most_recent_and_reads_the_longest():
    # `videos` arrives newest-first, so `limit` means "the most recent N" —
    # which is what "revisa los últimos cien" means. The *read* set is the
    # longest of those, because at quote time nobody knows which will survive
    # the preselection and the longest is the worst case by input.
    videos = _videos(5, 600)
    videos[3] = replace(videos[3], duration_s=9000)
    plan = est.plan_for("t", videos, limit=4, deep_limit=1)
    assert [v.video_id for v in plan.evaluated] == [v.video_id for v in videos[:4]]
    assert [v.video_id for v in plan.read] == [videos[3].video_id]


def test_an_upcoming_premiere_is_not_judged():
    # `resolve_video` refuses it anyway, so quoting it would price a video that
    # cannot run.
    videos = _videos(2)
    videos[0] = replace(videos[0], live_state="upcoming")
    plan = est.plan_for("t", videos, limit=10, deep_limit=10)
    assert [v.video_id for v in plan.evaluated] == [videos[1].video_id]


def test_a_title_filter_narrows_the_judged_set_before_the_limit():
    # The screen's keyword chips are free and narrow the catalogue before the
    # model reads a title. The filter is applied *before* `limit`, so "the most
    # recent N *of these*" is what gets judged — filtering after the limit would
    # let a filter over an old series evaluate nothing at all.
    videos = _videos(6, 600)
    wanted = [videos[1].video_id, videos[4].video_id, videos[5].video_id]
    plan = est.plan_for("t", videos, limit=2, deep_limit=1, video_ids=wanted)
    assert [v.video_id for v in plan.evaluated] == wanted[:2]
    # Newest-first is preserved: the catalogue's order, not the filter's.
    shuffled = list(reversed(wanted))
    plan = est.plan_for("t", videos, limit=10, deep_limit=1, video_ids=shuffled)
    assert [v.video_id for v in plan.evaluated] == wanted


def test_an_id_the_catalogue_does_not_hold_is_ignored_not_refused():
    # The catalogue can change between syncs, and the `preselection` artifact
    # records the exact ids evaluated, so the filter's effect is on the record
    # either way.
    videos = _videos(3)
    plan = est.plan_for(
        "t", videos, limit=10, deep_limit=1, video_ids=["zzzzzzzzzzz", videos[2].video_id]
    )
    assert [v.video_id for v in plan.evaluated] == [videos[2].video_id]


def test_no_filter_means_the_whole_catalogue():
    videos = _videos(3)
    assert est.plan_for("t", videos, limit=10, deep_limit=1).evaluated == videos
    assert est.plan_for("t", videos, limit=10, deep_limit=1, video_ids=None).evaluated == videos
    # An *empty* filter is a filter: nothing matched, nothing is judged.
    assert est.plan_for("t", videos, limit=10, deep_limit=1, video_ids=[]).evaluated == []


def test_reading_nothing_is_allowed_and_quotes_nothing():
    plan = est.plan_for("t", _videos(3), limit=3, deep_limit=0)
    estimate = est.discovery_estimate(_settings(), plan)
    assert _row(estimate, topics.STAGE) is None
    assert _row(estimate, preselect.STAGE) is not None


def test_a_channel_with_nothing_in_it_quotes_nothing_rather_than_zero():
    # A stage absent renders as no row. A row of zeroes would read as a pass
    # about to do nothing, which is a different claim.
    estimate = est.discovery_estimate(_settings(), est.plan_for("t", [], limit=10, deep_limit=5))
    assert estimate.stages == []
    assert estimate.total_usd is None


# --- the metadata half is exact ----------------------------------------------


def test_the_metadata_half_prices_the_string_the_call_will_send():
    videos = _videos(30)
    plan = est.plan_for("justicia social", videos, limit=30, deep_limit=0)
    row = _row(est.discovery_estimate(_settings(), plan), preselect.STAGE)

    batches = preselect.batches(videos)
    characters = sum(len(preselect.payload_for("justicia social", b)) for b in batches)
    from brainworker.activities.ingest import CHARS_PER_TOKEN

    assert row.input_tokens == int(characters / CHARS_PER_TOKEN) + len(
        batches
    ) * preselect.CALL_OVERHEAD
    assert row.output_tokens == 30 * preselect.OUTPUT_PER_VIDEO


def test_the_metadata_half_has_no_range_because_it_has_no_unknown():
    plan = est.plan_for("t", _videos(10), limit=10, deep_limit=0)
    row = _row(est.discovery_estimate(_settings(), plan), preselect.STAGE)
    assert row.usd_high == row.usd
    assert row.output_tokens_high == row.output_tokens


# --- the transcript half carries the speech rate's own range -----------------


def test_the_transcript_half_widens_its_input_and_not_its_output():
    # Every other estimator in this product widens output only, because its
    # input is the document's own characters. Here the input is projected from
    # a duration, and the constant that projects it has a measured spread.
    plan = est.plan_for("t", _videos(4, 4573), limit=4, deep_limit=4)
    row = _row(est.discovery_estimate(_settings(), plan), topics.STAGE)
    assert row.output_tokens_high == row.output_tokens
    assert row.usd_high > row.usd


def test_the_range_is_exactly_the_measured_speech_rate_spread():
    plan = est.plan_for("t", _videos(1, 4573), limit=1, deep_limit=1)
    row = _row(est.discovery_estimate(_settings(), plan), topics.STAGE)
    from brainworker.activities.ingest import CHARS_PER_TOKEN

    low = videosource.projected_characters(4573)
    high = videosource.projected_characters(4573, high=True)
    assert row.input_tokens == int(low / CHARS_PER_TOKEN) + topics.CALL_OVERHEAD
    # The high figure is not a row of its own; it is what `usd_high` prices.
    from brainworker.activities.ingest import price_for

    assert row.usd_high == price_for(
        row.model,
        int(high / CHARS_PER_TOKEN) + topics.CALL_OVERHEAD,
        row.output_tokens,
    )


def test_a_pathological_duration_is_capped_rather_than_quoted_whole():
    # The ceiling bounds a worst case; `TRANSCRIPT_CHARS` is what the call
    # actually reads, so quoting beyond it would price a call that cannot happen.
    plan = est.plan_for("t", _videos(1, 60 * 60 * 12), limit=1, deep_limit=1)
    row = _row(est.discovery_estimate(_settings(), plan), topics.STAGE)
    from brainworker.activities.ingest import CHARS_PER_TOKEN

    assert row.input_tokens == int(
        topics.TRANSCRIPT_CHARS / CHARS_PER_TOKEN
    ) + topics.CALL_OVERHEAD


def test_the_overhead_is_paid_once_per_video_not_once_per_batch():
    # One call per video, because a transcript is the whole input and batching
    # would make a reading unattributable to its video.
    one = est.plan_for("t", _videos(1, 600), limit=1, deep_limit=1)
    four = est.plan_for("t", _videos(4, 600), limit=4, deep_limit=4)
    a = _row(est.discovery_estimate(_settings(), one), topics.STAGE)
    b = _row(est.discovery_estimate(_settings(), four), topics.STAGE)
    assert b.output_tokens == 4 * a.output_tokens


# --- reasoning ----------------------------------------------------------------


def test_both_passes_have_reasoning_off_by_default():
    s = _settings()
    assert s.gemini.thinking_for(preselect.STAGE) == 0
    assert s.gemini.thinking_for(topics.STAGE) == 0


def test_a_global_reasoning_budget_reaches_the_quote():
    # Reasoning is billed as output. A budget somebody set globally that the
    # quote ignored would surprise them at the bill, which is the direction this
    # product refuses.
    plan = est.plan_for("t", _videos(10), limit=10, deep_limit=0)
    off = _row(est.discovery_estimate(_settings(), plan), preselect.STAGE)
    on = _settings(
        thinking_budget=4096,
        stage_thinking={},
    )
    row = _row(est.discovery_estimate(on, plan), preselect.STAGE)
    # No measured multiplier for this stage, so the figure does not move — but
    # the branch is taken, which is what keeps a measured entry working the day
    # somebody adds one.
    assert on.gemini.thinking_for(preselect.STAGE) == 4096
    assert row.output_tokens == off.output_tokens


# --- totals -------------------------------------------------------------------


def test_the_total_is_the_sum_of_both_ends():
    plan = est.plan_for("t", _videos(20, 4573), limit=20, deep_limit=5)
    estimate = est.discovery_estimate(_settings(), plan)
    assert estimate.total_usd == pytest.approx(sum(s.usd for s in estimate.stages))
    assert estimate.total_usd_high == pytest.approx(
        sum(s.usd_high for s in estimate.stages)
    )
    assert estimate.total_usd_high > estimate.total_usd
    assert estimate.price_source
