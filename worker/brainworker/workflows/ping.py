"""The walking-skeleton workflow.

It exists to prove the full path end to end — API starts a workflow, Temporal
persists and schedules it, the worker picks it up, an activity reaches Qdrant
and Postgres, the result comes back — before any real pipeline code depends on
that path working.

Note the import style: everything the workflow body touches is imported through
``workflow.unsafe.imports_passed_through()``. Temporal replays workflow code to
rebuild state, so a module with import-time side effects would run them again on
every replay.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ..activities.health import ProbeReport, probe_services


@workflow.defn(name="PingWorkflow")
class PingWorkflow:
    @workflow.run
    async def run(self) -> ProbeReport:
        return await workflow.execute_activity(
            probe_services,
            start_to_close_timeout=timedelta(seconds=30),
            # A probe is a point-in-time reading. Retrying it past a few
            # attempts would report the state of a later moment than the one
            # the caller asked about, so the ceiling is deliberately low.
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
