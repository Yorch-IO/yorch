"""Promoting a version by hand, as an activity.

Thin, for the reason `removing.py` gives about the ordering *it* wraps. The rule
here is the mirror image — **catalog first, then graph**, because a crash between
them leaves a stale `active` flag that re-projection repairs, while the reverse
leaves a version answerable in a graph the catalog does not consider indexed and
nothing repairs that on its own. That rule lives in `activation.py` next to
`activities/ingest.activate_version`, which makes the same choice, and a second
implementation in TypeScript would have been a third place for the two to drift.

So the work stays where it is and gains a way in, exactly as removal did: the
FastAPI plane calls the module directly, the NestJS plane cannot call Python and
comes through here.

Named `promote_version` rather than `activate_version` because that name is
already an activity — the one the ingest workflow runs as its last step. Two
activities cannot share a registered name, and the collision would surface as a
worker accepting a task and failing it, which reads like a Temporal fault rather
than a duplicate line in `ACTIVITIES`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import activation

log = logging.getLogger(__name__)


def _refuse(e: activation.ActivationError) -> ApplicationError:
    """Carry the `kind` across the Temporal boundary.

    `kind` is what `app/src/lib/api.ts` keys its guidance on, so flattening it
    into a message would cost the UI its ability to offer a fix. `details[0]` is
    where the caller reads it back.

    `non_retryable` because `version_not_found` is a statement about what exists
    — and, since the catalog lookup is scoped, also about who is asking. Neither
    answer changes on a second attempt.
    """
    return ApplicationError(str(e), e.kind, type="ActivationError", non_retryable=True)


@activity.defn(name="promote_version")
async def promote_version(
    library_id: str, version_id: str, tenant: str
) -> dict[str, Any]:
    """Publish an index that is already paid for.

    The tenant is positional and has no default, unlike `remove_document`'s. A
    defaulted one is what made this operation unreachable for every organisation
    but the legacy one, and Temporal maps payloads onto parameters by arity — so
    a default here is also a parameter a caller can silently fail to fill.
    """
    try:
        # Keyword-only on the other side, so a lambda rather than positional
        # arguments through `to_thread`.
        promoted = await asyncio.to_thread(
            lambda: activation.activate_version(
                library_id, version_id, tenant_id=tenant
            )
        )
    except activation.ActivationError as e:
        raise _refuse(e) from e
    log.info("promoted %s in %s for %s", version_id, library_id, tenant)
    return promoted.as_dict()
