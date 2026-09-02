"""A run row for the two verbs that are not workflows.

Removing a document and activating a version both change what the library can
answer, and until now neither left a trace anywhere: they are request handlers on
the FastAPI plane and thin workflows on the paid one, and `run_kind_check`
allowed neither kind. So "what changed my library, and when" — the one question
an audit trail exists to answer — could not be asked of the catalog at all.

The blocker was the same one questions had until `20260831160000_run_kind_ask`:
`record_cost` and `record_artifact` derive their tenant from the run they hang
off, so no run row means no bookkeeping of any kind, and a run row needs a kind
the constraint allows.

**This lives beside the work rather than in either plane**, for the reason
`removal.py` already gives about its own ordering: the rule belongs in one module
so no caller can get it wrong, and a second implementation in TypeScript would be
a second place to get it wrong.

Two things are deliberately unlike `IngestWorkflow`'s trail:

- **`seq` and `at` are taken here, not handed in.** There is no replay: these are
  ordinary function calls, not workflow code, so `workflow.now()` has no meaning
  and a retry is a new invocation with a new run id. The idempotency the
  workflows need is not needed and would be theatre.
- **Every write is best-effort.** A removal that succeeded and failed to record
  itself is a bookkeeping gap; a removal refused because the catalog blinked is a
  library left holding points and nodes for a document the user asked to be rid
  of. The first is recoverable and the second is what `removal.py` exists to
  avoid. Same rule the artifact and cost writers follow, and for the same reason.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Callable, Iterator

from . import config
from .catalog import Catalog

log = logging.getLogger(__name__)

#: How long to wait on a bookkeeping write before giving up on it.
#:
#: Unpooled and short on purpose: a pool retries a refused connection in the
#: background, so a catalog that is merely down turns each best-effort write into
#: a full-timeout stall rather than one immediate error. Measured once already at
#: 2 seconds against 99 for an activity suite.
RECORD_TIMEOUT = 3.0


def current_workflow_id() -> str | None:
    """The running activity's workflow id, or `None` when there is no activity.

    `activity.info()` raises `RuntimeError: Not in activity context` outside one,
    and these activities are called as plain functions by their own tests — which
    is the point of them being thin. Degrading to `None` lets
    :func:`audited` mint an id instead, which is exactly what the FastAPI plane
    needs anyway.
    """
    from temporalio import activity

    try:
        return activity.info().workflow_id
    except RuntimeError:
        return None


def mint_run_id(prefix: str) -> str:
    """`{prefix}-{13-digit ms}-{8 hex}`, the format both planes already mint.

    Sortable by time rather than random, because the id is also the artifact
    directory name and the `run.id`, and a queue orders by it.
    """
    return f"{prefix}-{int(time.time() * 1000)}-{secrets.token_hex(4)}"


@contextlib.contextmanager
def audited(
    settings: config.Settings,
    *,
    kind: str,
    tenant_id: str,
    run_id: str | None = None,
    document_id: str | None = None,
    version_id: str | None = None,
) -> Iterator[Callable[[str], None]]:
    """Open a run row, record each stage, and close it with an outcome.

    Yields a `step(stage)` callable. `run_id` is the caller's workflow id when
    there is one — the paid plane reaches this through `RemovalWorkflow`, and a
    run row whose id is not the workflow id would be unreachable from
    `GET /runs/{workflowId}`. The FastAPI plane has no workflow, so it mints one.

    An exception propagates: the verb's own failure is the one worth raising, and
    the row is closed `failed` on the way past.
    """
    run_id = run_id or mint_run_id(kind)
    seq = 0
    stage = "starting"

    def _catalog() -> Catalog:
        return Catalog(settings.database_url, pooled=False, timeout=RECORD_TIMEOUT)

    def _try(what: str, fn: Callable[[Catalog], None]) -> None:
        try:
            with _catalog() as catalog:
                fn(catalog)
        except Exception as e:  # noqa: BLE001 — bookkeeping must never fail the verb
            log.warning("could not record %s for %s: %s", what, run_id, e)

    _try(
        "the run",
        lambda c: c.start_run(
            run_id=run_id,
            workflow_id=run_id,
            kind=kind,
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
        ),
    )

    def step(name: str) -> None:
        nonlocal seq, stage
        seq += 1
        stage = name
        at = datetime.now(timezone.utc)
        _try(name, lambda c: c.set_run_stage(run_id, name, seq=seq, at=at))

    try:
        yield step
    except Exception as e:  # noqa: BLE001 — recorded, then re-raised unchanged
        _try(
            "the failure",
            lambda c: c.finish_run(
                run_id,
                "failed",
                error_kind=type(e).__name__,
                error_detail=str(e)[:2000],
                seq=seq + 1,
                at=datetime.now(timezone.utc),
                stage=stage,
            ),
        )
        raise
    _try(
        "the outcome",
        lambda c: c.finish_run(
            run_id,
            "succeeded",
            seq=seq + 1,
            at=datetime.now(timezone.utc),
            stage=stage,
        ),
    )
