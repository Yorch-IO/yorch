"""What reading a channel costs, quoted before anything is spent.

Two paid passes and no others: the catalogue is quota, the probe is free, and
the indexing of whatever survives is quoted separately by each video's own gate
— which is the existing `estimate_video`, reused rather than reimplemented.

**This estimator widens the input, and every other one in this product widens
only the output.** That is not an inconsistency, it is the same rule applied to
a different unknown. `estimate_for` says of its own spread: *"Input is nearly
deterministic — the document's characters plus a per-call overhead, both known
before the run."* Here half of it is not. The preselection's input is exact,
because `preselect.payload_for` builds the literal string that will be sent. The
topic pass's input is a *projection from a duration* through
`videosource.CHARS_PER_SECOND_OF_SPEECH`, which is the one measured constant in
this product with a measured spread attached — 14.5 c/s pooled over 11.21 hours
of real Spanish preaching, with `SPEECH_RATE_SPREAD` of 1.40 against a measured
maximum of 15.24. So the range on that stage is the speech rate's own range, and
reporting a single number would be claiming a precision the measurement does not
have.

The direction rule is the product's: under-reporting misleads a person into
approving work they would have declined, and wild over-reporting misleads them
into declining work they could afford. Both are failures, which is why the low
end stays within reach of a typical video rather than being pushed up to cover
the worst.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import videosource
from ..activities.ingest import CHARS_PER_TOKEN, PRICE_SOURCE, price_for
from ..config import Settings
from ..pipeline import Estimate, StageEstimate
from ..youtube import ChannelVideo
from . import preselect, topics


@dataclass(frozen=True)
class DiscoveryPlan:
    """Exactly what a discovery run would do, so the quote can price it.

    Kept apart from the quote itself because the screen shows both: how many
    videos will be judged and how many will be read is the decision, and the
    dollars are its consequence.
    """

    topic: str
    #: Every video whose metadata is judged.
    evaluated: list[ChannelVideo]
    #: The videos whose transcripts would be read. At quote time nobody knows
    #: which they will be — the preselection has not run — so this is the
    #: **worst case by input**: the longest of the candidates, up to the limit.
    #: Choosing the shortest would quote a bill the run cannot come in under.
    read: list[ChannelVideo]

    @property
    def deep_limit(self) -> int:
        return len(self.read)


def plan_for(
    topic: str,
    videos: list[ChannelVideo],
    *,
    limit: int,
    deep_limit: int,
    video_ids: list[str] | None = None,
) -> DiscoveryPlan:
    """Which videos a discovery would judge and which it would read.

    `videos` arrives newest-first from the store, so `limit` means "the most
    recent N", which is what the screen offers and what a person means by
    "revisa los últimos cien".

    `video_ids` narrows the judged set to those videos before the limit is
    applied — the screen's title-keyword filter, which costs nothing and is what
    lets a person read a channel without paying for the metadata pass. `None`
    means the whole catalogue; an id the catalogue does not hold is ignored by
    intersection rather than refused, because the catalogue can change between
    syncs and the `preselection` artifact records the exact ids evaluated
    anyway, so the filter's effect is on the record either way.
    """
    wanted = None if video_ids is None else set(video_ids)
    evaluated = [
        v
        for v in videos
        if v.live_state != "upcoming" and (wanted is None or v.video_id in wanted)
    ][: max(0, limit)]
    longest = sorted(evaluated, key=lambda v: v.duration_s, reverse=True)
    return DiscoveryPlan(
        topic=topic, evaluated=evaluated, read=longest[: max(0, deep_limit)]
    )


def discovery_estimate(settings: Settings, plan: DiscoveryPlan) -> Estimate:
    """The bill for the two passes, stage by stage."""
    stages = [
        _preselect_row(settings, plan),
        _topics_row(settings, plan),
    ]
    stages = [s for s in stages if s is not None]
    lows = [s.usd for s in stages if s.usd is not None]
    highs = [
        (s.usd_high if s.usd_high is not None else s.usd)
        for s in stages
        if s.usd is not None or s.usd_high is not None
    ]
    return Estimate(
        stages=list(stages),
        total_usd=sum(lows) if lows else None,
        total_usd_high=sum(h for h in highs if h is not None) if highs else None,
        price_source=PRICE_SOURCE,
        unpriced_stages=[s.stage for s in stages if s.usd is None],
    )


def _preselect_row(settings: Settings, plan: DiscoveryPlan) -> StageEstimate | None:
    """The metadata pass, priced from the literal strings it will send.

    Exact rather than projected on the input side, which is what makes this the
    cheap half being cheap *provably* rather than by assertion:
    `preselect.payload_for` is the same function the call uses.
    """
    if not plan.evaluated:
        return None
    batches = preselect.batches(plan.evaluated)
    characters = sum(len(preselect.payload_for(plan.topic, b)) for b in batches)
    return _row(
        settings,
        preselect.STAGE,
        settings.gemini.model,
        input_tokens=int(characters / CHARS_PER_TOKEN)
        + len(batches) * preselect.CALL_OVERHEAD,
        # A ceiling rather than a mean: a verdict, a score and one short
        # sentence come to about 55 tokens, and this is 70. Unmeasured, and a
        # ceiling is the honest shape for an unmeasured output.
        output_tokens=len(plan.evaluated) * preselect.OUTPUT_PER_VIDEO,
    )


def _topics_row(settings: Settings, plan: DiscoveryPlan) -> StageEstimate | None:
    """The transcript pass, projected from each video's duration.

    One call per video, so the overhead is paid per video rather than per batch
    — the opposite of the metadata pass, because a transcript is the whole input
    and batching several would make a reading unattributable to its video. That
    is the same argument `extract_semantics` records for one chunk per call.
    """
    if not plan.read:
        return None
    low = sum(
        min(videosource.projected_characters(v.duration_s), topics.TRANSCRIPT_CHARS)
        for v in plan.read
    )
    high = sum(
        min(
            videosource.projected_characters(v.duration_s, high=True),
            topics.TRANSCRIPT_CHARS,
        )
        for v in plan.read
    )
    calls = len(plan.read)
    return _row(
        settings,
        topics.STAGE,
        settings.gemini.model,
        input_tokens=int(low / CHARS_PER_TOKEN) + calls * topics.CALL_OVERHEAD,
        output_tokens=calls * topics.OUTPUT_PER_VIDEO,
        input_tokens_high=int(high / CHARS_PER_TOKEN) + calls * topics.CALL_OVERHEAD,
    )


def _row(
    settings: Settings,
    stage: str,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    input_tokens_high: int | None = None,
) -> StageEstimate:
    """One stage's figures, with reasoning billed as output where it is on.

    The multiplier table is `estimate_for`'s and is consulted the same way: a
    stage with no entry multiplies by one, which is right for both of these —
    they are classification over text that was handed to them, and
    `config.Gemini.stage_thinking` turns reasoning off for both. Reading the
    table anyway is what makes a global `BRAIN_THINKING_BUDGET` reach the quote
    instead of surprising somebody at the bill.
    """
    from ..activities.ingest import THINKING_OUTPUT_MULTIPLIER

    if settings.gemini.thinking_for(stage) != 0:
        output_tokens = int(
            output_tokens * THINKING_OUTPUT_MULTIPLIER.get(stage, 1.0)
        )
    high_in = input_tokens if input_tokens_high is None else input_tokens_high
    return StageEstimate(
        stage=stage,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usd=price_for(model, input_tokens, output_tokens),
        # The *output* is already a ceiling on both stages, so the high end
        # differs from the low end only where the **input** is projected — which
        # is the topic pass, and only there.
        output_tokens_high=output_tokens,
        usd_high=price_for(model, high_in, output_tokens),
    )
