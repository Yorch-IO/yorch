"""The removal workflow, which is a way in and nothing more.

Everything that matters about removal — projections first, catalog last, so a
crash leaves it retryable — lives in `removal.py` and is tested in
`tests/unit/test_removal.py`. What is under test here is the dispatch and the
error contract.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.workflows.removal import RemovalWorkflow

TASK_QUEUE = "test-removal"
CALLS: list[tuple[str, str, str]] = []


@activity.defn(name="remove_document")
async def remove_document(library_id: str, document_id: str, tenant: str) -> dict:
    CALLS.append(("document", library_id, document_id))
    return {"document_id": document_id, "versions_removed": ["ver_1"], "qdrant_points": 16}


@activity.defn(name="remove_version")
async def remove_version(library_id: str, version_id: str, tenant: str) -> dict:
    CALLS.append(("version", library_id, version_id))
    return {"document_id": None, "versions_removed": [version_id], "qdrant_points": 3}


@activity.defn(name="remove_document")
async def missing_document(library_id: str, document_id: str, tenant: str) -> dict:
    raise ApplicationError(
        f"no existe {document_id!r}",
        "document_not_found",
        type="RemovalError",
        non_retryable=True,
    )


async def _run(env: WorkflowEnvironment, acts, *args) -> dict:
    client: Client = env.client
    async with Worker(client, task_queue=TASK_QUEUE, workflows=[RemovalWorkflow], activities=acts):
        return await client.execute_workflow(
            RemovalWorkflow.run,
            args=list(args),
            id=f"removal-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


async def test_it_dispatches_to_the_activity_the_target_names(env: WorkflowEnvironment):
    CALLS.clear()
    result = await _run(env, [remove_document, remove_version], "document", "lib_a", "doc_1")
    assert result["qdrant_points"] == 16
    result = await _run(env, [remove_document, remove_version], "version", "lib_a", "ver_9")
    assert result["versions_removed"] == ["ver_9"]
    assert CALLS == [("document", "lib_a", "doc_1"), ("version", "lib_a", "ver_9")]


async def test_a_missing_document_fails_immediately_with_its_kind(env: WorkflowEnvironment):
    """Non-retryable on purpose: a document that is not there will not be there
    on the second attempt, and retrying turns an immediate 404 into a minute of
    silence."""
    with pytest.raises(WorkflowFailureError) as caught:
        await _run(env, [missing_document, remove_version], "document", "lib_a", "doc_x")
    cause = caught.value.cause.cause
    assert isinstance(cause, ApplicationError)
    assert cause.details[0] == "document_not_found"
