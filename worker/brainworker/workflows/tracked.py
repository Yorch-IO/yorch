"""The run trail, shared by every workflow that keeps one.

Lifted out of `workflows/channel.py` when a third workflow needed it, and lifted
rather than copied for the reason the class exists at all: it holds two rules
that are invisible from any one call site, and a third copy is a third place for
one of them to be got wrong.

* **`seq` is a counter on the workflow object and `at` is `workflow.now()`**, so
  a retried transition carries the number *and the timestamp* it carried the
  first time and `ON CONFLICT (run_id, seq) DO NOTHING` drops it. `now()` in SQL
  would move under a retry and silently stretch the previous stage's measured
  duration.
* **The run row comes first.** `_insert_event` derives its tenant from the run
  row and *silently drops* an event written before it exists — which is why
  `IngestWorkflow` buffers its first two transitions and flushes them once the
  row is there. A workflow that calls `_open` as its first activity needs no
  buffer, and every workflow built on this one does.

`kind` is a parameter rather than a constant, which is the one thing that had to
change in the move: the original hardcoded `"channel"`.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from ..activities import ingest as act
    from ..pipeline import RunOpen
    from .video import failure_of

#: A catalog write. Short, because it is bookkeeping and a stage must not wait
#: on it, and retried three times because losing the trail loses the only record
#: that survives the namespace's retention period.
WRITE_TIMEOUT = timedelta(minutes=2)
_RETRY = RetryPolicy(maximum_attempts=3)


class Tracked:
    """A workflow that records where it is, and what it did on the way."""

    #: Overridden by each workflow. `run.kind` is not bookkeeping: it is how a
    #: client knows which gate shape to expect, and a run listed under the wrong
    #: kind would be polled at the wrong route and silently decoded into the
    #: wrong type.
    kind: str = ""

    def __init__(self) -> None:
        self._stage = "starting"
        self._seq = 0

    @workflow.query
    def stage(self) -> str:
        return self._stage

    async def _open(
        self, run_id: str, *, tenant_id: str, library_id: str, label: str
    ) -> None:
        await workflow.execute_activity(
            act.open_run,
            RunOpen(
                run_id=run_id,
                workflow_id=workflow.info().workflow_id,
                kind=self.kind,
                tenant_id=tenant_id,
                library_id=library_id,
                label=label,
            ),
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _enter(
        self, run_id: str, stage: str, state: str = "running", detail: str | None = None
    ) -> None:
        self._stage = stage
        self._seq += 1
        await workflow.execute_activity(
            act.set_run_stage,
            args=[run_id, stage, state, self._seq, workflow.now(), detail],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _finish(
        self,
        run_id: str,
        state: str,
        error_kind: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        self._seq += 1
        await workflow.execute_activity(
            act.record_run_outcome,
            args=[
                run_id, state, error_kind, error_detail,
                self._seq, workflow.now(), self._stage,
            ],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _record_failure(self, run_id: str, error: ActivityError) -> None:
        kind, detail = failure_of(error)
        try:
            await self._finish(run_id, "failed", kind, detail[:2000])
        except Exception:  # noqa: BLE001
            workflow.logger.warning("could not record run failure for %s", run_id)
