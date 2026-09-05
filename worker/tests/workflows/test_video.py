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
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.artifacts import ArtifactRef
from brainworker.pipeline import (
    AudioStaged,
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
    Spend,
    StageEstimate,
    StageOptions,
    Staged,
    Transcribed,
    TranscriptionJob,
    VideoProbe,
    VideoRequest,
)
from brainworker.workflows.ingest import Approval
from brainworker.workflows.video import VideoIngestWorkflow

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


def prober(with_captions: bool = True, kind: str = "manual"):
    @activity.defn(name="probe_video")
    async def _probe(req: VideoRequest, run_id: str) -> VideoProbe:
        assert isinstance(req, VideoRequest), f"got {type(req).__name__}"
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


@activity.defn(name="record_run_events")
async def record_run_events(run_id: str, pending: list) -> None:
    return None


@activity.defn(name="set_run_stage")
async def set_run_stage(run_id: str, stage: str, state: str, seq, at) -> None:
    CALLED.append(f"stage:{stage}")


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


@activity.defn(name="estimate_video")
async def estimate_video(
    p: VideoProbe,
    options: StageOptions,
    characters: int,
    chunk_count: int,
    characters_high: int,
) -> Estimate:
    assert isinstance(p, VideoProbe), f"got {type(p).__name__}"
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
                   kinds=[ChunkKindCount("transcripcion", 13)])


@activity.defn(name="project_structure")
async def project_structure(
    req: IngestRequest, staged: Staged, registered: Registered,
    run_id: str, chunks: ArtifactRef,
) -> dict:
    return {"chunks": 13, "citations": 13}


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
        prober(captions, kind), register(already_indexed), record_run_events,
        set_run_stage, record_run_outcome, link_duplicate, record_video_artifacts,
        group_transcript, preview_transcript, estimate_video, fetch_audio,
        start_transcription, poller(statuses or []), collect_transcript,
        abandon_transcription, correct_text, chunk_transcript, project_structure,
        embed_and_index, activate_version,
    ]


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture(autouse=True)
def _clear():
    SPENT.clear(); CALLED.clear(); POLLS.clear(); QUOTED.clear()
    yield
    SPENT.clear(); CALLED.clear(); POLLS.clear(); QUOTED.clear()


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
    profiling, semantics, evaluating or tuning stage in VIDEO_STAGES."""
    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[VideoIngestWorkflow], activities=activities()):
        handle = await _start(env, request(), StageOptions())
        rec = (await _wait_for_gate(handle)).recommended
        assert not rec.learn_profile
        assert not rec.extract_semantics
        assert not rec.generate_evalset
        assert not rec.tune
        assert not rec.condense_descriptions
        assert rec.embed is True
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
    assert not quoted.extract_semantics
    assert not quoted.generate_evalset
    assert not quoted.tune
    # And it still quotes what *does* run.
    assert quoted.embed is True
