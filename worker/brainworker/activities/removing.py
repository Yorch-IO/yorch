"""Permanent removal, as an activity.

The ordering this wraps is the whole point of not reimplementing it. `removal.py`
writes the projections first and the catalog last, because the catalog is the
source of truth and the other two are derived from it: a crash after the catalog
row is gone leaves points and nodes that nothing can find again to retry. That
rule lives in one module so no caller can get it wrong, and porting it to
TypeScript would have been a second place to get it wrong.

Removal is a request handler on the FastAPI plane rather than a workflow, and
its docstring gives the reason — it is short-lived and the reverse order leaves
it retryable. That reasoning still holds; what changed is that the *other* plane
cannot call Python. So the work stays exactly where it is and gains a way in.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import audit, config, removal
from ..graph.schema import LEGACY_TENANT_ID

log = logging.getLogger(__name__)


def _refuse(e: removal.RemovalError) -> ApplicationError:
    """Carry the `kind` across the Temporal boundary.

    `kind` is what `app/src/lib/api.ts` keys its guidance on, so flattening it
    into a message would cost the UI its ability to offer a fix. `details[0]`
    is where the caller reads it back.

    `non_retryable` because every RemovalError is a statement about what exists:
    a document that is not there will not be there on the second attempt, and
    retrying would turn an immediate 404 into a minute of silence.
    """
    return ApplicationError(str(e), e.kind, type="RemovalError", non_retryable=True)


@activity.defn(name="remove_document")
async def remove_document(
    library_id: str, document_id: str, tenant: str = LEGACY_TENANT_ID
) -> dict[str, Any]:
    settings = config.load()
    workflow_id = audit.current_workflow_id()
    try:
        # Keyword-only on the other side, so a lambda rather than positional
        # arguments through `to_thread`.
        removed = await asyncio.to_thread(
            lambda: removal.remove_document(
                settings,
                library_id=library_id,
                document_id=document_id,
                tenant=tenant,
                # The workflow's own id, so `GET /runs/{workflowId}` finds the
                # row this removal writes rather than a second one nothing names.
                run_id=workflow_id,
            )
        )
    except removal.RemovalError as e:
        raise _refuse(e) from e
    return removed.as_dict()


@activity.defn(name="remove_version")
async def remove_version(
    library_id: str, version_id: str, tenant: str = LEGACY_TENANT_ID
) -> dict[str, Any]:
    settings = config.load()
    workflow_id = audit.current_workflow_id()
    try:
        removed = await asyncio.to_thread(
            lambda: removal.remove_version(
                settings,
                library_id=library_id,
                version_id=version_id,
                tenant=tenant,
                run_id=workflow_id,
            )
        )
    except removal.RemovalError as e:
        raise _refuse(e) from e
    return removed.as_dict()
