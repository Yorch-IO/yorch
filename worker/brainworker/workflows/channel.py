"""Reading a channel: two workflows, one run kind, one stage vocabulary.

They are separate because a person acts between them. `ChannelDiscoverWorkflow`
judges metadata and hands back a shortlist; the client then starts a free video
run for each of those, which resolves, downloads the captions and parks at its
own gate; and `ChannelTopicsWorkflow` reads those transcripts. A single workflow
spanning all three would have to wait on runs it did not start, which is a
parent waiting on children for no benefit — every video is already a run row in
the import queue, with its own estimate, its own artifacts and its own gate.

**The batch is a sum on a screen, not a gate of its own**, and that is the
decision this design rests on. Each video's gate quotes that video exactly,
through the existing `estimate_video`; the channel screen adds them up and
approves the ticked ones by signalling each run. Nothing is reimplemented, a
half-finished batch leaves every remaining run parked for its seven days rather
than lost, and re-running it costs nothing — an already-indexed video
short-circuits at `registering` and spends $0.

Neither workflow needs `workflow.patched`. The one patch in this codebase guards
an activity inserted at the *head* of a workflow that had executions in flight;
these two are new, so nothing older than them can be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from ..activities import ingest as act
    from ..activities.asking import record_question_cost, start_question_run
    from ..activities.channel import (
        preselect_channel_videos,
        quote_channel_discovery,
        read_channel_topics,
        synthesise_channel,
    )
    from ..answering.types import Question
    from ..channel.synthesis import Synthesis
    from ..channel.types import (
        DiscoverRequest,
        DiscoveryOutcome,
        TopicsOutcome,
        TopicsRequest,
    )
    from ..pipeline import Estimate, RunOpen
    from .video import failure_of

#: Reading a catalogue off the volume and pricing it. No provider call.
FREE_TIMEOUT = timedelta(minutes=5)

#: Bookkeeping.
WRITE_TIMEOUT = timedelta(minutes=2)

#: The metadata pass: four calls for a hundred videos.
PRESELECT_TIMEOUT = timedelta(minutes=30)

#: The transcript pass: one call per video over up to 200,000 characters each.
#: Generous, because the failure this guards against is a provider that has
#: stopped answering rather than a slow one — and the per-minute embedding quota
#: has already been measured making one call take 18.8 s on its own.
TOPICS_TIMEOUT = timedelta(hours=2)

#: Long enough that a heartbeat interval of one video is comfortably inside it.
TOPICS_HEARTBEAT_TIMEOUT = timedelta(minutes=15)

#: Generous, for the reason `ASK_TIMEOUT` is: a real question against the
#: church-history library once outran the app's own 180 s timeout — was
#: computed, was billed, and was discarded under a message blaming an
#: unreachable API. A synthesis reads more evidence than a question does.
SYNTHESIS_TIMEOUT = timedelta(minutes=20)

_RETRY = RetryPolicy(maximum_attempts=3)

#: Two attempts, not three, for the reason `_PAID_RETRY` exists: a retried
#: attempt re-runs the whole pass, and the whole pass is what costs money.
_PAID_RETRY = RetryPolicy(maximum_attempts=2)


class _Tracked:
    """The run trail, shared by both workflows.

    The same shape `IngestWorkflow` and `VideoIngestWorkflow` use, and the same
    two rules: `seq` is a counter on the workflow object and `at` is
    `workflow.now()`, so a retried transition carries the number *and the
    timestamp* it carried the first time and `ON CONFLICT (run_id, seq) DO
    NOTHING` drops it. `now()` in SQL would move under a retry and silently
    stretch the previous stage's measured duration.

    Unlike those two there is no `_pending` buffer, because there is nothing to
    buffer: the run row is opened by the first activity these workflows run, so
    no event can precede it. `_insert_event` derives its tenant from the run row
    and silently drops an event written before it exists, which is what the
    buffer in the other two is for.
    """

    def __init__(self) -> None:
        self._stage = "starting"
        self._seq = 0

    @workflow.query
    def stage(self) -> str:
        return self._stage

    async def _open(self, run_id: str, *, tenant_id: str, library_id: str, label: str) -> None:
        await workflow.execute_activity(
            act.open_run,
            RunOpen(
                run_id=run_id,
                workflow_id=workflow.info().workflow_id,
                kind="channel",
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


@workflow.defn(name="ChannelDiscoverWorkflow")
class ChannelDiscoverWorkflow(_Tracked):
    """Quote both passes, then judge the channel's metadata.

    The quote covers the transcript pass as well as this one, because that is
    the figure the person was shown before they pressed the button — a receipt
    that priced half of what was offered would be worse than none.
    """

    def __init__(self) -> None:
        super().__init__()
        self._estimate: Estimate | None = None

    @workflow.query
    def estimate(self) -> Estimate | None:
        return self._estimate

    @workflow.run
    async def run(self, request: DiscoverRequest) -> DiscoveryOutcome:
        run_id = workflow.info().workflow_id
        await self._open(
            run_id,
            tenant_id=request.tenant_id,
            library_id=request.library_id,
            label=request.topic,
        )
        try:
            await self._enter(run_id, "quoting")
            self._estimate = await workflow.execute_activity(
                quote_channel_discovery,
                args=[run_id, request],
                start_to_close_timeout=FREE_TIMEOUT,
                retry_policy=_RETRY,
            )

            await self._enter(run_id, "preselecting")
            outcome: DiscoveryOutcome = await workflow.execute_activity(
                preselect_channel_videos,
                args=[run_id, request],
                start_to_close_timeout=PRESELECT_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
        except ActivityError as e:
            await self._record_failure(run_id, e)
            raise
        await self._enter(
            run_id,
            "done",
            detail=f"{outcome.relevant} relevantes, {outcome.doubtful} dudosos "
            f"de {outcome.evaluated}",
        )
        await self._finish(run_id, "succeeded")
        return outcome


@workflow.defn(name="ChannelTopicsWorkflow")
class ChannelTopicsWorkflow(_Tracked):
    """Read the uncorrected transcripts of videos that have already probed."""

    @workflow.run
    async def run(self, request: TopicsRequest) -> TopicsOutcome:
        run_id = workflow.info().workflow_id
        await self._open(
            run_id,
            tenant_id=request.tenant_id,
            library_id=request.library_id,
            label=request.topic,
        )
        try:
            await self._enter(run_id, "reading")
            outcome: TopicsOutcome = await workflow.execute_activity(
                read_channel_topics,
                args=[run_id, request],
                start_to_close_timeout=TOPICS_TIMEOUT,
                heartbeat_timeout=TOPICS_HEARTBEAT_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
        except ActivityError as e:
            await self._record_failure(run_id, e)
            raise
        await self._enter(
            run_id,
            "done",
            detail=f"{outcome.answering} de {outcome.videos} responden; "
            f"{outcome.verified} temas con cita comprobada",
        )
        await self._finish(run_id, "succeeded")
        return outcome


@dataclass
class SynthesisOutcome:
    """What a poller reads. The same two-call shape `/ask` uses, and for the
    same recorded reason: a real question outran a 180 s client timeout, was
    computed, was billed, and was discarded under a message blaming an
    unreachable API."""

    state: str = "running"
    synthesis: "Synthesis | None" = None
    error: dict[str, str] | None = None


@workflow.defn(name="ChannelAskWorkflow")
class ChannelAskWorkflow:
    """A comparative reading of one channel, as a durable question.

    `run.kind` is **`ask`**, not `channel`: this has no gate and no pipeline, so
    it has no stage vocabulary to belong to — exactly like every other question.
    Reusing `start_question_run` and `record_question_cost` is what makes that
    true rather than merely stated, and it is why this needed no migration.
    """

    def __init__(self) -> None:
        self._outcome = SynthesisOutcome()

    @workflow.query
    def result(self) -> SynthesisOutcome:
        """Answerable from the moment the workflow exists, which is the point."""
        return self._outcome

    @workflow.run
    async def run(self, question: Question) -> SynthesisOutcome:
        run_id = workflow.info().workflow_id
        await workflow.execute_activity(
            start_question_run,
            args=[run_id, question],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )
        try:
            result: Synthesis = await workflow.execute_activity(
                synthesise_channel,
                question,
                start_to_close_timeout=SYNTHESIS_TIMEOUT,
                # One attempt. Every one is a paid generation.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as e:
            kind, detail = failure_of(e)
            self._outcome = SynthesisOutcome(
                state="failed", error={"kind": kind, "message": detail[:2000]}
            )
            # A question that failed still spent whatever it spent before it
            # did: the planner runs first and is a paid call.
            await self._book(run_id, "failed", [])
            return self._outcome

        self._outcome = SynthesisOutcome(state="done", synthesis=result)
        await self._book(run_id, "done", result.spend)
        return self._outcome

    async def _book(self, run_id: str, state: str, spend: list) -> None:
        """Write the bill. Never allowed to change the outcome."""
        await workflow.execute_activity(
            record_question_cost,
            args=[run_id, state, spend],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )


def workflows() -> list[Any]:
    """Registered by `runner.py`, named here so the two cannot drift."""
    return [ChannelDiscoverWorkflow, ChannelTopicsWorkflow, ChannelAskWorkflow]
