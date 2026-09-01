"""Promoting a version by hand, reachable from a control plane that cannot call
Python.

Thin on purpose, like `workflows/removal.py`. Everything that matters — catalog
first, then graph — lives in `activation.py`, and this exists only to give the
NestJS plane a way in. The FastAPI plane still calls the module directly.

A workflow rather than a plain request handler for the same reason removal is
one over there: the paid plane speaks Temporal, not Python. The act itself is
short and idempotent — an upsert on `document_active_version` and one graph
write — so the retry below is free of consequence.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ..activities.activating import promote_version

#: Two writes, neither of which scans: a catalog transaction and a single node's
#: flag. Generous rather than tuned — the failure this guards against is a store
#: that has stopped answering, not a slow one.
ACTIVATION_TIMEOUT = timedelta(minutes=2)


@workflow.defn(name="ActivationWorkflow")
class ActivationWorkflow:
    @workflow.run
    async def run(
        self, library_id: str, version_id: str, tenant: str
    ) -> dict[str, Any]:
        return await workflow.execute_activity(
            promote_version,
            args=[library_id, version_id, tenant],
            start_to_close_timeout=ACTIVATION_TIMEOUT,
            # Retryable because both writes are idempotent: the catalog row is an
            # upsert and the graph write sets a flag. An `ActivationError` is
            # raised non-retryable by the activity — a version that is not this
            # organisation's will not become so on the second try.
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
