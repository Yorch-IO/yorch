"""Audio out of a customer's S3 bucket: catalogue it, then index one object.

Three workflows, sized to what each does:

- **`BucketSyncWorkflow`** lists the bucket into the catalogue. Awaited by the
  plane like `activate` and `epub`: it is a listing, seconds for a hundred
  objects and a minute for a hundred thousand, and the caller's next act is to
  render the result. One activity, heartbeating, saving page by page and
  skipping the header read for any object it already knows — which is what
  makes a retry re-walk cheaply rather than start over. No run row: it spends
  nothing and writes no artifact, exactly like a channel sync.

- **`AudioIngestWorkflow`** is `VideoIngestWorkflow` with the two
  YouTube-specific activities swapped for their bucket twins: `probe_object`
  reads the object's headers instead of asking yt-dlp, and `fetch_object`
  streams the object instead of a media URL. It produces a `VideoProbe` with
  no captions, which is the shape the rest of the video path already handles
  as "nothing to preview until the money is spent", and from the transcript
  on it *is* the video path — `TimedIngest._index_transcript`, unchanged.
  Two things are its own. `check_archive` at the head of `fetching` can make
  the whole run free: a transcript written back into the customer's bucket
  by a previous import of these exact bytes is reused instead of paid for
  again. And `archiving` after `transcribing` is what writes that transcript
  back, best-effort, so the next import can.

- **`MediaLinkWorkflow`** presigns one object for a click. Thin and awaited,
  so the STS and presign code lives once in Python and the paid plane takes
  no AWS SDK dependency. No run row either.

**The request carries the whole `BucketSource`**, never a bucket id to look
up, for the reason `VideoRequest.fetch_queue` travels in the request: a
workflow may only decide on what its own history holds, and a run parked
seven days at its gate must fetch from the bucket it was quoted against, not
from whatever `bucket.json` says by the time somebody approves it.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from ..artifacts import ArtifactRef
    from ..activities import bucket as bkt
    from ..activities import ingest as act
    from ..activities import video as vid
    from ..pipeline import (
        AudioRequest,
        AudioStaged,
        BucketSyncRequest,
        BucketSynced,
        Estimate,
        IngestRequest,
        LocalTranscript,
        MediaLink,
        MediaLinkRequest,
        Registered,
        RunOpen,
        Staged,
        StageOptions,
        Transcribed,
        TranscriptionJob,
        VideoGateReport,
        VideoProbe,
        VideoResult,
    )
    from ..graph.projection import AUDIO_FORMAT
    from ..videosource import projected_characters
    from docagent.chunk import ChunkRules
    from .ingest import (
        FREE_TIMEOUT,
        WRITE_TIMEOUT,
        Approval,
        _RETRY,
    )
    from .timed import TimedIngest
    from .video import AUDIO_HEARTBEAT_TIMEOUT, AUDIO_TIMEOUT

#: A listing of a hundred thousand objects with two ranged reads each, at
#: eight in flight, is a quarter of an hour. Hours, so a large bucket on a
#: slow day is a slow sync and not a failed one; it heartbeats throughout.
SYNC_TIMEOUT = timedelta(hours=3)
SYNC_HEARTBEAT_TIMEOUT = timedelta(minutes=5)
#: A presign is two requests. Seconds.
LINK_TIMEOUT = timedelta(seconds=30)
#: How long a run parked for a transcript from the desktop app waits for it.
#:
#: Two weeks, not the gate's seven days: the app transcribes one recording at
#: a time on a person's own machine, a hundred and forty-seven of them at a
#: few minutes each is a day or two of the machine being on, and a laptop
#: closed over a weekend must not lose a batch that was approved on Friday.
#: A `workflow.wait_condition` with a timeout is server-side state, like the
#: gate's, so nothing is in flight on the worker while it waits.
LOCAL_TRANSCRIPT_DEADLINE = timedelta(days=14)
#: The queue `fetch_object` runs on, derived from the workflow's own. The
#: worker serves it beside its main queue with a small concurrency bound, so
#: a hundred and forty-seven approvals in one sweep open four streams into
#: the container and not a hundred. Derived rather than configured because
#: the workflow's task queue is in its history, which keeps replay honest.
FETCH_QUEUE_SUFFIX = "-audio-fetch"


def fetch_queue_for(task_queue: str) -> str:
    return f"{task_queue}{FETCH_QUEUE_SUFFIX}"


@workflow.defn(name="BucketSyncWorkflow")
class BucketSyncWorkflow:
    @workflow.run
    async def run(self, request: BucketSyncRequest) -> BucketSynced:
        return await workflow.execute_activity(
            bkt.sync_bucket,
            request,
            start_to_close_timeout=SYNC_TIMEOUT,
            heartbeat_timeout=SYNC_HEARTBEAT_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


@workflow.defn(name="MediaLinkWorkflow")
class MediaLinkWorkflow:
    @workflow.run
    async def run(self, request: MediaLinkRequest) -> MediaLink:
        return await workflow.execute_activity(
            bkt.presign_object,
            request,
            start_to_close_timeout=LINK_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=2),
        )


@workflow.defn(name="AudioIngestWorkflow")
class AudioIngestWorkflow(TimedIngest):
    def __init__(self) -> None:
        self._init_state()
        self._upload: LocalTranscript | None = None
        self._switch: bool = False
        self._transcriber: str = ""

    # -- signals and queries ----------------------------------------------

    @workflow.signal
    def approve(self, approval: Approval) -> None:
        """Answer the gate. Same name and payload as the other two ingest
        workflows', which is what lets `/runs/{id}/approve` serve this one
        with no change at all."""
        self._approval = approval

    @workflow.query
    def gate_report(self) -> VideoGateReport | None:
        """The same report type a video run publishes, on the same query name,
        so `GET /runs/{id}/video-gate` reads an audio run unchanged."""
        return self._report

    @workflow.query
    def stage(self) -> str:
        return self._stage

    @workflow.signal
    def transcript_ready(self, upload: LocalTranscript) -> None:
        """The desktop app finished transcribing and uploaded the result.

        Last write wins, like `approve`: a second upload for the same run
        replaces the first before it is staged, and after staging it is
        ignored — the run has moved on.
        """
        self._upload = upload

    @workflow.signal
    def use_remote_transcriber(self) -> None:
        """Give up on the local transcript and pay Amazon instead.

        Not an approval. The gate quoted a local run at $0 for transcription,
        so switching engines is switching to a bill nobody has seen — which is
        why this re-opens the gate with Amazon's price rather than proceeding.
        """
        self._switch = True

    @workflow.query
    def transcriber(self) -> str:
        """Which engine this run is on now: what the app polls to know whether
        a run it was transcribing for has been taken away from it."""
        return self._transcriber

    # -- run ---------------------------------------------------------------

    @workflow.run
    async def run(self, request: AudioRequest, options: StageOptions) -> VideoResult:
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
            await self._finish(
                run_id, "failed", e.type or "workflow_failed", str(e)[:2000]
            )
            raise

    async def _run(
        self, request: AudioRequest, options: StageOptions, run_id: str
    ) -> VideoResult:
        # The run row first, unconditionally — this workflow is younger than
        # the patch that guards the same insert on the video path, so there is
        # no history that predates it to keep replayable.
        await workflow.execute_activity(
            act.open_run,
            RunOpen(
                run_id=run_id,
                workflow_id=workflow.info().workflow_id,
                kind="audio",
                tenant_id=request.tenant_id,
                library_id=request.library_id,
                label=request.title or _label_of(request.key),
            ),
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )
        self._registered = True

        await self._enter(run_id, "probing")
        probe: VideoProbe = await workflow.execute_activity(
            bkt.probe_object,
            args=[request, run_id],
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

        if registered.already_indexed and not request.reindex:
            await workflow.execute_activity(
                act.link_duplicate,
                args=[ingest_request, staged, registered],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=_RETRY,
            )
            await self._finish(run_id, "succeeded")
            return VideoResult(
                run_id=run_id,
                document_id=registered.document_id,
                version_id=registered.version_id,
                state="already_indexed",
                detail="esta grabación ya estaba indexada",
                transcript_source="transcribe",
            )

        # The manifest's dates and feed link, on the document, best-effort.
        # After registration for the reason `record_video_artifacts` runs
        # after it: the write needs a document row to land on.
        await workflow.execute_activity(
            bkt.set_document_dates,
            args=[request, registered],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        if probe.probe_ref is not None:
            await workflow.execute_activity(
                vid.record_video_artifacts,
                args=[run_id, [probe.probe_ref]],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=_RETRY,
            )

        # Nothing to preview: an object has no captions, so there is no text
        # until the money is spent, and `VideoGateReport.preview` says so.
        await self._enter(run_id, "previewing")
        self._transcriber = request.transcriber or "transcribe"
        recommended = _recommended(options)
        await self._quote(run_id, probe, registered, recommended)

        approved = await self._gate(request.auto_approve, recommended, run_id)
        if not approved.approved:
            await self._finish(run_id, "cancelled", "rejected", approved.reason)
            return VideoResult(
                run_id=run_id,
                document_id=registered.document_id,
                version_id=registered.version_id,
                state="rejected",
                detail=approved.reason,
            )
        options = approved.options

        transcribed = await self._transcribe(run_id, request, probe, registered, options)
        # A run switched from a local transcript to Amazon passed a *second*
        # gate, and the switches ticked there are the ones that govern what
        # follows — `_approval` always holds the latest answer.
        if self._approval is not None:
            options = self._approval.options
        result = await self._index_transcript(
            run_id, request.library_id, ingest_request, staged, registered,
            probe, transcribed, options,
        )
        # The corrected transcript goes back to the customer, after the index
        # exists and not before: it is written *from* what was indexed, so a
        # run that failed at chunking has nothing honest to archive. Appended
        # after `_index_transcript` rather than inside it because the shared
        # tail serves videos too, and a video has no bucket to write to.
        #
        # Conditional on `correction_prefix`, which is empty by default, so a
        # history in flight — a gate parked for seven days — decodes a payload
        # that never carried the field as "off" and issues no command at all.
        # That is what keeps this replay-safe without a `workflow.patched`.
        if request.source.correction_prefix and options.correct:
            await self._enter(run_id, "archiving", detail="corrección")
            await workflow.execute_activity(
                bkt.archive_correction,
                args=[request, registered, run_id],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        return result

    async def _quote(
        self,
        run_id: str,
        probe: VideoProbe,
        registered: Registered,
        recommended: StageOptions,
    ) -> None:
        """Publish the gate's report for the engine the run is on now.

        Called once before the gate and again when a run parked for a local
        transcript is switched to Amazon: the estimate is the only thing that
        changes between the two, and it is the thing the person is asked to
        approve, so it is recomputed rather than patched.
        """
        characters = projected_characters(probe.duration_s)
        characters_high = projected_characters(probe.duration_s, high=True)
        chunk_count = max(1, characters // ChunkRules().target_chars)
        estimate: Estimate = await workflow.execute_activity(
            bkt.estimate_audio,
            args=[probe, recommended, characters, chunk_count, characters_high,
                  run_id, self._transcriber],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )
        self._report = VideoGateReport(
            run_id=run_id,
            document_id=registered.document_id,
            version_id=registered.version_id,
            probe=probe,
            estimate=estimate,
            preview=None,
            transcript=None,
            warnings=list(probe.warnings),
            recommended=recommended,
        )

    async def _transcribe(
        self,
        run_id: str,
        request: AudioRequest,
        probe: VideoProbe,
        registered: Registered,
        options: StageOptions,
    ) -> Transcribed:
        """The archive if it has one; else the object to S3, a job, a wait, and
        the transcript written back — or, for a local run, a wait for the app."""
        await self._enter(run_id, "fetching")
        archived: ArtifactRef | None = await workflow.execute_activity(
            bkt.check_archive,
            args=[request, probe, run_id],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )
        if archived is not None:
            # Entered so the `transcription_result` artifact is attributed to
            # the stage that owns it on both paths, with the detail saying why
            # nothing was paid.
            await self._enter(
                run_id, "transcribing",
                detail="transcripción reutilizada del archivo del bucket; sin cargo",
            )
            return await self._group(run_id, probe, archived)

        if self._transcriber == bkt.LOCAL_TRANSCRIBER:
            local = await self._await_local_transcript(
                run_id, request, probe, registered, options
            )
            if local is not None:
                return local
            # Switched to Amazon while parked. The gate re-opened with the
            # price and was approved, or `_await_local_transcript` raised.

        audio: AudioStaged = await workflow.execute_activity(
            bkt.fetch_object,
            args=[run_id, request, probe, registered.version_id],
            task_queue=fetch_queue_for(workflow.info().task_queue),
            start_to_close_timeout=AUDIO_TIMEOUT,
            heartbeat_timeout=AUDIO_HEARTBEAT_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        language = _language_for(request)
        result = await self._await_transcription(
            run_id, probe, audio, request.tenant_id, registered.version_id, language
        )
        job_name = vid.job_name_for(registered.version_id)
        await self._archive(run_id, request, probe, audio, _job_ref(job_name), result,
                            language, registered.version_id)
        return await self._group(run_id, probe, result)

    async def _await_local_transcript(
        self,
        run_id: str,
        request: AudioRequest,
        probe: VideoProbe,
        registered: Registered,
        options: StageOptions,
    ) -> Transcribed | None:
        """Park until the desktop app uploads a transcript, or until somebody
        gives up on it and switches the run to Amazon.

        `None` means the switch: the gate has been re-opened with Amazon's
        price and approved, and the caller continues down the paid path. A
        rejection at that gate is a cancelled run, exactly like a rejection at
        the first one.
        """
        await self._enter(
            run_id, "transcribing", "awaiting_transcript",
            detail="esperando la transcripción hecha en el equipo de la aplicación",
        )
        try:
            await workflow.wait_condition(
                lambda: self._upload is not None or self._switch,
                timeout=LOCAL_TRANSCRIPT_DEADLINE,
            )
        except TimeoutError:
            raise ApplicationError(
                f"ninguna aplicación subió la transcripción en "
                f"{LOCAL_TRANSCRIPT_DEADLINE.days} días",
                type="local_transcript_timeout",
                non_retryable=True,
            )
        if self._upload is None:
            # Switched. Re-quote and re-park at the gate, so the price Amazon
            # will charge is the price somebody approves.
            self._switch = False
            self._transcriber = "transcribe"
            self._approval = None
            # The switches the run was approved with, re-recommended: the
            # engine changed, not what the person asked the pipeline to do.
            recommended = _recommended(options)
            await self._quote(run_id, probe, registered, recommended)
            approved = await self._gate(request.auto_approve, recommended, run_id)
            if not approved.approved:
                await self._finish(run_id, "cancelled", "rejected", approved.reason)
                raise ApplicationError(
                    approved.reason or "rechazado en la segunda compuerta",
                    type="rejected", non_retryable=True,
                )
            return None

        upload = self._upload
        await self._enter(run_id, "transcribing", detail=f"{upload.engine} {upload.model}".strip())
        result: ArtifactRef = await workflow.execute_activity(
            bkt.stage_transcript,
            args=[run_id, upload, request.tenant_id, registered.version_id],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )
        job = TranscriptionJob(
            job_name="local", status="COMPLETED",
            language=upload.language or _language_for(request),
            engine=f"{upload.engine}/{upload.model}".rstrip("/"),
        )
        staged = AudioStaged(s3_uri="", media_format="", bytes=0,
                             seconds=probe.duration_s, sha256="")
        await self._archive(run_id, request, probe, staged, job, result, job.language,
                            registered.version_id)
        return await self._group(run_id, probe, result)

    async def _archive(
        self,
        run_id: str,
        request: AudioRequest,
        probe: VideoProbe,
        audio: AudioStaged,
        job: TranscriptionJob,
        result: ArtifactRef,
        language: str,
        version_id: str,
    ) -> None:
        """Write the transcript back into the customer's bucket, best-effort,
        whichever engine made it. A failure is a line on the trail."""
        await self._enter(run_id, "archiving")
        written: bool = await workflow.execute_activity(
            bkt.archive_transcript,
            args=[request, probe, audio, job, result, run_id, version_id, language],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        if not written and request.source.archive_prefix:
            await self._enter(
                run_id, "archiving",
                detail="la transcripción no se pudo escribir en el bucket del "
                       "cliente; queda como artefacto de esta ejecución",
            )


# --- pure helpers ---------------------------------------------------------------


def _label_of(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _job_ref(job_name: str) -> TranscriptionJob:
    """The job as the archive's sidecar names it. Its name is derived from
    the version, so it need not be carried out of the wait."""
    return TranscriptionJob(job_name=job_name, status="COMPLETED")


def _as_ingest_request(request: AudioRequest, probe: VideoProbe) -> IngestRequest:
    """The shape every reused activity expects. `source_path` is the `s3://`
    URL, which is what `document.source_path` is for — a place to read the
    thing again — and nothing on this path hands it to `stage_source`."""
    return IngestRequest(
        library_id=request.library_id,
        source_path=probe.canonical_url,
        source_key=probe.source_key,
        title=probe.title,
        author=request.author or probe.channel or None,
        tenant_id=request.tenant_id,
        library_name=request.library_name,
        reindex=request.reindex,
        run_kind="audio",
    )


def _as_staged(probe: VideoProbe) -> Staged:
    """`fmt` is what `_locator` branches on to render a timestamp instead of a
    byte range; `byte_size` is 0 for the reason the video path gives — the
    duration must not become a term in every "corpus size" sum."""
    return Staged(
        content_sha256=probe.content_sha256,
        byte_size=0,
        fmt=AUDIO_FORMAT,
        extractor="aws_transcribe",
        title=probe.title,
    )


def _recommended(options: StageOptions) -> StageOptions:
    """The switches the gate opens with.

    Three stages this path has no activity for are forced off, exactly as
    `workflows/video._recommended` does for a video with no captions.
    Everything else — `extract_semantics` and now `correct` — **passes
    through**, so the number quoted is the number spent.

    Correction used to be forced to `correction_default(None)`, which is False
    because a machine transcript arrives punctuated. That was the right
    *default* and the wrong place for it: the quote is made from this
    recommendation, so a client that ticked correction at the gate got a run
    spending on a stage the estimate never covered — the recorded
    `VideoGateReview` defect, in which the app forced `extract_semantics: false`
    at approval over a gate that had quoted it, from the other direction.
    The default now lives where the choice is made: both clients send
    `correct: false` in the switches they probe with, and a person ticks it
    *before* quoting, which is the only moment at which the answer can still
    reach the estimate.

    What that costs is a caller who builds `StageOptions()` by hand and sends
    it unexamined: the dataclass defaults `correct` to True, so such a caller
    now quotes and runs correction where it used to be dropped silently. That
    is the better of the two failures — it is what they asked for, and it is
    priced.
    """
    return replace(
        options,
        learn_profile=False,
        generate_evalset=False,
        tune=False,
    )


def _language_for(request: AudioRequest) -> str:
    lang = request.source.language or "es-US"
    return lang if "-" in lang else f"{lang}-{lang.upper()}"
