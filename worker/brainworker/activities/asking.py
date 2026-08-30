"""Answering a question, as an activity.

`answering/service.py` says a question is "deliberately synchronous and not a
Temporal workflow", and the argument it gives is sound: a question is
interactive and short-lived, and durability buys nothing when the remedy for a
failure is to ask again. What it did *not* anticipate is a second control plane.

The state has to live somewhere both planes can reach. It used to live in the
API process's own memory — an ``OrderedDict`` capped at 64 with a one-hour TTL,
reaped only on ``POST /ask`` — and that was correct only because ``entrypoint.py``
runs uvicorn with a single worker; passing ``workers=N`` there would have sent a
poll to a process that never saw the question. Two planes make that assumption
false by construction.

So the work moves here and the state becomes the workflow's. Durability is a
side effect rather than the motive, and a welcome one: a question that outran
the app's 180s timeout was computed, billed, and thrown away under an error
message pointing at an unreachable API.
"""

from __future__ import annotations

import asyncio
import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import config
from ..answering import Question, ask
from ..answering.types import Answer

log = logging.getLogger(__name__)


@activity.defn(name="answer_question")
async def answer_question(question: Question) -> Answer:
    """Plan, retrieve and compose one answer.

    Not retried by the workflow that calls it, on purpose: every attempt is a
    paid generation, and `providers/gemini.py` already retries the transport
    failures worth retrying. A second attempt here would bill the user twice for
    one question.
    """
    settings = config.load()
    if not settings.gemini.configured:
        # Refused before anything is planned. The same refusal the API makes at
        # the edge, repeated here because an activity may be reached by a
        # workflow started before the project was set.
        raise ApplicationError(
            "Falta BRAIN_GEMINI_PROJECT_ID: no se puede preguntar.",
            "provider_unconfigured",
            type="ProviderError",
            non_retryable=True,
        )
    # `ask` is synchronous and network-bound; a thread keeps the activity's own
    # event loop free, which is what lets Temporal heartbeat and cancel it.
    return await asyncio.to_thread(ask, settings, question)
