"""The paid provider probe.

Its own workflow rather than a leg of `PingWorkflow`, because the two answer
different questions and only one of them costs money. `PingWorkflow` proves the
stack is wired; this proves Vertex will accept a real request, and it spends one
short embedding to do it.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ..activities.health import probe_provider


@workflow.defn(name="ProviderProbeWorkflow")
class ProviderProbeWorkflow:
    @workflow.run
    async def run(self) -> dict[str, str]:
        return await workflow.execute_activity(
            probe_provider,
            start_to_close_timeout=timedelta(seconds=60),
            # A probe is a point-in-time reading, and this one bills for each
            # attempt. Two at most, and only for the transport failures the
            # activity marks retryable.
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
