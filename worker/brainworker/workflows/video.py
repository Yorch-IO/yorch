"""Indexing a video: probe, gate, transcribe if you must, index.

A workflow of its own rather than a branch in `IngestWorkflow`, because the two
differ before they converge: a document is bytes that already exist and a video
is a URL that has to be turned into text, sometimes by paying Amazon. Only the
tail — correct, chunk, project, embed, activate — is shared, and it is shared by
calling the same activities rather than by threading a flag through sixteen
stages of the other one.

**The gate is the pivot, and the two paths differ in what it can show.** With
captions the transcript is already in hand for free, so the gate quotes
correction from the real character count and shows the chunks that will be
indexed. Without them there is nothing to preview until the money is spent, and
`VideoGateReport.preview` is `None` — which says so, rather than fabricating a
count nobody measured.

**The waiting for Amazon is a timer, not an activity.** The EC2 host stops
nightly at 23:00 and nothing starts it again on a schedule. A `workflow.sleep`
is server-side durable state: the host can go down mid-wait and the workflow
resumes where it was, while Transcribe keeps working. A polling *activity* is
worker-local: the stop kills it, Temporal retries the whole attempt, and the
attempt begins by downloading the audio again.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.exceptions import TimeoutType

with workflow.unsafe.imports_passed_through():
    from ..artifacts import ArtifactRef
    from ..activities import ingest as act
    from ..activities import paid
    from ..activities import video as vid
    from ..pipeline import (
        Chunked,
        Correction,
        Estimate,
        Extraction,
        IngestRequest,
        Indexed,
        Preview,
        Registered,
        RunOpen,
        Spend,
        Staged,
        StageOptions,
        Transcribed,
        TranscriptionJob,
        VideoGateReport,
        VideoInfo,
        VideoProbe,
        VideoRequest,
        VideoResult,
    )
    from ..videosource import correction_default, projected_characters
    from docagent.chunk import ChunkRules
    from .ingest import (
        FREE_TIMEOUT,
        GATE_TIMEOUT,
        PAID_TIMEOUT,
        WRITE_TIMEOUT,
        Approval,
        _PAID_RETRY,
        _RETRY,
        _RUN_ROW_FIRST,
        _total,
    )

#: Downloading a long video's audio is minutes, not seconds, and it heartbeats.
AUDIO_TIMEOUT = timedelta(hours=2)
AUDIO_HEARTBEAT_TIMEOUT = timedelta(minutes=5)

#: One look at a job. Short on purpose — see the module docstring.
POLL_TIMEOUT = timedelta(minutes=1)

#: How long to keep waiting for Amazon before giving up and deleting the job.
#:
#: Days rather than hours, and that is not generosity: the host stops nightly
#: with no start schedule, so a job finishing at 23:05 is collected whenever
#: somebody next starts the machine. A timeout in hours would abandon jobs that
#: had already succeeded and been paid for.
TRANSCRIBE_DEADLINE = timedelta(days=3)

#: Poll backoff. Ten looks covers three hours; a job that outlives the host's
#: nightly stop is picked up on the first tick after it comes back.
FIRST_POLL = timedelta(seconds=30)
MAX_POLL = timedelta(minutes=5)

#: How long an activity routed to the fetch queue may sit unclaimed.
#:
#: It exists so that "nobody is running the fetcher" is a **failure** rather than
#: a run that waits for ever. Without it the split trades one invisible outcome
#: for another: the old bug was a run that vanished, and a run parked
#: indefinitely on an empty queue is barely better. Minutes, because the fetcher
#: is a process somebody starts by hand and a restart should not fail a run.
#:
#: Temporal does not retry a schedule-to-start timeout, which is what makes this
#: fail fast rather than three times over.
FETCH_START_TIMEOUT = timedelta(minutes=10)


@workflow.defn(name="VideoIngestWorkflow")
class VideoIngestWorkflow:
    def __init__(self) -> None:
        self._approval: Approval | None = None
        self._report: VideoGateReport | None = None
        self._stage: str = "starting"
        self._seq: int = 0
        self._registered: bool = False
        self._pending: list[dict[str, object]] = []

    # -- signals and queries ----------------------------------------------

    @workflow.signal
    def approve(self, approval: Approval) -> None:
        """Answer the gate. Same name and same payload as `IngestWorkflow`'s.

        Deliberately identical, because that is what lets `/runs/{id}/approve`,
        `/cancel`, `/audit`, `/events` and `/runs/{id}` serve a video run with no
        change at all. The gate *report* is the one thing that cannot be shared,
        and it has its own query and its own route for the reason
        `rebuild_gate` documents.
        """
        self._approval = approval

    @workflow.query
    def gate_report(self) -> VideoGateReport | None:
        return self._report

    @workflow.query
    def stage(self) -> str:
        return self._stage

    # -- run ---------------------------------------------------------------

    @workflow.run
    async def run(self, request: VideoRequest, options: StageOptions) -> VideoResult:
        run_id = workflow.info().workflow_id
        try:
            return await self._run(request, options, run_id)
        except asyncio.CancelledError:
            await self._finish(run_id, "cancelled", "cancelled", "cancelado")
            raise
        except ActivityError as e:
            await self._record_failure(run_id, e)
            raise
        except ApplicationError as e:
            # Raised by this workflow rather than by an activity — a transcription
            # job that failed or outlived its deadline. Without this branch the
            # run reads `running` for ever, which is the same lie `_set_stage`
            # used to tell, arrived at from a third direction: the workflow
            # execution is FAILED at Temporal and the catalog never hears.
            await self._finish(
                run_id, "failed", e.type or "workflow_failed", str(e)[:2000]
            )
            raise

    async def _run(
        self, request: VideoRequest, options: StageOptions, run_id: str
    ) -> VideoResult:
        await self._open(request, run_id)

        await self._enter(run_id, "probing")
        # Which worker asks YouTube. Empty means this one, which is what the
        # product did before the field existed. Resolved once and read from the
        # workflow's own history, never from settings — a workflow may only
        # decide on what it can replay.
        fetch_queue = request.fetch_queue or workflow.info().task_queue
        info: VideoInfo | None = request.resolved
        if info is None:
            # Nobody resolved this for us, so the pipeline asks YouTube itself.
            # That is the original path and it is still right wherever the
            # worker's own egress is answered — a laptop, or a hosted
            # deployment with `fetch_queue` pointed at one. It is the path that
            # fails with `youtube_refused_this_host` from a datacentre, which
            # is why a client that *can* make the call is offered the chance.
            info = await workflow.execute_activity(
                vid.resolve_video,
                request,
                task_queue=fetch_queue,
                schedule_to_start_timeout=FETCH_START_TIMEOUT,
                start_to_close_timeout=FREE_TIMEOUT,
                retry_policy=_RETRY,
            )
        # No branch here for checking it: `probe_video` runs
        # `videosource.check_resolved` on whatever it is handed, so the record
        # this workflow supplies and the record an activity produced are held to
        # the same rule by the same line of code.
        probe: VideoProbe = await workflow.execute_activity(
            vid.probe_video,
            args=[request, run_id, info],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        ingest_request = _as_ingest_request(request, probe)
        staged = _as_staged(probe)

        await self._enter(run_id, "registering")
        registered: Registered = await workflow.execute_activity(
            act.register_document,
            args=[ingest_request, staged, run_id, workflow.info().workflow_id],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )
        await self._flush_pending(run_id)

        if registered.already_indexed and not request.reindex:
            # The identity was computed before the gate precisely so this can
            # happen before anything is paid for. Deriving it from the finished
            # transcript would mean discovering the duplicate after the bill.
            await workflow.execute_activity(
                act.link_duplicate,
                args=[ingest_request, staged, registered],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=_RETRY,
            )
            await self._discard_staged_audio(request)
            await self._finish(run_id, "succeeded")
            return VideoResult(
                run_id=run_id,
                document_id=registered.document_id,
                version_id=registered.version_id,
                state="already_indexed",
                detail="este vídeo ya estaba indexado",
                transcript_source=_source_of(probe),
            )

        # `probe_video` ran before the run row existed, so its artifacts have
        # files and no catalog rows. This is the same flush `_flush_pending`
        # performs for the events it buffered, and it is best-effort in the same
        # way: a missing row loses a line of the audit trail, not the run.
        written = [r for r in (probe.probe_ref, probe.captions) if r is not None]
        if written:
            await workflow.execute_activity(
                vid.record_video_artifacts,
                args=[run_id, written],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=_RETRY,
            )

        transcribed: Transcribed | None = None
        preview: Preview | None = None
        if probe.chosen is not None:
            assert probe.captions is not None
            transcribed = await self._group(run_id, probe, probe.captions)
            await self._enter(run_id, "previewing")
            preview = await workflow.execute_activity(
                vid.preview_transcript,
                args=[run_id, transcribed],
                start_to_close_timeout=FREE_TIMEOUT,
                retry_policy=_RETRY,
            )
        else:
            await self._enter(run_id, "previewing")

        characters = transcribed.characters if transcribed else _projected(probe)
        # Zero with captions: the text is in hand, so there is no projection to
        # draw a range around and the per-stage spreads stand as they are.
        characters_high = 0 if transcribed else _projected(probe, high=True)
        chunk_count = preview.chunk_count if preview else _projected_chunks(characters)
        # Narrowed **before** the estimate, not after. Quoting the stages this
        # workflow has no stage for is not a harmless over-report: on a
        # 19-second video measured locally it added $0.0203 of profile learning
        # and $0.0059 of semantics to a bill whose real total was $0.000012, and
        # "over-reporting wildly" misleads a user into declining affordable work
        # exactly as much as under-reporting misleads them into approving an
        # expensive one.
        recommended = _recommended(options, probe)
        estimate: Estimate = await workflow.execute_activity(
            vid.estimate_video,
            args=[probe, recommended, characters, chunk_count, characters_high,
                  run_id],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )
        self._report = VideoGateReport(
            run_id=run_id,
            document_id=registered.document_id,
            version_id=registered.version_id,
            probe=probe,
            estimate=estimate,
            preview=preview,
            transcript=transcribed,
            warnings=list(probe.warnings),
            recommended=recommended,
        )

        approved = await self._gate(request, recommended, run_id)
        if not approved.approved:
            await self._discard_staged_audio(request)
            await self._finish(run_id, "cancelled", "rejected", approved.reason)
            return VideoResult(
                run_id=run_id,
                document_id=registered.document_id,
                version_id=registered.version_id,
                state="rejected",
                detail=approved.reason,
            )
        options = approved.options

        if transcribed is None:
            transcribed = await self._transcribe(
                run_id, probe, request, registered
            )

        spent: list[Spend] = []
        source_text = transcribed.text
        if options.correct:
            await self._enter(run_id, "correcting")
            correction: Correction = await workflow.execute_activity(
                paid.correct_text,
                args=[run_id, _as_extraction(transcribed, probe, registered)],
                start_to_close_timeout=PAID_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            spent.append(correction.spend)
            source_text = correction.text

        await self._enter(run_id, "chunking")
        chunked: Chunked = await workflow.execute_activity(
            vid.chunk_transcript,
            # The uncorrected stream travels as the fallback: a correction that
            # moved the paragraph count would move every timestamp, and a wrong
            # timestamp is worse than a missing correction.
            args=[run_id, source_text, transcribed.cues, transcribed.text],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        # A second `chunking` row, carrying what chunking found. The two
        # conditions it reports — a correction that moved the paragraph count,
        # so the *uncorrected* stream was indexed, and a paragraph that reached
        # no chunk — used to exist only in the worker's stderr, which is not
        # somewhere a reader of the run looks and not somewhere a replaced
        # container keeps. Scheduled only when there is something to say, which
        # is also what makes it replay-safe: a history from before `Chunked`
        # carried warnings decodes to none and this command is never issued.
        if chunked.warnings:
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

        indexed: Indexed | None = None
        if options.embed:
            await self._enter(run_id, "embedding")
            indexed = await workflow.execute_activity(
                paid.embed_and_index,
                args=[run_id, request.library_id, registered, staged, chunked],
                start_to_close_timeout=PAID_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            spent.append(indexed.spend)

            await self._enter(run_id, "activating")
            await workflow.execute_activity(
                act.activate_version,
                args=[ingest_request, staged, registered],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=_RETRY,
            )

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

    # -- the two ways a transcript is produced -----------------------------

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

    async def _discard_staged_audio(self, request: VideoRequest) -> None:
        """Throw away audio the client staged for a run that will not use it.

        The download happens before the gate — the bytes cost nothing, so they
        are not what the gate is guarding, and the alternative parks a workflow
        after approval until a laptop sends them, which stalls invisibly when
        the window is closed. The consequence is this: a rejected gate, or a
        video that turns out to be already indexed, leaves up to a gigabyte in
        the inbox unless something removes it. `stage_audio` deletes in a
        `finally` and neither of those paths reaches it.

        Not covered: a run that *crashes* between registration and staging.
        That leaves the file exactly as an upload through `POST /uploads` whose
        ingest is never started leaves one, which is an existing property of
        the inbox rather than something introduced here.
        """
        if not request.audio_path:
            return
        await workflow.execute_activity(
            vid.discard_audio,
            args=[request.audio_path, request.tenant_id],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

    async def _transcribe(
        self,
        run_id: str,
        probe: VideoProbe,
        request: VideoRequest,
        registered: Registered,
    ) -> Transcribed:
        """Audio to S3, a job at Amazon, then wait — on a timer, not in a call."""
        await self._enter(run_id, "fetching")
        if request.audio_path:
            # The caller already downloaded it, which is the only thing that
            # works when the caller is also who resolved the video: a
            # `googlevideo` URL carries the address that resolved it and answers
            # 403 anywhere else. All that is left is to put it where Transcribe
            # can read it, and that is a job for the host holding the instance
            # role rather than for the laptop that has the bytes.
            audio = await workflow.execute_activity(
                vid.stage_audio,
                args=[run_id, request.audio_path, probe, request.tenant_id,
                      registered.version_id],
                start_to_close_timeout=AUDIO_TIMEOUT,
                heartbeat_timeout=AUDIO_HEARTBEAT_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        else:
            # Beside the resolution, not beside the workspace. A `googlevideo`
            # URL carries the address that resolved it (`ip=…`) and answers 403
            # from anywhere else — measured — so the download cannot be split
            # from the `extract_info` that produced the URL. It costs nothing to
            # move: this activity writes no artifact, only a transient file it
            # deletes in a `finally`, and returns an S3 URI.
            audio = await workflow.execute_activity(
                vid.fetch_audio,
                args=[run_id, probe, request.tenant_id, registered.version_id],
                task_queue=request.fetch_queue or workflow.info().task_queue,
                schedule_to_start_timeout=FETCH_START_TIMEOUT,
                start_to_close_timeout=AUDIO_TIMEOUT,
                heartbeat_timeout=AUDIO_HEARTBEAT_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        await self._enter(run_id, "transcribing")
        language = _language_for(request, probe)
        job: TranscriptionJob = await workflow.execute_activity(
            vid.start_transcription,
            args=[run_id, probe, audio, request.tenant_id, registered.version_id,
                  language],
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
            # Deleted before failing, because the name is derived from the
            # version and Amazon keeps it for 90 days — leaving it would make
            # every later attempt a ConflictException reporting this failure.
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

        result: ArtifactRef = await workflow.execute_activity(
            vid.collect_transcript,
            args=[run_id, job, request.tenant_id, registered.version_id],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )
        return await self._group(run_id, probe, result)

    # -- the gate ----------------------------------------------------------

    async def _gate(
        self, request: VideoRequest, options: StageOptions, run_id: str
    ) -> Approval:
        if request.auto_approve:
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

    # -- bookkeeping, identical in shape to IngestWorkflow's ---------------

    async def _open(self, request: VideoRequest, run_id: str) -> None:
        """Open the run row before `probe_video` can fail against nothing.

        **This is the one that fired.** On 2026-09-05 a user submitted a YouTube
        URL against the paid plane on EC2, `POST /videos` answered 200, and
        nothing ever appeared in the import queue — `probe_video` was refused by
        YouTube from the datacenter egress IP 2.8 s in, and the `run` row was
        INSERTed by `register_document`, the activity after it. `_record_failure`
        then ran `finish_run`, a bare UPDATE, which affected zero rows and raised
        nothing: the activity reported Completed and the failure was recorded
        nowhere. `SELECT count(*) FROM run WHERE kind='video'` in production was
        0 with one `VideoIngestWorkflow` in the namespace.

        `label` is the URL, because that is genuinely all there is to say about a
        video that failed before it was probed — the title comes out of the probe
        that did not happen.

        Patched for the reason `IngestWorkflow._open` gives; the same id, because
        it is one change.
        """
        if not workflow.patched(_RUN_ROW_FIRST):
            return
        await workflow.execute_activity(
            act.open_run,
            RunOpen(
                run_id=run_id,
                workflow_id=workflow.info().workflow_id,
                kind="video",
                tenant_id=request.tenant_id,
                library_id=request.library_id,
                label=request.url,
            ),
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )
        self._registered = True

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


# --- pure helpers, so the workflow body reads as a sequence of stages --------


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
      this workflow means exactly one thing: `fetch_queue` names a queue no
      fetcher is serving. `activity_failed` would send a reader looking for a
      broken activity when the answer is that a process is not running.
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


def _as_ingest_request(request: VideoRequest, probe: VideoProbe) -> IngestRequest:
    """The shape every reused activity expects.

    `source_path` is the canonical URL rather than a path, which is what
    `document.source_path` is for — "so the Library can re-index without asking
    the user to find the file again". Nothing in the video path passes it to
    `stage_source`, which is the one function that would reject it.
    """
    return IngestRequest(
        library_id=request.library_id,
        source_path=probe.canonical_url,
        source_key=probe.source_key,
        title=probe.title,
        author=request.author or probe.channel or None,
        tenant_id=request.tenant_id,
        library_name=request.library_name,
        reindex=request.reindex,
        run_kind="video",
    )


def _as_staged(probe: VideoProbe) -> Staged:
    """What `register_document` and `project_structure` read out of staging.

    `byte_size` is 0 and honest: a video has no byte size at registration, and
    putting the duration there would make it a term in every "corpus size" sum
    the UI takes. `fmt` is what `_locator` branches on to render a timestamp
    instead of a byte range.
    """
    return Staged(
        content_sha256=probe.content_sha256,
        byte_size=0,
        fmt="youtube",
        extractor="youtube_captions" if probe.chosen else "aws_transcribe",
        title=probe.title,
    )


def _as_extraction(
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


def _recommended(options: StageOptions, probe: VideoProbe) -> StageOptions:
    """The options the gate opens with, given where this transcript came from.

    Only `correct` moves away from the caller's own choice, and it moves on the
    rule in `videosource.correction_default`: automatic captions arrive with no
    punctuation, which is the one deficit correction closes that `verify` will
    not reject.

    Everything this workflow has no stage for is switched **off** — and that is
    not tidiness, it is the estimate. Quoting profile learning and semantics on
    a 19-second video added $0.026 to a bill whose real total was $0.000012, and
    over-reporting wildly misleads a user into declining affordable work exactly
    as much as under-reporting misleads them into approving an expensive one.
    """
    return replace(
        options,
        correct=correction_default(probe.chosen.kind if probe.chosen else None),
        learn_profile=False,
        extract_semantics=False,
        generate_evalset=False,
        tune=False,
        condense_descriptions=False,
    )


def _source_of(probe: VideoProbe) -> str:
    if probe.chosen is None:
        return "transcribe"
    return f"captions:{probe.chosen.language}:{probe.chosen.kind}"


def _language_for(request: VideoRequest, probe: VideoProbe) -> str:
    """Which language Amazon is told to expect.

    From the request's preference, else the library's own — `library.language`
    already exists and defaults to `es`, so a Spanish shelf transcribes as
    Spanish with no per-video setting to get wrong.
    """
    if request.languages:
        lang = request.languages[0]
        return lang if "-" in lang else f"{lang}-{lang.upper()}"
    return "es-ES"


def _projected(probe: VideoProbe, *, high: bool = False) -> int:
    return projected_characters(probe.duration_s, high=high)


def _projected_chunks(characters: int) -> int:
    """Roughly how many chunks that many characters will make.

    The chunker aims for `ChunkRules.target_chars`, so this is the ratio and not
    a measurement. It feeds only the per-chunk lines of the estimate, which for
    a video are the ones that do not run.
    """
    return max(1, characters // ChunkRules().target_chars)
