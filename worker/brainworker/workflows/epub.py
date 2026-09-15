"""Building a book for an already-indexed version, as a workflow.

Thin on purpose, like `workflows/activation.py` and `workflows/removal.py`.
Everything that matters — which run holds the chunks, whether the metadata is
worth asking for, catalog first and then the file — lives in `booking.py`, and
this exists to give both planes one way in.

**Both planes**, which is the difference from activation, where the FastAPI
plane calls the module directly. This writes an artifact and can spend, and
every artifact writer and every charge in this codebase lives in an activity so
that `record_artifact` and `record_cost` can derive the tenant from the run row
they hang off. A request handler doing it would be a second place for that rule
to live.

One attempt beyond the first, and no more. The build is idempotent — the same
version, the same chunks and the same identifier produce the same archive, and
`write_bytes` replaces rather than appends — but the metadata call inside it is
not free, and a policy that retried freely would re-buy it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ..activities.exporting import export_version_epub

#: A file read, a render and a file write, plus at most one bounded generation
#: call. Generous rather than tuned: the failure this guards against is a store
#: that has stopped answering, not a slow one. A 600-chunk book renders in well
#: under a second.
EXPORT_TIMEOUT = timedelta(minutes=10)


@workflow.defn(name="EpubWorkflow")
class EpubWorkflow:
    @workflow.run
    async def run(
        self, library_id: str, document_id: str, version_id: str, tenant: str
    ) -> dict[str, Any]:
        return await workflow.execute_activity(
            export_version_epub,
            args=[library_id, document_id, version_id, tenant],
            start_to_close_timeout=EXPORT_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
