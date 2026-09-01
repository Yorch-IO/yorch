"""The activation workflow, which is a way in and nothing more.

Everything that matters about activation — catalog first, then graph — lives in
`activation.py`. What is under test here is the wiring and the error contract,
and in particular **that the tenant survives the trip**, because a tenant that
does not arrive is the whole reason this exists: the parameter defaulted to the
legacy organisation until 2026-08-31, so the one operation that can publish an
index already paid for answered 404 for everybody else.

The doubles below are typed like the real activity on purpose. Temporal maps
payloads onto parameters by arity, so an untyped `*args` double accepts a call
the real converter cannot make — which is how five workflow tests once passed
against a workflow that could not run.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.workflows.activation import ActivationWorkflow

TASK_QUEUE = "test-activation"
CALLS: list[tuple[str, str, str]] = []


@activity.defn(name="promote_version")
async def promote_version(library_id: str, version_id: str, tenant: str) -> dict:
    CALLS.append((library_id, version_id, tenant))
    return {"version_id": version_id, "documents": ["doc_1", "doc_2"]}


@activity.defn(name="promote_version")
async def not_this_organisations(library_id: str, version_id: str, tenant: str) -> dict:
    raise ApplicationError(
        f"no existe {version_id!r} en {library_id!r}",
        "version_not_found",
        type="ActivationError",
        non_retryable=True,
    )


async def _run(env: WorkflowEnvironment, acts, *args) -> dict:
    client: Client = env.client
    async with Worker(
        client, task_queue=TASK_QUEUE, workflows=[ActivationWorkflow], activities=acts
    ):
        return await client.execute_workflow(
            ActivationWorkflow.run,
            args=list(args),
            id=f"activate-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


async def test_the_tenant_reaches_the_activity_that_scopes_on_it(
    env: WorkflowEnvironment,
):
    """The catalog lookup inside `activate_version` is the authorization
    predicate — a salted id is a value its own members hold, so the id proves
    nothing. A tenant lost anywhere on this path is either a refusal for the
    owner or a promotion for somebody else."""
    CALLS.clear()
    result = await _run(
        env, [promote_version], "lib_teologia", "ver_0b71", "tnt_f489b4a6"
    )
    assert result["documents"] == ["doc_1", "doc_2"]
    assert CALLS == [("lib_teologia", "ver_0b71", "tnt_f489b4a6")]


async def test_every_document_holding_the_version_is_returned(
    env: WorkflowEnvironment,
):
    """One version can be held by several documents — byte-identical files at two
    paths are two documents and one version — and promoting it for one while the
    other still points at something older would make the same bytes answer
    differently depending on which copy was asked about."""
    CALLS.clear()
    result = await _run(env, [promote_version], "lib_teologia", "ver_0b71", "tnt_1")
    assert len(result["documents"]) == 2


async def test_another_organisations_version_fails_immediately_with_its_kind(
    env: WorkflowEnvironment,
):
    """Non-retryable, and a 404 rather than a 403: the answer is the same
    whether the version does not exist or is not yours, and saying which would
    tell a stranger that an id they guessed is real."""
    with pytest.raises(WorkflowFailureError) as caught:
        await _run(env, [not_this_organisations], "lib_teologia", "ver_x", "tnt_other")
    cause = caught.value.cause.cause
    assert isinstance(cause, ApplicationError)
    assert cause.details[0] == "version_not_found"
