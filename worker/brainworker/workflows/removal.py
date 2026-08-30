"""Permanent removal, reachable from a control plane that cannot call Python.

Thin on purpose. Everything that matters — projections first, catalog last, so a
crash leaves the operation retryable rather than leaving points and nodes
nothing can find again — lives in `removal.py`, and this exists only to give the
NestJS plane a way in. The FastAPI plane still calls the module directly.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ..activities.removing import remove_document, remove_version
    from ..graph.schema import LEGACY_TENANT_ID

#: Removal touches three stores in sequence; the Qdrant delete over a large
#: filter is the slow leg.
REMOVAL_TIMEOUT = timedelta(minutes=10)


@workflow.defn(name="RemovalWorkflow")
class RemovalWorkflow:
    @workflow.run
    async def run(
        self,
        target: Literal["document", "version"],
        library_id: str,
        target_id: str,
        tenant: str = LEGACY_TENANT_ID,
    ) -> dict[str, Any]:
        activity = remove_document if target == "document" else remove_version
        return await workflow.execute_activity(
            activity,
            args=[library_id, target_id, tenant],
            start_to_close_timeout=REMOVAL_TIMEOUT,
            # Retryable because the order it runs in was chosen to make it so.
            # A `RemovalError` is raised non-retryable by the activity: a
            # document that is not there will not be there on the second try.
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
