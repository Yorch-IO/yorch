"""What indexing a *timed* source shares, whatever produced the clock.

`VideoIngestWorkflow` and `AudioIngestWorkflow` differ before the gate — a
YouTube URL is resolved, probed and maybe captioned; an object in a customer's
bucket is HEADed and its headers read — and in how the audio reaches S3. From
the moment a transcript exists they are the same sequence of activities with
the same arguments in the same order: correct, chunk with the time table,
project, export, embed, extract, activate. This module is that sequence, once.

**It was extracted, not written.** The body of :meth:`TimedIngest._index_transcript`
is the tail of `VideoIngestWorkflow._run` as it stood on 2026-09-16, moved
verbatim so the *commands* a video run issues are unchanged — which is what
keeps eleven runs parked at their gates in the local namespace replayable
after the worker restarts. `tests/workflows/test_replay.py` replays a real
pre-extraction history to say so, rather than trusting this paragraph.

**A base class rather than module functions**, because the sequence reads the
workflow's own bookkeeping (`_enter`, `_finish`, `_seq`) and a function taking
the workflow as its first argument is a method with a worse name. The concrete
classes keep their own `@workflow.run`, signals and queries; nothing here is
decorated, and the sandbox is indifferent to inheritance.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.exceptions import TimeoutType

with workflow.unsafe.imports_passed_through():
    from ..artifacts import ArtifactRef
    from ..activities import exporting as export
    from ..activities import ingest as act
    from ..activities import paid
    from ..activities import video as vid
    from ..pipeline import (
        AudioStaged,
        Chunked,
        Correction,
        Extraction,
        IngestRequest,
        Indexed,
        Registered,
        Spend,
        Staged,
        StageOptions,
        Transcribed,
        TranscriptionJob,
        VideoGateReport,
        VideoProbe,
        VideoResult,
    )
    from .ingest import (
        FREE_TIMEOUT,
        GATE_TIMEOUT,
        PAID_HEARTBEAT_TIMEOUT,
        PAID_TIMEOUT,
        WRITE_TIMEOUT,
        Approval,
        _PAID_RETRY,
        _RETRY,
        _total,
    )

#: One look at a job. Short on purpose — see `workflows/video.py`.
POLL_TIMEOUT = timedelta(minutes=1)
#: How long to keep waiting for Amazon before giving up and deleting the job.
#: Days rather than hours: the host stops nightly with no start schedule, so a
#: job finishing at 23:05 is collected whenever somebody next starts the
#: machine, and a timeout in hours would abandon jobs already paid for.
TRANSCRIBE_DEADLINE = timedelta(days=3)
#: Poll backoff. Ten looks covers three hours; a job that outlives the host's
#: nightly stop is picked up on the first tick after it comes back.
FIRST_POLL = timedelta(seconds=30)
MAX_POLL = timedelta(minutes=5)
#: How long an activity routed to a fetch queue may sit unclaimed — see
#: `VideoRequest.fetch_queue`. Temporal does not retry a schedule-to-start
#: timeout, which is what makes this fail fast rather than three times over.
FETCH_START_TIMEOUT = timedelta(minutes=10)


class TimedIngest:
    """The shared half. Concrete workflows call `_init_state` from `__init__`."""

    _approval: Approval | None
    _report: VideoGateReport | None
    _stage: str
    _seq: int
    _registered: bool
    _pending: list[dict[str, object]]

    def _init_state(self) -> None:
        self._approval = None
        self._report = None
        self._stage = "starting"
        self._seq = 0
        self._registered = False
        self._pending = []

    async def _after_index(self, run_id: str, registered, options) -> None:
        """A last step, while the run is still open. Nothing, by default."""
        return None

    # -- from a transcript to an index --------------------------------------

    async def _index_transcript(
        self,
        run_id: str,
        library_id: str,
        ingest_request: IngestRequest,
        staged: Staged,
        registered: Registered,
        probe: VideoProbe,
        transcribed: Transcribed,
        options: StageOptions,
    ) -> VideoResult:
        spent: list[Spend] = []
        source_text = transcribed.text
        if options.correct:
            await self._enter(run_id, "correcting")
            correction: Correction = await workflow.execute_activity(
                paid.correct_text,
                args=[run_id, as_extraction(transcribed, probe, registered)],
                start_to_close_timeout=PAID_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            spent.append(correction.spend)
            source_text = correction.text

        await self._enter(run_id, "chunking")
        chunked: Chunked = await workflow.execute_activity(
            vid.chunk_transcript,
            args=[run_id, source_text, transcribed.cues, transcribed.text],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        if chunked.warnings:
            # A second `chunking` row carrying the detail. The two conditions it
            # reports — a correction that moved the paragraph count, a paragraph
            # that reached no chunk — are "the correction was paid for and not
            # indexed", which must not live only in a container's stderr.
            await self._enter(
                run_id, "chunking", detail=" · ".join(chunked.warnings)
            )

        await self._enter(run_id, "projecting")
        projected: dict[str, int] = await workflow.execute_activity(
            act.project_structure,
            args=[ingest_request, staged, registered, run_id, chunked.chunks],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        if options.build_epub:
            await self._enter(run_id, "epub")
            metadata: Spend | None = await workflow.execute_activity(
                export.resolve_book_metadata,
                args=[run_id, registered, chunked.chunks],
                start_to_close_timeout=FREE_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            if metadata is not None:
                spent.append(metadata)
            await workflow.execute_activity(
                export.build_epub,
                args=[run_id, library_id, registered, chunked.chunks],
                start_to_close_timeout=FREE_TIMEOUT,
                retry_policy=_RETRY,
            )

        indexed: Indexed | None = None
        if options.embed:
            await self._enter(run_id, "embedding")
            indexed = await workflow.execute_activity(
                paid.embed_and_index,
                args=[run_id, library_id, registered, staged, chunked],
                start_to_close_timeout=PAID_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            spent.append(indexed.spend)

            if options.extract_semantics:
                await self._enter(run_id, "semantics")
                semantics = await workflow.execute_activity(
                    paid.extract_semantics,
                    args=[run_id, registered, chunked, options],
                    start_to_close_timeout=PAID_TIMEOUT,
                    heartbeat_timeout=PAID_HEARTBEAT_TIMEOUT,
                    retry_policy=_PAID_RETRY,
                )
                spent.append(semantics.spend)
                if semantics.condense_spend is not None:
                    spent.append(semantics.condense_spend)

            await self._enter(run_id, "activating")
            await workflow.execute_activity(
                act.activate_version,
                args=[ingest_request, staged, registered],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=_RETRY,
            )

        # Anything a path wants to do with what it just indexed, while the run
        # is still open. A bucket run writes the corrected transcript back to
        # the customer here; a video has nowhere to write one.
        #
        # **Before `_finish`, and that is the point.** Called after it, the
        # step's own `_enter` reopened a run the outcome had already closed —
        # measured on eight real runs, which ended `state=running`,
        # `stage=archiving` with `finished_at` already set, so every one of
        # them read as still going for ever in the queue.
        await self._after_index(run_id, registered, options)

        await self._enter(run_id, "done")
        await self._finish(run_id, "succeeded")
        return VideoResult(
            run_id=run_id,
            document_id=registered.document_id,
            version_id=registered.version_id,
            state="indexed" if indexed else "projected",
            indexed_chunks=indexed.points if indexed else 0,
            projected=projected,
            total_usd=_total(spent),
            transcript_source=transcribed.source,
            detail=f"{chunked.count} fragmentos de {transcribed.paragraphs} párrafos",
        )

    # -- Amazon ----------------------------------------------------------------

    async def _await_transcription(
        self,
        run_id: str,
        probe: VideoProbe,
        audio: AudioStaged,
        tenant_id: str,
        version_id: str,
        language: str,
    ) -> ArtifactRef:
        """A job at Amazon, then wait — on a timer, not in a call.

        The waiting is `workflow.sleep`: server-side durable state the host can
        stop underneath. `poll_transcription` is single-shot and short so it can
        never be the thing in flight when the host goes down.
        """
        await self._enter(run_id, "transcribing")
        job: TranscriptionJob = await workflow.execute_activity(
            vid.start_transcription,
            args=[run_id, probe, audio, tenant_id, version_id, language],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_PAID_RETRY,
        )

        deadline = workflow.now() + TRANSCRIBE_DEADLINE
        delay = FIRST_POLL
        while job.status in ("QUEUED", "IN_PROGRESS"):
            if workflow.now() >= deadline:
                await workflow.execute_activity(
                    vid.abandon_transcription,
                    args=[job.job_name],
                    start_to_close_timeout=WRITE_TIMEOUT,
                    retry_policy=_RETRY,
                )
                raise ApplicationError(
                    f"Amazon no terminó en {TRANSCRIBE_DEADLINE.days} días",
                    type="transcribe_timeout",
                    non_retryable=True,
                )
            await workflow.sleep(delay)
            delay = min(delay * 2, MAX_POLL)
            job = await workflow.execute_activity(
                vid.poll_transcription,
                args=[job.job_name],
                start_to_close_timeout=POLL_TIMEOUT,
                retry_policy=_RETRY,
            )

        if job.status != "COMPLETED":
            await workflow.execute_activity(
                vid.abandon_transcription,
                args=[job.job_name],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=_RETRY,
            )
            raise ApplicationError(
                job.failure_reason or "la transcripción falló",
                type="transcribe_failed",
                non_retryable=True,
            )

        return await workflow.execute_activity(
            vid.collect_transcript,
            args=[run_id, job, tenant_id, version_id],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _group(
        self, run_id: str, probe: VideoProbe, source: ArtifactRef
    ) -> Transcribed:
        """Turn whichever source produced cues into the paragraph stream.

        Takes the reference the producing activity returned. It has to be a real
        one: `ArtifactStore.read_bytes` verifies the sha256, so a locator built
        from a path and an empty hash fails the very check it exists to pass.
        """
        await self._enter(run_id, "grouping")
        return await workflow.execute_activity(
            vid.group_transcript,
            args=[run_id, probe, source],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

    # -- the gate ----------------------------------------------------------------

    async def _gate(
        self, auto_approve: bool, options: StageOptions, run_id: str
    ) -> Approval:
        if auto_approve:
            return Approval(approved=True, options=options, reason="auto")
        await self._enter(run_id, "awaiting_approval", "awaiting_approval")
        try:
            await workflow.wait_condition(
                lambda: self._approval is not None, timeout=GATE_TIMEOUT
            )
        except TimeoutError:
            return Approval(
                approved=False,
                reason=f"nadie respondió en {GATE_TIMEOUT.days} días",
            )
        assert self._approval is not None
        return self._approval

    # -- bookkeeping, identical in shape to IngestWorkflow's -------------------

    async def _enter(
        self,
        run_id: str,
        stage: str,
        state: str = "running",
        detail: str | None = None,
    ) -> None:
        self._stage = stage
        self._seq += 1
        if not self._registered:
            self._pending.append(
                {"seq": self._seq, "at": workflow.now(), "stage": stage,
                 "detail": detail}
            )
            return
        await workflow.execute_activity(
            act.set_run_stage,
            args=[run_id, stage, state, self._seq, workflow.now(), detail],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _flush_pending(self, run_id: str) -> None:
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        self._registered = True
        await workflow.execute_activity(
            act.record_run_events,
            args=[run_id, pending],
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
            args=[run_id, state, error_kind, error_detail,
                  self._seq, workflow.now(), self._stage],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _record_failure(self, run_id: str, error: ActivityError) -> None:
        kind, detail = failure_of(error)
        try:
            self._seq += 1
            await workflow.execute_activity(
                act.record_run_outcome,
                args=[run_id, "failed", kind, detail[:2000],
                      self._seq, workflow.now(), self._stage],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except Exception:
            workflow.logger.warning("could not record run failure for %s", run_id)


# --- pure helpers ---------------------------------------------------------------


def failure_of(error: ActivityError) -> tuple[str, str]:
    """What kind of failure this was, and what to say about it.

    Pure and at module level for the reason `radial.ts` and `auditversion.py`
    are: the classification is the part worth asserting, and asserting it
    through a real Temporal run would mean waiting out a ten-minute
    schedule-to-start timeout that the time-skipping environment does not skip —
    an activity nobody claimed still counts as an activity in flight.

    Three cases, and the third is why this grew:

    - An `ApplicationError` carries the kind an activity chose.
    - A **schedule-to-start** timeout means nobody claimed the task, which on
      the video workflow means exactly one thing: `fetch_queue` names a queue
      no fetcher is serving. `activity_failed` would send a reader looking for
      a broken activity when the answer is that a process is not running.
    - Anything else keeps `activity_failed`.

    The timeout type is compared against the **enum member**, never against its
    string. `TimeoutType` is an `IntEnum`, so `str(TimeoutType.SCHEDULE_TO_START)`
    is `"2"` and a name match silently never fires — the same trap `event_type`
    set for the raw-history translation, which read `"3"`, matched nothing in
    the allowlist, and reported a run that did nothing.
    """
    cause = error.cause
    if isinstance(cause, ApplicationError):
        return cause.type or "activity_failed", str(cause)
    if (
        isinstance(cause, TemporalTimeoutError)
        and cause.type == TimeoutType.SCHEDULE_TO_START
    ):
        return "fetch_worker_unavailable", (
            "nadie recogió la tarea en la cola de descarga en "
            f"{FETCH_START_TIMEOUT.seconds // 60} minutos: "
            "¿está corriendo el worker de descarga?"
        )
    return "activity_failed", str(cause or error)


def as_extraction(
    transcribed: Transcribed, probe: VideoProbe, registered: Registered
) -> Extraction:
    return Extraction(
        text=transcribed.text,
        evidence=transcribed.evidence,
        extractor="youtube_captions" if probe.chosen else "aws_transcribe",
        structured=False,
        source_key=probe.source_key,
        tenant_id=registered.tenant_id,
        # The flag that keeps a corrected paragraph from splitting in two and
        # taking every later timestamp with it.
        single_line_paragraphs=True,
    )
