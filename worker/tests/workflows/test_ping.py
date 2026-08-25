"""Walking-skeleton workflow tests.

Activities are mocked: what is under test is the workflow's own logic and its
ability to run to completion under replay, not whether Qdrant is reachable.
Reachability is what the real activity measures, and it needs a real stack —
that is the e2e smoke, not this.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.activities.health import ProbeReport, ServiceProbe
from brainworker.workflows.ping import PingWorkflow

TASK_QUEUE = "test-ping"


@activity.defn(name="probe_services")
async def probe_all_healthy() -> ProbeReport:
    return ProbeReport(
        probes=[
            ServiceProbe("qdrant", True, "ready, 0 collection(s)"),
            ServiceProbe("postgres", True, "database brain"),
        ]
    )


@activity.defn(name="probe_services")
async def probe_qdrant_down() -> ProbeReport:
    return ProbeReport(
        probes=[
            ServiceProbe("qdrant", False, "ConnectError: connection refused"),
            ServiceProbe("postgres", True, "database brain"),
        ]
    )


async def _run(env: WorkflowEnvironment, probe) -> ProbeReport:
    client: Client = env.client
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[PingWorkflow],
        activities=[probe],
    ):
        return await client.execute_workflow(
            PingWorkflow.run,
            id=f"ping-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


async def test_reports_every_service_when_all_are_healthy(env: WorkflowEnvironment):
    report = await _run(env, probe_all_healthy)
    assert report.ok
    assert {p.service for p in report.probes} == {"qdrant", "postgres"}


async def test_one_unreachable_service_does_not_fail_the_workflow(env: WorkflowEnvironment):
    """A probe reports; it does not raise. A failed workflow would tell the
    Stack screen nothing about *which* service is down, which is the only thing
    it needs to know."""
    report = await _run(env, probe_qdrant_down)
    assert not report.ok
    down = [p for p in report.probes if not p.ok]
    assert [p.service for p in down] == ["qdrant"]
    assert "connection refused" in down[0].detail
