"""One question, made durable enough for two control planes to share.

The answer used to live in the API process's memory. That was safe only while
exactly one process could ever hold it — `entrypoint.py` runs uvicorn with a
single worker, and `main.py` says so — and a second plane makes that assumption
false. The state moves here, where both planes can read it by workflow id.

**A failed question is a completed workflow.** `ask_failed` on the other plane is
a 200 body carrying `state: "failed"`, not an HTTP error, because the caller
asked a question and deserves an answer about it either way. Letting the workflow
fail would turn that into a Temporal error the poller has to interpret, and would
put a red line in the namespace for something the user can simply retype.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from ..activities.asking import answer_question
    from ..answering.types import Answer, Question

#: Generous, because a real question against the church-history library once
#: outran the app's own 180s timeout — was computed, was billed, and was
#: discarded under a message blaming an unreachable API.
ASK_TIMEOUT = timedelta(minutes=15)


@dataclass
class AskOutcome:
    """What a poller reads. Mirrors the other plane's `/ask/{id}` body."""

    state: str = "running"
    answer: Answer | None = None
    error: dict[str, str] | None = None


@workflow.defn(name="AskWorkflow")
class AskWorkflow:
    def __init__(self) -> None:
        self._outcome = AskOutcome()

    @workflow.query
    def result(self) -> AskOutcome:
        """Answerable from the moment the workflow exists, which is the point."""
        return self._outcome

    @workflow.run
    async def run(self, question: Question) -> AskOutcome:
        try:
            answer = await workflow.execute_activity(
                answer_question,
                question,
                start_to_close_timeout=ASK_TIMEOUT,
                # One attempt. Every one is a paid generation, and the provider
                # adapter already retries the transport failures worth retrying;
                # a second attempt here bills the user twice for one question.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as e:
            cause = e.cause
            kind = "ask_failed"
            message = str(cause or e)
            if isinstance(cause, ApplicationError) and cause.details:
                kind = str(cause.details[0])
                message = cause.message
            self._outcome = AskOutcome(state="failed", error={"kind": kind, "message": message})
            return self._outcome

        # Set together, and answer-before-state is not a concern here the way it
        # was in the in-process store: a query reads one immutable object.
        self._outcome = AskOutcome(state="done", answer=answer)
        return self._outcome
