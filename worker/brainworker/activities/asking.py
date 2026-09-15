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
from ..catalog import Catalog
from ..pipeline import Spend

log = logging.getLogger(__name__)

#: Bookkeeping writes use an unpooled connection with a short ceiling, for the
#: reason `activities/ingest.py` gives: a pool retries a refused connection in
#: the background, so a catalog that is merely down turns each best-effort write
#: into a full-timeout stall rather than one immediate error.
RECORD_TIMEOUT = 3.0


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


@activity.defn(name="start_question_run")
async def start_question_run(question_id: str, question: Question) -> None:
    """Open the catalog row a question's charges will hang off.

    **A question could not be billed until this existed.** `record_cost` takes no
    tenant: it derives one in the INSERT from the run the charge belongs to, so
    that a charge and its run can never disagree about who owns them. No run row
    therefore meant no cost row, and `SELECT count(*) FROM cost_entry WHERE
    run_id LIKE 'ask-%'` was 0 across the whole catalog while the ledger held $32
    of indexing.

    Opened at the *start* rather than on completion so a question that is still
    running is visible while it runs — a real one once outran the app's 180s
    timeout, and "nothing is happening" is the wrong thing for a screen to show
    about work that is being paid for.

    Best-effort. A question the user asked must not fail because the catalog is
    down; the answer is the product and this is bookkeeping.
    """
    settings = config.load()
    try:
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            catalog.start_run(
                run_id=question_id,
                workflow_id=question_id,
                kind="ask",
                tenant_id=question.tenant_id,
            )
    except Exception as e:
        log.warning("could not open the run row for %s: %s", question_id, e)


@activity.defn(name="record_question_cost")
async def record_question_cost(
    question_id: str, state: str, spend: list[Spend]
) -> None:
    """Write what the question actually spent, and close its row.

    Three stages reach here: `planning`, `ask-embedding` and `answering`. The
    middle one is the query's own vector, which nothing captured before — it is
    four orders of magnitude under the answering call and it is still a charge.

    `state` maps the question's outcome onto the run's: `done` is a success
    whatever the answer said, because `off_corpus` and `insufficient_evidence`
    are answers about the corpus and not failures of the run. Only a question
    that could not be answered at all is `failed`.
    """
    settings = config.load()
    try:
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            for item in spend:
                catalog.record_cost(
                    question_id,
                    stage=item.stage,
                    provider="vertex",
                    model=item.model,
                    input_tokens=item.input_tokens,
                    output_tokens=item.output_tokens,
                    usd=item.usd,
                )
            catalog.finish_run(
                question_id, "succeeded" if state == "done" else "failed"
            )
    except Exception as e:
        # The money was spent whether or not this row landed. Losing the record
        # is bad; losing the answer the user paid for is worse.
        log.warning("could not record what %s spent: %s", question_id, e)
