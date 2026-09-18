"""What a transformation will cost, quoted before anything is spent.

`activities/ingest.py::estimate_for` is the shape this follows, down to the
inner `add()` that applies the thinking multiplier and the spread. Three things
about this particular estimate are worth reading before changing a constant.

**The uncertain quantity here is the call count, not the output length.** Every
other estimate in this product knows how many calls it will make — one per
paragraph, one per chunk — and is unsure how long each answer is. This one is
unsure how many chapters there will be, because the outline is planned *after*
the gate. So the range's two ends are a chapter count rather than a token
length, and the low end prices the projected count while the high end prices the
cap. That cap is not a prediction: `outline.validate` **enforces** it, and a
proposal exceeding it is refused and refined. The estimate is a contract the
planner is held to, which is `channel/estimate.py`'s own move — quote the
longest candidate rather than the likeliest, because *"choosing the shortest
would quote a bill the run cannot come in under."*

**`transform-compose` gets no `THINKING_OUTPUT_MULTIPLIER` entry, deliberately,
and the other two now do.** The composition ratio is measured against real
output tokens, so the reasoning is already inside it and a multiplier on top
would compound two margins — the mistake
`THINKING_OUTPUT_MULTIPLIER["semantics"]` records against itself. The genre
analysis and the outline proposals had no such measurement when they were
written and no multiplier either, which is how both came in **under** the quote
on the first real run: the forbidden direction.

**Re-measured 2026-09-18 against the first production transformation.** One
document — a 1994 sermon transcript, 61,646 characters, recast as an essay in
three chapters — so these are one measurement and not a corpus, and the next
one may move them again. What it billed against what this quoted:

| stage | quoted | billed | |
|---|---|---|---|
| `transform-genre` | $0.0075 | **$0.010413** | under by 1.4x |
| `transform-plan` | $0.0285 – $0.0305 | **$0.057599** | under by 1.9x |
| `transform-compose` | $1.0007 – $1.6110 | $0.143997 | over by 6.9x |
| **total** | **$1.0371 – $1.6494** | **$0.212318** | over by 4.9x |

Both failures at once, and they are different failures. Under-reporting is the
one this product's rules forbid outright — a user approved a figure smaller than
the bill. Over-reporting by five times is the other real harm the range exists
to bound: it misleads somebody into declining affordable work exactly as much as
the reverse misleads them into approving expensive work.

The numbers behind the new constants, all from that run: the work came to
**0.213** of the source's characters, its output cost **0.976 tokens per
character written**, and the outline took all three attempts at ~1,810 output
tokens each against the 270 this quoted. Every constant is set to over-report
that run by a margin and each says by how much.
"""

from __future__ import annotations

import math

from ..pipeline import Estimate, StageEstimate
from .genres import Genre

#: Reasoning is billed as output and `candidates_token_count` excludes it, so a
#: stage that reasons and is quoted from its visible answer under-reports —
#: which is what `transform-genre` and `transform-plan` both did on the first
#: production run, by 1.4x and 1.9x. Their own entries, not
#: `activities/ingest.py`'s: that map is keyed by *its* stage names and these
#: two are not in it.
#:
#: 6.0 is correction's and profile's measured value, borrowed rather than
#: measured here — the observed ratios are 4.6x and 6.7x, so it sits between
#: them and over-reports the smaller. `transform-compose` is deliberately absent:
#: its ratio is measured against real output tokens and already contains the
#: reasoning.
THINKING_OUTPUT_MULTIPLIER: dict[str, float] = {
    "transform-genre": 6.0,
    "transform-plan": 6.0,
}

#: Output characters per source character, before the genre's own ratio.
#:
#: **Measured at 0.236 on the first production run** (13,117 characters of essay
#: from 61,646 of sermon, divided by that genre's 0.9). Set to 0.35, which
#: over-reports that run by 1.5x — the margin is the point, and the previous
#: value of 1.0 was a guess that over-reported it by more than four.
#:
#: One document and one genre. A commentary or a treatise expands where an essay
#: selects, which is what `Genre.expansion` is for, and none of those ratios has
#: been measured at all.
OUTPUT_PER_CHAR = 0.35

