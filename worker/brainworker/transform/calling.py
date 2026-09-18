"""One place that makes a generation call, so both graphs charge the same way.

Two things here are load-bearing and neither is obvious.

**A fresh `VertexAdapter` per call.** `adapter.usage` accumulates, so an adapter
reused across a graph's four calls would make every `Spend` after the first
report the sum of everything before it — a ledger that over-reports by
construction and looks, on a two-call stage, exactly like a stage that cost
twice what it did. `synthesis.py` gets away with one adapter because it makes
one call. These graphs do not.

**An output ceiling, and no reasoning budget.** `MAX_OUTPUT_TOKENS` bounds the
*bill*: a call with dynamic thinking can spend the model's whole 65,536-token
ceiling reasoning and return no text at all, measured once at **$0.497373 for
nothing**. It does not bound the reasoning, and no level, constant or schema
field here names a `thinking_budget` — the recorded A/B found a fixed 8192
produced **7.00 citations against the default's 7.75**, because leaving it unset
sends no `ThinkingConfig` and the model chooses per call: a literal does not
raise that dynamic value, it caps it.
"""

from __future__ import annotations

import asyncio
import logging

from ..pipeline import Spend
from ..providers import Provider, VertexAdapter

log = logging.getLogger(__name__)

#: The output ceiling for a composition call.
#:
#: 24,576 rather than `answer.MAX_OUTPUT_TOKENS`'s 16,384, and the difference is
#: the difference between the two jobs: an answer is a few paragraphs and the
#: widest one ever measured here was 3,582 output tokens, while a chapter of a
#: book is the length of a chapter of a book. Both numbers are set the same way
#: — several times the widest real output — so that the ceiling can only ever
#: catch a runaway and never truncate a real one. `_prepare`'s warning against
#: setting `max_output_tokens` is about a number near the output's own length,
#: which this is not.
MAX_OUTPUT_TOKENS = 24_576

#: Planning calls are structural and short. A separate, smaller ceiling, for the
#: same reason: it can only catch a runaway.
PLAN_MAX_OUTPUT_TOKENS = 8_192


def spend_of(provider: Provider, adapter: VertexAdapter, stage: str) -> Spend:
    """What one call cost, priced by exact model id.

    The import is local for the reason `synthesis._spend`'s is: `activities`
    pulls in Temporal, psycopg and the whole pipeline, and this package is meant
    to be importable — and testable — without any of them.
    """
    from ..activities.ingest import price_for

    usage = adapter.usage
    return Spend(
        stage=stage,
        model=provider.settings.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        usd=price_for(provider.settings.model, usage.input_tokens, usage.output_tokens),
    )


async def generate_json(
    provider: Provider,
    prompt: str,
    *,
    system: str,
    schema: dict,
    stage: str,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
) -> tuple[object, Spend]:
    """One structured call, off the event loop, with its own charge.

    `asyncio.to_thread` is not a precaution. Six activities in this worker were
    `async def` around a synchronous network call and held the only event loop
    for the length of a document, which meant the heartbeat they recorded could
    never be *sent* — the call that recorded it was holding the loop that had to
    send it. The measured cost was a worker frozen for 97 minutes and a stage
    billed twice, **$9.4539** of a $10.017265 run.

    Raises whatever the adapter raises, including `TruncatedResponse`. A caller
    that can say something better than "the model returned nothing usable" is
    expected to catch it; one that cannot should let it fail the activity.
    """
    adapter = VertexAdapter(provider)
    raw = await asyncio.to_thread(
        adapter.generate_json,
        prompt,
        system=system,
        schema=schema,
        stage=stage,
        max_output_tokens=max_output_tokens,
    )
    return raw, spend_of(provider, adapter, stage)
