"""The question workflow, which is where a question's state now lives.

Activities are mocked: what is under test is the workflow's own behaviour —
that a failure becomes a *completed* workflow carrying an error rather than a
failed one, and that the query is answerable throughout — not whether Vertex
answers.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.answering.types import Answer, Question
from brainworker.workflows.ask import AskOutcome, AskWorkflow

TASK_QUEUE = "test-ask"


@activity.defn(name="answer_question")
async def answers(question: Question) -> Answer:
    return Answer(state="answered", text=f"respuesta a {question.text}", reason="")


@activity.defn(name="answer_question")
async def refuses(question: Question) -> Answer:
    raise ApplicationError(
        "Vertex AI quota exhausted",
        "provider_quota",
        type="ProviderError",
        non_retryable=True,
    )


@activity.defn(name="answer_question")
async def breaks(question: Question) -> Answer:
    raise RuntimeError("algo se rompió sin un kind")


async def _run(env: WorkflowEnvironment, act) -> AskOutcome:
    client: Client = env.client
    async with Worker(client, task_queue=TASK_QUEUE, workflows=[AskWorkflow], activities=[act]):
        return await client.execute_workflow(
            AskWorkflow.run,
            Question(library_id="lib_a", text="¿el arrianismo?"),
            id=f"ask-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


async def test_a_answered_question_is_collectable(env: WorkflowEnvironment):
    outcome = await _run(env, answers)
    assert outcome.state == "done"
    assert outcome.answer is not None and outcome.answer.text.startswith("respuesta a")
    assert outcome.error is None


async def test_a_provider_failure_completes_the_workflow_carrying_its_kind(
    env: WorkflowEnvironment,
):
    """A failed question is not a failed workflow.

    The other plane answers `state: "failed"` in a 200 body, because the caller
    asked a question and deserves an answer about it either way. Letting the
    workflow fail would put a red line in the namespace for something the user
    can retype, and would make the poller interpret a Temporal error.
    """
    outcome = await _run(env, refuses)
    assert outcome.state == "failed"
    assert outcome.answer is None
    # The kind survives the Temporal boundary — it is what the desktop app keys
    # its advice on, and "no quota" and "no project" have different fixes.
    assert outcome.error == {"kind": "provider_quota", "message": "Vertex AI quota exhausted"}


async def test_a_failure_with_no_kind_still_reports_one(env: WorkflowEnvironment):
    """`ask_failed` is the fallback, so the UI always has something to key on."""
    outcome = await _run(env, breaks)
    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error["kind"] == "ask_failed"


async def test_the_query_answers_before_the_activity_does(env: WorkflowEnvironment):
    """Polling has to work from the moment the id exists, not only at the end."""
    client: Client = env.client

    @activity.defn(name="answer_question")
    async def slow(question: Question) -> Answer:
        import asyncio

        await asyncio.sleep(30)
        return Answer(state="answered", text="tarde", reason="")

    async with Worker(client, task_queue=TASK_QUEUE, workflows=[AskWorkflow], activities=[slow]):
        handle = await client.start_workflow(
            AskWorkflow.run,
            Question(library_id="lib_a", text="¿?"),
            id=f"ask-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        assert (await handle.query(AskWorkflow.result)).state == "running"
        assert (await handle.result()).state == "done"
