"""Rebuild a version's projections from artifacts it already paid for.

A workflow rather than a request handler, and for a narrower reason than the
ingest gate's. Removal is a plain function because its failure mode is "press it
again"; a rebuild embeds every chunk of a book — thousands of sequential API
calls on a long document — and losing that half way to a closed laptop lid is
precisely what durable execution is for.

One question at the gate, not the ingest report's per-stage table, because only
one stage here can spend. Everything else is a file read and a graph write.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from ..graph.schema import LEGACY_TENANT_ID
    from ..activities import ingest as act
    from ..activities import paid
    from ..activities import rebuild as reb
    from ..pipeline import (
        Chunked,
        Estimate,
        Indexed,
        Preview,
        RebuildInputs,
        RebuildReport,
        RebuildResult,
        Semantics,
        Spend,
        StageOptions,
    )
    from .ingest import (
        Approval,
        GATE_TIMEOUT,
        PAID_TIMEOUT,
        WRITE_TIMEOUT,
        FREE_TIMEOUT,
        _PAID_RETRY,
        _RETRY,
    )


@workflow.defn(name="RebuildWorkflow")
class RebuildWorkflow:
    def __init__(self) -> None:
        self._approval: Approval | None = None
        self._report: RebuildReport | None = None
        self._stage = "starting"

    # -- signals and queries ----------------------------------------------

    @workflow.signal
    def approve(self, approval: Approval) -> None:
        self._approval = approval

    @workflow.query
    def gate_report(self) -> RebuildReport | None:
        return self._report

    @workflow.query
    def stage(self) -> str:
        return self._stage

    # -- run ---------------------------------------------------------------

    @workflow.run
    async def run(
        self, library_id: str, document_id: str, tenant: str = LEGACY_TENANT_ID
    ) -> RebuildResult:
        run_id = workflow.info().workflow_id
        try:
            return await self._run(library_id, document_id, run_id, tenant)
        except ActivityError as e:
            await self._record_failure(run_id, e)
            raise

    async def _run(
        self, library_id: str, document_id: str, run_id: str, tenant: str
    ) -> RebuildResult:
        self._stage = "loading"
        inputs: RebuildInputs = await workflow.execute_activity(
            reb.load_rebuild_inputs,
            args=[library_id, document_id, run_id, workflow.info().workflow_id, tenant],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

        # The estimator is reused rather than reimplemented, restricted to the
        # one stage that can spend. It over-reports on purpose: a user who
        # approved a smaller number than they were billed has been misled, and
        # the reverse has not.
        self._stage = "estimating"
        estimate: Estimate = await workflow.execute_activity(
            act.estimate_cost,
            args=[
                Preview(
                    text=inputs.chunks,
                    chunks=inputs.chunks,
                    chunk_count=inputs.chunks.rows or 0,
                    kinds=[],
                    characters=inputs.characters,
                    chunks_are_final=True,
                ),
                StageOptions(
                    correct=False,
                    embed=True,
                    extract_semantics=False,
                    learn_profile=False,
                    generate_evalset=False,
                ),
                None,
            ],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        self._report = RebuildReport(
            document_id=inputs.registered.document_id,
            version_id=inputs.registered.version_id,
            title=inputs.staged.title,
            chunk_count=inputs.chunks.rows or 0,
            characters=inputs.characters,
            source_run_id=inputs.source_run_id,
            # Stated so the screen can say what a rebuild will *not* restore.
            # Documents indexed before the `semantics` artifact existed have no
            # file to replay, and thinning the graph without saying so would be
            # the worst of the three possible behaviours.
            semantics_available=inputs.semantics is not None,
            estimate=estimate,
        )

        approval = await self._gate(run_id)
        if not approval.approved:
            await self._finish(run_id, "cancelled")
            return RebuildResult(
                run_id=run_id,
                document_id=inputs.registered.document_id,
                version_id=inputs.registered.version_id,
                state="cancelled",
                detail=approval.reason or "no aprobado",
            )

        spent: list[Spend] = []

        self._stage = "embedding"
        indexed: Indexed = await workflow.execute_activity(
            paid.embed_and_index,
            args=[
                run_id,
                library_id,
                inputs.registered,
                inputs.staged,
                Chunked(chunks=inputs.chunks, count=inputs.chunks.rows or 0),
            ],
            start_to_close_timeout=PAID_TIMEOUT,
            retry_policy=_PAID_RETRY,
        )
        spent.append(indexed.spend)

        self._stage = "projecting"
        graph = await workflow.execute_activity(
            act.project_structure,
            args=[
                inputs.request,
                inputs.staged,
                inputs.registered,
                run_id,
                inputs.chunks,
            ],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

        replayed = False
        if inputs.semantics is not None:
            self._stage = "replaying semantics"
            semantics: Semantics = await workflow.execute_activity(
                reb.replay_semantics,
                args=[inputs.source_run_id, inputs.registered, inputs.semantics],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=_RETRY,
            )
            spent.append(semantics.spend)
            replayed = True

        self._stage = "activating"
        await workflow.execute_activity(
            act.activate_version,
            # Three arguments, not four. Temporal maps a payload onto an
            # activity's parameters by arity: hand a three-parameter activity a
            # fourth and the converter gives up and passes raw dicts, which dies
            # on `'dict' object has no attribute 'library_id'` several frames
            # from anything that names the cause.
            args=[inputs.request, inputs.staged, inputs.registered],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

        self._stage = "done"
        await self._finish(run_id, "succeeded")
        return RebuildResult(
            run_id=run_id,
            document_id=inputs.registered.document_id,
            version_id=inputs.registered.version_id,
            state="rebuilt",
            detail=f"reproducido desde {inputs.source_run_id}",
            points=indexed.points,
            graph=graph,
            semantics_replayed=replayed,
            spend=spent,
        )

    # -- gate --------------------------------------------------------------

    async def _gate(self, run_id: str) -> Approval:
        self._stage = "awaiting_approval"
        await workflow.execute_activity(
            act.set_run_stage,
            args=[run_id, "awaiting_approval", "awaiting_approval"],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )
        try:
            await workflow.wait_condition(
                lambda: self._approval is not None, timeout=GATE_TIMEOUT
            )
        except TimeoutError:
            # Bounded for the same reason the ingest gate is: a timeout costs
            # nothing and leaves the document exactly as it was, while an
            # unbounded wait accumulates workflows that never report an outcome.
            return Approval(
                approved=False,
                reason=f"nadie respondió en {GATE_TIMEOUT.days} días",
            )
        assert self._approval is not None
        return self._approval

    # -- bookkeeping -------------------------------------------------------

    async def _finish(self, run_id: str, state: str) -> None:
        await workflow.execute_activity(
            act.record_run_outcome,
            args=[run_id, state, None, None],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _record_failure(self, run_id: str, error: ActivityError) -> None:
        cause = error.cause
        kind = "activity_failed"
        detail = str(cause or error)
        if isinstance(cause, ApplicationError):
            kind = cause.type or kind
        try:
            await workflow.execute_activity(
                act.record_run_outcome,
                args=[run_id, "failed", kind, detail[:2000]],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except Exception:
            workflow.logger.warning("could not record rebuild failure for %s", run_id)