#: Output tokens per output character. Includes reasoning; see the module
#: docstring for why there is no multiplier on top of it.
#:
#: **Measured at 0.976** on the same run — 12,803 output tokens for 13,117
#: characters of Spanish prose. The old 1.8 was borrowed from an answering
#: turn's *token* ratio, which is a different quantity. 1.0, which is the
#: measurement rounded up rather than a second margin on top of one.
OUTPUT_TOKENS_PER_CHAR = 1.0

#: Tokens one composition call costs before any document text: the three-layer
#: system prompt, the schema, the outline block and the continuity block.
#: Measured on the composed prompt rather than guessed — `compose_transform_system`
#: is about 6,000 characters for a two-purpose novel, and the outline and
#: continuity blocks add roughly a thousand more.
COMPOSE_CALL_OVERHEAD = 2_200

#: And what one evidence fragment adds to that call.
EVIDENCE_TOKENS_PER_FRAGMENT = 460

#: Fragments a research query returns, at the default effort level. Equal to
#: `BUDGETS["standard"].top_k`, and derived rather than restated so the two
#: cannot drift.
def _fragments_per_query() -> int:
    from ..answering.effort import BUDGETS, DEFAULT_EFFORT

    return BUDGETS[DEFAULT_EFFORT].top_k


#: The genre-detection call: the opening of the document in, a genre name out.
GENRE_INPUT_CHARS = 8_000
#: **Measured at 927 output tokens**, against the 120 this quoted — the stage is
#: reasoning-dominated and the visible answer is three short fields. The base
#: stays near the visible answer and `THINKING_OUTPUT_MULTIPLIER` carries the
#: rest, which is the shape every other reasoning stage here uses.
GENRE_OUTPUT_TOKENS = 200

#: One outline proposal: the source's chapter table plus the excerpt in, a
#: chapter list out. Priced at the **worst case** of three attempts, the way
#: `PROFILE_CALL_INPUT * PROFILE_MAX_ATTEMPTS` is, because a refine round is a
#: call and an estimate that assumed the first proposal validated would
#: under-report exactly when the document is hardest.
PLAN_INPUT_CHARS = 10_000
#: **Measured at ~1,810 output tokens per proposal** for a three-chapter outline
#: — about 600 per chapter, of which the visible JSON is a fraction and the rest
#: is reasoning. 120 here with the multiplier below lands at 2,160 per call,
#: which over-reports that run by 1.2x.
PLAN_OUTPUT_TOKENS_PER_CHAPTER = 120
#: And all three were used on the first real run, which is why this was always
#: priced at the worst case rather than at the likeliest.
PLAN_MAX_ATTEMPTS = 3

#: How much wider the enforced chapter cap is than the projected count. The
#: range's whole width, and the reason the low end can stay within reach of a
#: typical document while the high end covers the worst.
CHAPTER_SPREAD = 1.25

#: Fraction of chapters expected to need their one revision. **A guess**, and
#: the one most likely to be wrong in the expensive direction: a corpus whose
#: library disagrees with its documents would revise far more.
REVISION_RATE = 0.25


def projected_chapters(source_chapters: int, characters: int, genre: Genre) -> int:
    """How many chapters the work is quoted for.

    From the source's own chapter count, which is free from `chunks.jsonl`,
    scaled by the genre's ratio — and floored by how many chapters the material
    *has* to be divided into, because a chapter's source material is capped at
    what one call can be written from. A document with one detected chapter and
    200,000 characters is not a one-chapter work, and the fallback path would
    split it whatever the ratio said.
    """
    from .types import MAX_CHAPTER_SOURCE_CHARS, MAX_CHAPTERS

    by_ratio = math.ceil(max(1, source_chapters) * genre.chapter_ratio)
    by_size = math.ceil(characters / MAX_CHAPTER_SOURCE_CHARS) if characters else 1
    return max(1, min(MAX_CHAPTERS, max(by_ratio, by_size)))


def chapter_cap(chapters: int) -> int:
    """The ceiling `outline.validate` enforces and the high end prices."""
    from .types import MAX_CHAPTERS

    return max(1, min(MAX_CHAPTERS, math.ceil(chapters * CHAPTER_SPREAD)))


