"""The video workflow, its gate, and the poll loop that waits for Amazon.

Activities are mocked. What is under test is the workflow's own reasoning: that
nothing reaches Amazon before somebody approves it, that a video with captions
never touches AWS at all, that a duplicate stops before the bill, and that the
wait for a transcription job is a timer the host can sleep through.

Every double is **typed**. Temporal maps payloads onto parameters by arity, so a
`*args` double accepts a call the real converter cannot make — which is how five
passing tests once hid a workflow that could not run.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.artifacts import ArtifactRef
from brainworker.pipeline import (
    AudioStaged,
    RunOpen,
    CaptionTrack,
    ChunkKindCount,
    Chunked,
    Correction,
    Estimate,
    Extraction,
    Indexed,
    IngestRequest,
    Preview,
    Registered,
    Semantics,
    Spend,
    StageEstimate,
    StageOptions,
    Staged,
    Transcribed,
    TranscriptionJob,
    VideoInfo,
    VideoProbe,
    VideoRequest,
)
from brainworker.workflows.ingest import Approval
from brainworker.workflows.video import VideoIngestWorkflow, failure_of

TASK_QUEUE = "test-video"
VID = "dQw4w9WgXcQ"

#: What reached AWS or a paid model. The assertion that matters most in this
#: file is that it stays empty on a run nobody approved.
SPENT: list[str] = []
CALLED: list[str] = []
POLLS: list[str] = []

TEXT_REF = ArtifactRef(kind="transcript_text", path="runs/r/transcript.txt",
                       sha256="a" * 64, bytes=100)
CUES_REF = ArtifactRef(kind="transcript", path="runs/r/transcript.json",
                       sha256="b" * 64, bytes=50)
CHUNKS_REF = ArtifactRef(kind="chunks", path="runs/r/chunks.jsonl",
                         sha256="c" * 64, bytes=80, rows=4)
PROBE_REF = ArtifactRef(kind="video_probe", path="runs/r/video-probe.json",
                        sha256="e" * 64, bytes=200)
CAPTIONS_REF = ArtifactRef(kind="captions", path="runs/r/captions.vtt",
                           sha256="f" * 64, bytes=300)
EVIDENCE_REF = ArtifactRef(kind="evidence", path="runs/r/evidence.json",
                           sha256="9" * 64, bytes=90)
PREVIEW_REF = ArtifactRef(kind="preview_chunks", path="runs/r/chunks.preview.jsonl",
                          sha256="d" * 64, bytes=40, rows=4)


def request(**kw) -> VideoRequest:
    return VideoRequest(library_id="lib_videos", url=f"https://youtu.be/{VID}", **kw)


def probe(with_captions: bool = True, kind: str = "manual") -> VideoProbe:
    track = CaptionTrack(language="es", kind=kind, ext="vtt")
    return VideoProbe(
        video_id=VID,
        canonical_url=f"https://youtu.be/{VID}",
        source_key=f"youtube/{VID}",
        title="Charla sobre hermenéutica",
        channel="Canal",
        duration_s=1800,
        upload_date="20260101",
        content_sha256="e" * 64,
        identity_basis="youtube\n" + VID,
        tracks=[track] if with_captions else [],
        chosen=track if with_captions else None,
        probe_ref=PROBE_REF,
        captions=CAPTIONS_REF if with_captions else None,
    )


# -- typed doubles -----------------------------------------------------------


def info_for(with_captions: bool = True, kind: str = "manual") -> VideoInfo:
    track = CaptionTrack(language="es", kind=kind, ext="vtt")
    return VideoInfo(
        video_id=VID,
        title="Charla sobre hermenéutica",
        channel="Canal",
        duration_s=1800,
        upload_date="20260101",
        format_id="140",
        tracks=[track] if with_captions else [],
        chosen=track if with_captions else None,
        caption_url="https://www.youtube.com/api/timedtext?x=1" if with_captions else "",
    )


def resolver(with_captions: bool = True, kind: str = "manual"):
    @activity.defn(name="resolve_video")
    async def _resolve(req: VideoRequest) -> VideoInfo:
        assert isinstance(req, VideoRequest), f"got {type(req).__name__}"
        CALLED.append("resolve")
        return info_for(with_captions, kind)

    return _resolve


def prober(with_captions: bool = True, kind: str = "manual"):
    @activity.defn(name="probe_video")
    async def _probe(
        req: VideoRequest, run_id: str, info: VideoInfo
    ) -> VideoProbe:
        assert isinstance(req, VideoRequest), f"got {type(req).__name__}"
        # Typed, and asserted: the converter maps payloads by arity, so an
        # untyped double would accept a call the real worker cannot make.
        assert isinstance(info, VideoInfo), f"got {type(info).__name__}"
        CALLED.append("probe")
        return probe(with_captions, kind)

    return _probe


def register(already_indexed: bool = False):
    @activity.defn(name="register_document")
    async def _register(
        req: IngestRequest, staged: Staged, run_id: str, workflow_id: str
    ) -> Registered:
        # The two facts the video path is responsible for getting right.
        assert isinstance(req, IngestRequest), f"got {type(req).__name__}"
        assert req.run_kind == "video", req.run_kind
        assert staged.fmt == "youtube", staged.fmt
        return Registered(
            document_id="doc_v", version_id="ver_v",
            created=True, already_indexed=already_indexed,
        )

    return _register


#: What the run row was opened with, so a test can assert it exists at all.
OPENED: list[RunOpen] = []


@activity.defn(name="open_run")
async def open_run(opening: RunOpen) -> None:
    OPENED.append(opening)
    CALLED.append("open_run")


@activity.defn(name="record_run_events")
async def record_run_events(run_id: str, pending: list) -> None:
    return None


@activity.defn(name="set_run_stage")
async def set_run_stage(
    run_id: str, stage: str, state: str, seq, at, detail: str | None = None
) -> None:
    CALLED.append(f"stage:{stage}" + (f"|{detail}" if detail else ""))


@activity.defn(name="record_run_outcome")
async def record_run_outcome(
    run_id: str, state: str, kind, detail, seq, at, stage: str
) -> None:
    CALLED.append(f"outcome:{state}")


@activity.defn(name="link_duplicate")
async def link_duplicate(
    req: IngestRequest, staged: Staged, registered: Registered
) -> None:
    CALLED.append("link_duplicate")


@activity.defn(name="record_video_artifacts")
async def record_video_artifacts(run_id: str, refs: list[ArtifactRef]) -> None:
    # Real references, never fabricated ones: `ArtifactStore.read_bytes`
    # verifies the sha256, so a locator built from a path and an empty hash
    # fails the check it exists to pass. That is not hypothetical — it is what
    # stopped the first real run dead, in `grouping`.
    assert all(len(r.sha256) == 64 for r in refs), refs
    CALLED.append("record_artifacts")


@activity.defn(name="group_transcript")
async def group_transcript(
    run_id: str, p: VideoProbe, source: ArtifactRef
) -> Transcribed:
    assert isinstance(p, VideoProbe), f"got {type(p).__name__}"
    CALLED.append(f"group:{source.kind}")
    assert len(source.sha256) == 64, f"fabricated ref: {source}"
    return Transcribed(
        text=TEXT_REF, cues=CUES_REF, evidence=EVIDENCE_REF,
        source="captions:es:manual" if p.chosen else "transcribe",
        paragraphs=40, characters=16_000, covered_s=1795.0,
    )


@activity.defn(name="preview_transcript")
async def preview_transcript(run_id: str, t: Transcribed) -> Preview:
    return Preview(
        text=TEXT_REF, chunks=PREVIEW_REF, chunk_count=13,
        kinds=[ChunkKindCount("transcripcion", 13)], characters=t.characters,
        chunks_are_final=False, warnings=[],
    )


QUOTED: list[StageOptions] = []
#: What `chunk_transcript` should report this time. A list rather than a flag
#: because the activity carries whatever it found, and a test that could only
#: switch warnings on would not show that both of them travel.
CHUNK_WARNINGS: list[str] = []


@activity.defn(name="estimate_video")
async def estimate_video(
    p: VideoProbe,
    options: StageOptions,
    characters: int,
    chunk_count: int,
    characters_high: int,
    run_id: str = "",
) -> Estimate:
    assert isinstance(p, VideoProbe), f"got {type(p).__name__}"
    # The gate's quote is persisted as an artifact now, so the activity has to
    # be told which run it belongs to.
    assert run_id, "estimate_video was not told its run"
    QUOTED.append(options)
    # With captions the text is counted, not projected, so there is no range to
    # draw around it; without them there must be one, because that count is the
    # most uncertain input in the product.
    assert (characters_high > characters) == (p.chosen is None), (
        f"chosen={p.chosen} but characters_high={characters_high} vs {characters}"
    )
    rows = [StageEstimate("correction", "gemini-3.6-flash", 4444, 4444, 0.006)]
    if p.chosen is None:
        rows.insert(0, StageEstimate("transcription", "aws-transcribe-batch",
                                     0, 0, 0.72, 0, 0.72))
    return Estimate(stages=rows, total_usd=sum(r.usd for r in rows),
                    price_source="published rates", total_usd_high=None)


@activity.defn(name="fetch_audio")
async def fetch_audio(
    run_id: str, p: VideoProbe, tenant_id: str, version_id: str
) -> AudioStaged:
    SPENT.append("audio")
    CALLED.append("fetch_audio")
    return AudioStaged(s3_uri="s3://b/k.m4a", media_format="mp4",
                       bytes=1000, seconds=p.duration_s)


@activity.defn(name="stage_audio")
async def stage_audio(
    run_id: str, audio_path: str, p: VideoProbe, tenant_id: str, version_id: str
) -> AudioStaged:
    # Typed like the real activity, which is not decoration: the converter maps
    # payloads onto parameters **by arity**, so an untyped `*args` double
    # accepts a call the real worker could not make. Five workflow tests once
    # passed against a workflow the real converter could not run.
    assert audio_path, "the workflow must pass the path the client staged"
    CALLED.append("stage_audio")
    return AudioStaged(s3_uri="s3://b/staged.m4a", media_format="mp4",
                       bytes=2000, seconds=p.duration_s)


@activity.defn(name="discard_audio")
async def discard_audio(audio_path: str, tenant_id: str) -> None:
    CALLED.append("discard_audio")


@activity.defn(name="start_transcription")
async def start_transcription(
    run_id: str, p: VideoProbe, audio: AudioStaged,
    tenant_id: str, version_id: str, language: str,
) -> TranscriptionJob:
    assert isinstance(audio, AudioStaged), f"got {type(audio).__name__}"
    assert language == "es-ES", language
    SPENT.append("transcription")
    return TranscriptionJob(job_name=f"brain-{version_id}", status="IN_PROGRESS")


def poller(statuses: list[str]):
    remaining = list(statuses)

    @activity.defn(name="poll_transcription")
    async def _poll(job_name: str) -> TranscriptionJob:
        POLLS.append(job_name)
        status = remaining.pop(0) if remaining else "COMPLETED"
        return TranscriptionJob(
            job_name=job_name, status=status,
            failure_reason="el audio no se pudo leer" if status == "FAILED" else "",
        )

    return _poll


@activity.defn(name="collect_transcript")
async def collect_transcript(
    run_id: str, job: TranscriptionJob, tenant_id: str, version_id: str
) -> ArtifactRef:
    return ArtifactRef(kind="transcription_result",
                       path="runs/r/transcription-result.json",
                       sha256="7" * 64, bytes=200)


@activity.defn(name="abandon_transcription")
async def abandon_transcription(job_name: str) -> None:
    CALLED.append("abandon")


@activity.defn(name="correct_text")
async def correct_text(run_id: str, extraction: Extraction) -> Correction:
    assert isinstance(extraction, Extraction), f"got {type(extraction).__name__}"
    # The flag that stops a corrected paragraph splitting in two and taking
    # every later timestamp with it.
    assert extraction.single_line_paragraphs is True
    # The evidence reference has to verify too — `correct_text` reads through
    # the store, and a fabricated one would fail there instead of here.
    assert len(extraction.evidence.sha256) == 64
    SPENT.append("correction")
    return Correction(
        text=ArtifactRef(kind="corrected_text", path="runs/r/corrected.txt",
                         sha256="1" * 64, bytes=110),
        report=ArtifactRef(kind="correction_report", path="runs/r/correction-report.json",
                           sha256="2" * 64, bytes=30),
        paragraphs=40, changed=12, rejected=0, missing=0, cache_hits=0,
        spend=Spend("correction", "gemini-3.6-flash", 4000, 4000, 0.006),
    )


@activity.defn(name="chunk_transcript")
async def chunk_transcript(
    run_id: str, text: ArtifactRef, cues: ArtifactRef, fallback: ArtifactRef | None
) -> Chunked:
    # The uncorrected stream must always travel as the fallback, or a correction
    # that moved the paragraph count has nowhere safe to land.
    assert fallback is not None and fallback.kind == "transcript_text"
    return Chunked(chunks=CHUNKS_REF, count=13,
                   kinds=[ChunkKindCount("transcripcion", 13)],
                   warnings=list(CHUNK_WARNINGS))


@activity.defn(name="extract_semantics")
async def extract_semantics(run_id: str, registered, chunked, options=None):
    CALLED.append("extract_semantics")
    SPENT.append("semantics")
    return Semantics(
        concepts=2, claims=1, edges=3,
        spend=Spend(stage="semantics", model="gemini-3.6-flash",
                    input_tokens=10, output_tokens=10, usd=0.4274),
    )


@activity.defn(name="project_structure")
async def project_structure(
    req: IngestRequest, staged: Staged, registered: Registered,
    run_id: str, chunks: ArtifactRef,
) -> dict:
    return {"chunks": 13, "citations": 13}


BOOKS: list[str] = []


@activity.defn(name="resolve_book_metadata")
async def resolve_book_metadata(
    run_id: str, registered: Registered, chunks: ArtifactRef
) -> Spend | None:
    """`None`, and for a video that is not a stub but the real behaviour:
    `register_video` fills the author from the channel, so `needs_metadata` is
    already false by the time this runs and nothing is ever asked."""
    return None


@activity.defn(name="build_epub")
async def build_epub(
    run_id: str, library_id: str, registered: Registered, chunks: ArtifactRef
) -> ArtifactRef:
    BOOKS.append(chunks.path)
    return ArtifactRef(kind="epub", path=f"runs/{run_id}/book.epub",
                       sha256="b" * 64, bytes=4096)


@activity.defn(name="embed_and_index")
async def embed_and_index(
    run_id: str, library_id: str, registered: Registered,
    staged: Staged, chunked: Chunked,
) -> Indexed:
    SPENT.append("embedding")
    return Indexed(collection="brain", points=13, dimensions=3072,
                   spend=Spend("embedding", "gemini-embedding-2", 4000, 0, 0.0004))


@activity.defn(name="activate_version")
async def activate_version(
    req: IngestRequest, staged: Staged, registered: Registered
) -> None:
    CALLED.append("activate")


def activities(*, captions=True, already_indexed=False, statuses=None,
               kind="manual"):
    return [
        open_run, resolver(captions, kind),
        prober(captions, kind), register(already_indexed), record_run_events,
        set_run_stage, record_run_outcome, link_duplicate, record_video_artifacts,
        group_transcript, preview_transcript, estimate_video, fetch_audio,
        stage_audio, discard_audio,
        start_transcription, poller(statuses or []), collect_transcript,
        abandon_transcription, correct_text, chunk_transcript, project_structure,
        resolve_book_metadata, build_epub,
        embed_and_index, extract_semantics, activate_version,
    ]


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture(autouse=True)
def _clear():
    SPENT.clear(); CALLED.clear(); POLLS.clear(); QUOTED.clear(); OPENED.clear()
    CHUNK_WARNINGS.clear(); BOOKS.clear()
    yield
    SPENT.clear(); CALLED.clear(); POLLS.clear(); QUOTED.clear(); OPENED.clear()
    CHUNK_WARNINGS.clear(); BOOKS.clear()


async def _start(env: WorkflowEnvironment, req: VideoRequest, opts: StageOptions):
    client: Client = env.client
    return await client.start_workflow(
        VideoIngestWorkflow.run,
        args=[req, opts],
        id=f"video-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )


# -- the gate ----------------------------------------------------------------


async def test_nothing_reaches_amazon_before_someone_approves(env):
    """The property this whole workflow is shaped around.

    A video with no captions has to be transcribed, and transcription is billed
    per second of audio — so a pipeline that downloaded and submitted first
    would have spent the money it was about to ask about.
    """
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(captions=False)):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        assert SPENT == []
        assert "fetch_audio" not in CALLED
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        result = await handle.result()

    assert result.state == "rejected"
    assert SPENT == [], "a rejected gate must cost nothing"
    assert "fetch_audio" not in CALLED


async def test_the_gate_shows_a_real_preview_when_captions_made_one_free(env):
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        report = await _wait_for_gate(handle)
        assert report.preview is not None
        assert report.preview.chunk_count == 13
        assert report.transcript is not None
        # Correction changes the text's length, so the previewed chunks are not
        # the chunks that will be indexed — the same rule the document gate obeys.
        assert report.preview.chunks_are_final is False
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        await handle.result()


async def test_the_gate_admits_it_has_nothing_to_preview_without_captions(env):
    """`None`, not a fabricated count.

    There is no text until the money is spent, and inventing a chunk count from
    a duration guess is precisely the lie a gate exists to prevent.
    """
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(captions=False)):
        handle = await _start(env, request(), StageOptions())
        report = await _wait_for_gate(handle)
        assert report.preview is None
        assert report.transcript is None
        assert [s.stage for s in report.estimate.stages][0] == "transcription"
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        await handle.result()


# -- the two paths -----------------------------------------------------------


async def test_a_video_with_captions_never_touches_aws(env):
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=True))
        result = await handle.result()

    assert result.state == "indexed"
    assert result.transcript_source == "captions:es:manual"
    assert "fetch_audio" not in CALLED
    assert "transcription" not in SPENT
    assert "stage:fetching" not in CALLED
    assert "stage:transcribing" not in CALLED
    assert "group:captions" in CALLED


async def test_a_video_without_captions_polls_until_the_job_completes(env):
    """The wait is a workflow timer, so this runs instantly under time skipping —
    which is the same property that lets the real one sleep through the host's
    nightly stop."""
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(
                          captions=False,
                          statuses=["IN_PROGRESS", "IN_PROGRESS", "COMPLETED"])):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=True))
        result = await handle.result()

    assert result.state == "indexed"
    assert result.transcript_source == "transcribe"
    assert len(POLLS) == 3
    assert POLLS[0] == "brain-ver_v", "the job name must be derived from the version"
    assert SPENT.count("transcription") == 1
    assert "group:transcription_result" in CALLED


async def test_a_failed_job_is_deleted_and_the_run_fails_with_its_reason(env):
    """The name is derived from the version and Amazon keeps it 90 days, so a
    failed job left behind makes every later attempt a ConflictException
    reporting this failure."""
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(captions=False, statuses=["FAILED"])):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=True))
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    assert "abandon" in CALLED
    assert "outcome:failed" in CALLED
    assert "embedding" not in SPENT


# -- convergence -------------------------------------------------------------


async def test_a_video_already_indexed_stops_before_the_bill(env):
    """The identity is computed before the gate precisely so this can happen
    without paying to transcribe the same video twice."""
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(captions=False, already_indexed=True)):
        handle = await _start(env, request(), StageOptions())
        result = await handle.result()

    assert result.state == "already_indexed"
    assert SPENT == []
    assert "link_duplicate" in CALLED
    assert "fetch_audio" not in CALLED


async def test_a_reindex_goes_through_even_when_the_content_is_unchanged(env):
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(already_indexed=True)):
        handle = await _start(env, request(reindex=True), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=True))
        result = await handle.result()

    assert result.state == "indexed"
    assert "link_duplicate" not in CALLED


async def test_auto_approve_skips_the_gate_without_skipping_the_estimate(env):
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(auto_approve=True), StageOptions())
        result = await handle.result()

    assert result.state == "indexed"
    assert "stage:awaiting_approval" not in CALLED


# -- where the two YouTube calls run -----------------------------------------


FETCH_QUEUE = "test-video-fetch"


async def test_the_two_youtube_calls_run_on_the_fetch_queue_when_one_is_named(env):
    """The split, asserted by *withholding* the activity from the main worker.

    This is the only shape that can prove routing. A test that registered
    `resolve_video` on both queues would pass whether or not the workflow routes
    it, because either worker could serve it. Here the main worker does not have
    it: if `task_queue=` is dropped from the call, the activity is never claimed
    on the main queue, the schedule-to-start timeout fires and the run fails.

    Measured 2026-09-05, and the reason this exists: `yt-dlp extract_info` is
    refused from the EC2 egress IP with "Sign in to confirm you're not a bot"
    while succeeding from a residential one in 2.6 s. Everything else — the
    caption download included, since a caption URL carries `ip=0.0.0.0` and was
    served to that same host at 200 — stays where the workspace is.
    """
    main = [a for a in activities() if getattr(a, "__temporal_activity_definition", None)
            and a.__temporal_activity_definition.name != "resolve_video"]
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=main), \
            Worker(env.client, task_queue=FETCH_QUEUE, activities=[resolver()]):
        handle = await _start(env, request(fetch_queue=FETCH_QUEUE), StageOptions())
        report = await _wait_for_gate(handle)
        assert report.probe.video_id == VID
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        await handle.result()

    assert "resolve" in CALLED and "probe" in CALLED


async def test_an_empty_fetch_queue_means_this_one_and_changes_nothing(env):
    """The default. One worker, no second queue, exactly the old behaviour.

    `fetch_queue` is empty for every existing caller, so the workflow falls back
    to `workflow.info().task_queue` and the whole product is unchanged until
    somebody deliberately sets it.
    """
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        await handle.result()

    assert CALLED.index("resolve") < CALLED.index("probe")


async def test_a_video_the_client_already_resolved_never_asks_youtube_again(env):
    """The client-side answer to the same measurement `fetch_queue` answers.

    `resolve_video` is **registered** here and asserted not to have been
    called, which is the opposite shape from the fetch-queue test below and
    deliberately so. That one withholds the activity because withholding is the
    only way to prove *routing*; here the question is whether it ran at all, and
    the double's own `CALLED.append` answers that in milliseconds. Withholding
    would answer it too — by hanging for the ten real minutes of a
    schedule-to-start timeout the time-skipping environment does not skip.

    This is what makes paid mode work at all. `extract_info` is refused from a
    datacentre address — measured 2026-09-05, "Sign in to confirm you're not a
    bot" — so the desktop app makes that one call on the machine the person is
    sitting at and sends the result, which `VideoInfo` was already narrowed
    enough to allow: 1,656,277 bytes of raw info dict against a few kilobytes.
    """
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(resolved=info_for()), StageOptions())
        report = await _wait_for_gate(handle)
        assert report.probe.video_id == VID
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        await handle.result()

    assert "resolve" not in CALLED
    assert "probe" in CALLED, "everything after the one refused call still runs here"


async def test_audio_the_client_staged_is_moved_to_s3_rather_than_downloaded_again(env):
    """The other half, and it cannot be served by the same activity.

    A `googlevideo` media URL carries the address that resolved it and answers
    403 anywhere else — measured — so when the app made the `extract_info` call
    the app is also the only thing that can make this one. What is left for the
    server is putting the bytes where Transcribe can read them, which needs the
    instance role a laptop does not have.

    `fetch_audio` is registered here on purpose: the assertion is that it is not
    *called*, not that it is unavailable. Getting the branch backwards would
    re-download from a host YouTube's signed URL refuses, and the run would fail
    at the one stage that has already cost the user their bandwidth.
    """
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(captions=False, statuses=["COMPLETED"])):
        handle = await _start(
            env,
            request(resolved=info_for(with_captions=False),
                    audio_path="/workspace/tenants/t/inbox/audio.m4a"),
            StageOptions(correct=False, embed=False),
        )
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=True))
        await handle.result()

    assert "stage_audio" in CALLED
    assert "fetch_audio" not in CALLED


async def test_without_a_staged_path_the_worker_downloads_the_audio_as_it_always_did(env):
    """The default, unchanged. Local mode never sets `audio_path`, because the
    container's egress *is* the machine's egress and YouTube answers it."""
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(captions=False, statuses=["COMPLETED"])):
        handle = await _start(env, request(), StageOptions(correct=False, embed=False))
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=True))
        await handle.result()

    assert "fetch_audio" in CALLED
    assert "stage_audio" not in CALLED


async def test_a_rejected_gate_throws_away_the_audio_the_client_had_already_staged(env):
    """The cost of downloading before the gate, paid back.

    The download is free in dollars, which is why it can happen before anybody
    approves anything — and why the alternative was rejected: parking the
    workflow after approval until a laptop sends the bytes makes a run that
    stalls silently when the window is closed. What it leaves behind is up to a
    gigabyte in the inbox, and `stage_audio`'s own `finally` never runs on this
    path.
    """
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(captions=False)):
        handle = await _start(
            env,
            request(resolved=info_for(with_captions=False),
                    audio_path="/workspace/tenants/t/inbox/audio.m4a"),
            StageOptions(),
        )
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        await handle.result()

    assert "discard_audio" in CALLED
    assert "stage_audio" not in CALLED


async def test_a_run_that_transcribes_does_not_discard_its_own_audio(env):
    """The obvious way to get the previous test wrong. `stage_audio` deletes
    the file itself, so discarding as well would be a second unlink of a path
    that by then names nothing — harmless here and a bug the day the two
    orders differ."""
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(captions=False, statuses=["COMPLETED"])):
        handle = await _start(
            env,
            request(resolved=info_for(with_captions=False),
                    audio_path="/workspace/tenants/t/inbox/audio.m4a"),
            StageOptions(correct=False, embed=False),
        )
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=True))
        await handle.result()

    assert "stage_audio" in CALLED
    assert "discard_audio" not in CALLED


def test_a_task_nobody_claimed_is_named_rather_than_called_a_failed_activity():
    """"Nobody is running the fetcher" must not read as a broken activity.

    Asserted on the pure classifier rather than through a real run, and that is
    not a shortcut: a schedule-to-start timeout is ten minutes, and the
    time-skipping environment does **not** skip it — an activity nobody claimed
    still counts as one in flight, so the test hangs for the full wall clock.
    The classification is the part worth asserting; `_record_failure` around it
    is one `execute_activity` call.

    The `TimeoutType` comparison is the reason this test exists at all.
    `TimeoutType` is an `IntEnum`, so a name match on `str(cause.type)` reads
    `"2"` and silently never fires — exactly the trap `event_type` set for the
    raw-history translation.
    """
    from temporalio.exceptions import TimeoutType

    unclaimed = ActivityError(
        "activity error", scheduled_event_id=1, started_event_id=2,
        identity="", activity_type="resolve_video", activity_id="1",
        retry_state=None,
    )
    unclaimed.__cause__ = TemporalTimeoutError(
        "activity timeout", type=TimeoutType.SCHEDULE_TO_START,
        last_heartbeat_details=[],
    )
    kind, detail = failure_of(unclaimed)
    assert kind == "fetch_worker_unavailable"
    assert "worker de descarga" in detail

    # A start-to-close timeout is an activity that ran and did not finish, which
    # is a different thing and must not borrow the fetcher's message.
    slow = ActivityError(
        "activity error", scheduled_event_id=1, started_event_id=2,
        identity="", activity_type="resolve_video", activity_id="1",
        retry_state=None,
    )
    slow.__cause__ = TemporalTimeoutError(
        "activity timeout", type=TimeoutType.START_TO_CLOSE,
        last_heartbeat_details=[],
    )
    assert failure_of(slow)[0] == "activity_failed"


def test_the_kind_an_activity_chose_survives_to_the_catalog():
    """`youtube_refused_this_host` is not `video_unavailable`, and the queue's
    guidance is keyed on the difference."""
    refused = ActivityError(
        "activity error", scheduled_event_id=1, started_event_id=2,
        identity="", activity_type="resolve_video", activity_id="1",
        retry_state=None,
    )
    refused.__cause__ = ApplicationError(
        "Sign in to confirm you're not a bot.",
        type="youtube_refused_this_host", non_retryable=True,
    )
    assert failure_of(refused)[0] == "youtube_refused_this_host"


async def _wait_for_gate(handle):
    """Poll the query until the free stages have produced a report."""
    import asyncio

    for _ in range(200):
        report = await handle.query(VideoIngestWorkflow.gate_report)
        if report is not None:
            return report
        await asyncio.sleep(0.05)
    raise AssertionError("the gate report never appeared")


# -- what the gate opens with ------------------------------------------------


async def test_the_gate_suggests_correction_only_for_automatic_captions(env):
    """Auto-captions arrive with no punctuation, which is the one deficit
    correction can close without `verify` rejecting the change. A manual track
    is already punctuated and is not worth the dominant stage of the bill."""
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(kind="auto")):
        handle = await _start(env, request(), StageOptions())
        report = await _wait_for_gate(handle)
        assert report.recommended.correct is True
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        await handle.result()

    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(kind="manual")):
        handle = await _start(env, request(), StageOptions())
        report = await _wait_for_gate(handle)
        assert report.recommended.correct is False
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        await handle.result()


async def test_the_gate_never_suggests_a_stage_this_workflow_does_not_have(env):
    """Leaving them on would quote work that cannot happen: there is no
    profiling, evaluating or tuning stage in VIDEO_STAGES.

    `extract_semantics` is deliberately **not** in that list any more. It was,
    and the reason mattered — there was nowhere to run it — but once the stage
    exists, forcing it off would be the worse bug: `Approval.options` defaults
    to a `StageOptions()` with it `True`, so a client approving without echoing
    the options back would run a stage the gate never quoted.
    """
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        rec = (await _wait_for_gate(handle)).recommended
        assert not rec.learn_profile
        assert not rec.generate_evalset
        assert not rec.tune
        assert rec.embed is True
        assert rec.extract_semantics is True, (
            "the stage exists now, so the gate must quote it rather than "
            "silently run it after the fact"
        )
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        await handle.result()


async def test_auto_approve_uses_the_recommendation_since_nobody_is_there_to_tick(env):
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(kind="manual")):
        handle = await _start(env, request(auto_approve=True), StageOptions())
        result = await handle.result()

    assert result.state == "indexed"
    # Manual captions: correction is not suggested, so it did not run.
    assert "correction" not in SPENT


async def test_the_estimate_quotes_only_the_stages_that_can_actually_run(env):
    """Measured on a real 19-second video: quoting the raw options added
    $0.0203 of profile learning and $0.0059 of semantics to a bill whose real
    total was $0.000012. Over-reporting wildly misleads a user into declining
    affordable work exactly as much as under-reporting misleads them into
    approving an expensive one."""
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow],
                      activities=activities(kind="manual")):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=False))
        await handle.result()

    assert len(QUOTED) == 1
    quoted = QUOTED[0]
    assert not quoted.learn_profile
    assert not quoted.generate_evalset
    assert not quoted.tune
    # And it quotes everything that *does* run, semantics included since the
    # stage was added — the number at the gate has to be the number spent.
    assert quoted.embed is True
    assert quoted.extract_semantics is True


async def test_what_chunking_noticed_reaches_the_trail_and_not_only_the_log(env):
    """The one thing this pipeline must never do quietly.

    `chunk_transcript` reports two conditions it cannot fix — a correction that
    moved the paragraph count, so the *uncorrected* stream was indexed, and a
    paragraph that reached no chunk. `Chunked` had no field for them, so they
    reached the worker's stderr and nothing else; the container that ran the
    first real video import was replaced three minutes later and took the only
    copy with it. A run that silently indexed an uncorrected transcript after
    paying for the correction has to be legible from the run itself.
    """
    CHUNK_WARNINGS.extend([
        "la corrección desalineó los párrafos; se indexó el transcript sin "
        "corregir para no mover las marcas de tiempo",
        "2 párrafo(s) del transcript no llegaron a ningún fragmento",
    ])
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=True))
        result = await handle.result()

    assert result.state == "indexed"
    noted = [c for c in CALLED if c.startswith("stage:chunking|")]
    assert len(noted) == 1, CALLED
    assert "desalineó" in noted[0] and "no llegaron" in noted[0]


async def test_a_run_with_nothing_to_report_schedules_no_extra_event(env):
    """Which is also why the fix is replay-safe: a history from before
    `Chunked` carried warnings decodes to none, so the command is never
    issued and the sequence is unchanged."""
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(VideoIngestWorkflow.approve, Approval(approved=True))
        await handle.result()

    assert [c for c in CALLED if c.startswith("stage:chunking|")] == []
    assert "stage:chunking" in CALLED


async def test_a_video_gets_concepts_when_the_gate_says_so(env):
    """The empty Graph screen, fixed.

    `library_mentions` builds its node list from `MENTIONS` edges, and those
    come from semantic extraction — so a video indexed without this stage is
    citable, retrievable and **invisible on the canvas**, which a reader cannot
    tell apart from one that never indexed. Measured on the first real video:
    0 concepts, 0 claims, 0 edges.
    """
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(
            VideoIngestWorkflow.approve,
            Approval(approved=True, options=StageOptions(extract_semantics=True)),
        )
        result = await handle.result()

    assert result.state == "indexed"
    assert "extract_semantics" in CALLED
    assert "stage:semantics" in CALLED


async def test_a_video_nobody_asked_concepts_for_does_not_pay_for_them(env):
    """Unticking it at the gate has to actually stop the spend."""
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(
            VideoIngestWorkflow.approve,
            Approval(approved=True, options=StageOptions(extract_semantics=False)),
        )
        result = await handle.result()

    assert result.state == "indexed"
    assert "extract_semantics" not in CALLED
    assert "stage:semantics" not in CALLED
    assert "semantics" not in SPENT


async def test_a_transcript_can_be_packaged_as_a_book_and_pays_nothing_for_it(env):
    """The one path where the metadata call is free by construction.

    `register_video` fills the author from the channel, so `needs_metadata` is
    already false by the time the stage runs — which is why the double above
    returns `None` rather than a stub charge. The book is still built, from the
    same chunk rows the graph was projected from.
    """
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(
            VideoIngestWorkflow.approve,
            Approval(approved=True, options=StageOptions(build_epub=True)),
        )
        result = await handle.result()

    assert result.state == "indexed"
    assert len(BOOKS) == 1
    assert "epub-metadata" not in SPENT
    assert "stage:epub" in CALLED


async def test_a_video_nobody_asked_a_book_for_does_not_get_one(env):
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        await _wait_for_gate(handle)
        await handle.signal(
            VideoIngestWorkflow.approve,
            Approval(approved=True, options=StageOptions(build_epub=False)),
        )
        await handle.result()

    assert BOOKS == []
    assert "stage:epub" not in CALLED