def transform_estimate(
    *,
    characters: int,
    source_chapters: int,
    genre: Genre,
    purposes: list[str] | tuple[str, ...],
    research_budget: int,
) -> Estimate:
    """Project the whole run, per stage, from counts that cost nothing to get."""
    from ..activities.ingest import CHARS_PER_TOKEN, PRICE_SOURCE, price_for
    from ..config import load

    settings = load()
    model = settings.gemini.model
    embedding_model = settings.gemini.embedding_model

    low = projected_chapters(source_chapters, characters, genre)
    high = chapter_cap(low)

    stages: list[StageEstimate] = []

    def add(
        stage: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        output_tokens_high: int | None = None,
    ) -> None:
        multiplier = THINKING_OUTPUT_MULTIPLIER.get(stage, 1.0)
        if settings.gemini.thinking_for(stage) == 0:
            multiplier = 1.0
        out_low = int(output_tokens * multiplier)
        out_high = int((output_tokens_high or output_tokens) * multiplier)
        stages.append(
            StageEstimate(
                stage=stage,
                model=model_id,
                input_tokens=input_tokens,
                output_tokens=out_low,
                usd=price_for(model_id, input_tokens, out_low),
                output_tokens_high=out_high,
                usd_high=price_for(model_id, input_tokens, out_high),
            )
        )

    # The probe. Eight query embeddings, and it has already happened by the time
    # this is rendered — it is quoted anyway, because a stage that cost almost
    # nothing and a stage that did not run are different facts, and
    # `ask-embedding` went unbilled for months by being too small to notice.
    from .budget import PROBE_CHARS, PROBE_SAMPLE

    probe_tokens = int(PROBE_SAMPLE * PROBE_CHARS / CHARS_PER_TOKEN)
    add("transform-probe", embedding_model, probe_tokens, 0)

    add(
        "transform-genre",
        model,
        int(min(characters, GENRE_INPUT_CHARS) / CHARS_PER_TOKEN) + COMPOSE_CALL_OVERHEAD,
        GENRE_OUTPUT_TOKENS,
    )

    plan_input = (
        int(PLAN_INPUT_CHARS / CHARS_PER_TOKEN) + COMPOSE_CALL_OVERHEAD
    ) * PLAN_MAX_ATTEMPTS
    add(
        "transform-plan",
        model,
        plan_input,
        low * PLAN_OUTPUT_TOKENS_PER_CHAPTER * PLAN_MAX_ATTEMPTS,
        high * PLAN_OUTPUT_TOKENS_PER_CHAPTER * PLAN_MAX_ATTEMPTS,
    )

    if research_budget and purposes:
        # **Not `EVAL_QUERY_TOKENS`.** That constant is sized for a question
        # somebody typed; a research query here is a slice of the chapter's own
        # source text, up to `QUERY_CHARS`. Quoting the short one put this line
        # **under** the bill on the first production run — 6 queries billed 593
        # input tokens against the 360 this quoted, about 99 each — which is the
        # forbidden direction however small the figure. The cap over-reports it
        # at 139.
        from .research import QUERY_CHARS

        add(
            "transform-research",
            embedding_model,
            int(research_budget * QUERY_CHARS / CHARS_PER_TOKEN),
            0,
        )

    # The bill. Input is the chapter's own source material plus whatever
    # research reached its prompt; output is the prose, scaled by the genre.
    fragments = _fragments_per_query() if research_budget else 0
    per_chapter_evidence = (
        int(research_budget / max(1, low)) * fragments * EVIDENCE_TOKENS_PER_FRAGMENT
    )
    source_tokens = int(characters / CHARS_PER_TOKEN)
    compose_input = (
        source_tokens
        + per_chapter_evidence * low
        + COMPOSE_CALL_OVERHEAD * low
    )
    prose_chars = characters * OUTPUT_PER_CHAR * genre.expansion
    compose_output = int(prose_chars * OUTPUT_TOKENS_PER_CHAR)
    add(
        "transform-compose",
        model,
        compose_input,
        compose_output,
        # The high end carries both the wider chapter count — more calls, each
        # paying the overhead again — and the revision rate.
        int(compose_output * (high / max(1, low)) * (1.0 + REVISION_RATE)),
    )

    priced = [s.usd for s in stages if s.usd is not None]
    priced_high = [
        (s.usd_high if s.usd_high is not None else s.usd)
        for s in stages
        if s.usd is not None
    ]
    return Estimate(
        stages=stages,
        total_usd=sum(priced) if priced else None,
        price_source=PRICE_SOURCE,
        unpriced_stages=[s.stage for s in stages if s.usd is None],
        total_usd_high=sum(priced_high) if priced_high else None,
    )
