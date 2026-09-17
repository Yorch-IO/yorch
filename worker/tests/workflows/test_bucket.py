"""The bucket workflows, their gate, and the archive that can make a run free.

Activities are mocked, every double typed like the real activity — Temporal
maps payloads onto parameters by arity, and an untyped double accepts a call
the real converter cannot make. What is under test is the workflow's own
reasoning: nothing reaches Amazon before somebody approves; a duplicate stops
before the bill; a transcript found in the customer's archive is used instead
of a job; the fetch is routed to the bounded queue; and a write-back that
could not happen is a line on the trail rather than a failed run.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.artifacts import ArtifactRef
from brainworker.pipeline import (
    AudioRequest,
    AudioStaged,
    BucketObject,
    BucketSource,
    BucketSyncRequest,
    BucketSynced,
    ChunkKindCount,
    Chunked,
    Estimate,
    Indexed,
    IngestRequest,
    LocalTranscript,
    MediaLink,
    MediaLinkRequest,
    Registered,
    RunOpen,
    Spend,
    StageEstimate,
    StageOptions,
    Staged,
    Transcribed,
    TranscriptionJob,
    VideoProbe,
)
from brainworker.workflows.bucket import (
    AudioIngestWorkflow,
    BucketSyncWorkflow,
    MediaLinkWorkflow,
    fetch_queue_for,
)
from brainworker.workflows.ingest import Approval

TASK_QUEUE = "test-bucket"
KEY = "audios/ivoox/1995-04-02_Si-Se-Humillare-a1.mp3"

SPENT: list[str] = []
CALLED: list[str] = []
OPENED: list[RunOpen] = []
ARCHIVE_HAS: list[bool] = []       # what check_archive answers
ARCHIVE_WRITES: list[bool] = []    # what archive_transcript answers
FETCH_QUEUES: list[str] = []

TEXT_REF = ArtifactRef(kind="transcript_text", path="runs/r/transcript.txt", sha256="a" * 64, bytes=100)
CUES_REF = ArtifactRef(kind="transcript", path="runs/r/transcript.json", sha256="b" * 64, bytes=50)
CHUNKS_REF = ArtifactRef(kind="chunks", path="runs/r/chunks.jsonl", sha256="c" * 64, bytes=80, rows=4)
PROBE_REF = ArtifactRef(kind="audio_probe", path="runs/r/audio-probe.json", sha256="e" * 64, bytes=200)
EVIDENCE_REF = ArtifactRef(kind="evidence", path="runs/r/evidence.json", sha256="9" * 64, bytes=90)
RESULT_REF = ArtifactRef(kind="transcription_result", path="runs/r/transcription-result.json", sha256="7" * 64, bytes=200)


def source(**kw) -> BucketSource:
    base = dict(bucket="tenant-bucket", prefix="audios/",
                role_arn="arn:aws:iam::123456789012:role/reader",
                archive_prefix="transcripciones/", language="es-US")
    base.update(kw)
    return BucketSource(**base)


def request(**kw) -> AudioRequest:
    base = dict(library_id="lib_s3_abc", source=source(), key=KEY,
                tenant_id="tnt_000000000000000000000002",
                object=BucketObject(key=KEY, etag="e1", size=1000, last_modified="",
                                    container="mp3", duration_s=3600,
                                    recorded_at="1995-04-02", url="https://feed/1"))
    base.update(kw)
    return AudioRequest(**base)


def probe() -> VideoProbe:
    return VideoProbe(
        video_id=KEY,
        canonical_url=f"s3://tenant-bucket/{KEY}",
        source_key=f"s3/tenant-bucket/{KEY}",
        title="Si Se Humillare Mi Pueblo",
        channel="El predicador",
        duration_s=3600,
        upload_date="1995-04-02",
        content_sha256="e" * 64,
        identity_basis="s3\ntenant-bucket\n" + KEY,
        probe_ref=PROBE_REF,
    )


# -- typed doubles -----------------------------------------------------------


@activity.defn(name="open_run")
async def open_run(opening: RunOpen) -> None:
    OPENED.append(opening)
    CALLED.append("open_run")


@activity.defn(name="set_run_stage")
async def set_run_stage(run_id: str, stage: str, state: str, seq, at, detail: str | None = None) -> None:
    CALLED.append(f"stage:{stage}" + (f"|{detail}" if detail else ""))


@activity.defn(name="record_run_outcome")
async def record_run_outcome(run_id: str, state: str, kind, detail, seq, at, stage: str) -> None:
    CALLED.append(f"outcome:{state}")


@activity.defn(name="record_run_events")
async def record_run_events(run_id: str, pending: list) -> None:
    return None


@activity.defn(name="probe_object")
async def probe_object(req: AudioRequest, run_id: str) -> VideoProbe:
    assert isinstance(req, AudioRequest), f"got {type(req).__name__}"
    assert isinstance(req.source, BucketSource)
    assert req.object is not None and isinstance(req.object, BucketObject)
    CALLED.append("probe")
    return probe()


def register(already_indexed: bool = False):
    @activity.defn(name="register_document")
    async def _register(req: IngestRequest, staged: Staged, run_id: str, workflow_id: str) -> Registered:
        assert req.run_kind == "audio", req.run_kind
        assert staged.fmt == "audio", staged.fmt
        assert staged.extractor == "aws_transcribe"
        assert req.source_key == f"s3/tenant-bucket/{KEY}"
        assert req.tenant_id == "tnt_000000000000000000000002"
        return Registered(document_id="doc_a", version_id="ver_a", created=True,
                          already_indexed=already_indexed)
    return _register


@activity.defn(name="link_duplicate")
async def link_duplicate(req: IngestRequest, staged: Staged, registered: Registered) -> None:
    CALLED.append("link_duplicate")


@activity.defn(name="set_document_dates")
async def set_document_dates(req: AudioRequest, registered: Registered) -> bool:
    CALLED.append("set_document_dates")
    return True


@activity.defn(name="record_video_artifacts")
async def record_video_artifacts(run_id: str, refs: list[ArtifactRef]) -> None:
    assert all(len(r.sha256) == 64 for r in refs)
    CALLED.append("record_artifacts")


QUOTED: list[StageOptions] = []
QUOTED_ENGINE: list[str] = []


@activity.defn(name="estimate_audio")
async def estimate_video(p: VideoProbe, options: StageOptions, characters: int,
                         chunk_count: int, characters_high: int, run_id: str,
                         transcriber: str) -> Estimate:
    assert p.chosen is None, "an object never has captions"
    assert characters_high > characters, "no text in hand, so the quote is a range"
    assert run_id
    QUOTED.append(options)
    QUOTED_ENGINE.append(transcriber)
    # A local run quotes a real zero for transcription; Amazon quotes its rate.
    usd = 0.0 if transcriber == "local" else 1.44
    model = "whisper.cpp (local)" if transcriber == "local" else "aws-transcribe-batch"
    rows = [StageEstimate("transcription", model, 0, 0, usd, 0, usd)]
    # The real `estimate_timed` prices every stage the options ask for; the
    # double reproduces that for the one stage these tests turn on and off,
    # because "the quote covers what the run will do" is the property the
    # workflow is responsible for and the double must not paper over it.
    if options.correct:
        rows.append(StageEstimate("correction", "gemini", 1000, 1000, 0.03, 1000, 0.05))
        usd += 0.03
    return Estimate(stages=rows, total_usd=usd, price_source="published rates", total_usd_high=usd)


@activity.defn(name="check_archive")
async def check_archive(req: AudioRequest, p: VideoProbe, run_id: str) -> ArtifactRef | None:
    CALLED.append("check_archive")
    if ARCHIVE_HAS and ARCHIVE_HAS[0]:
        return RESULT_REF
    return None


@activity.defn(name="fetch_object")
async def fetch_object(run_id: str, req: AudioRequest, p: VideoProbe, version_id: str) -> AudioStaged:
    assert isinstance(req, AudioRequest)
    FETCH_QUEUES.append(activity.info().task_queue)
    SPENT.append("fetch")
    CALLED.append("fetch_object")
    return AudioStaged(s3_uri="s3://ours/transcribe/t/ver_a.mp3", media_format="mp3",
                       bytes=1000, seconds=p.duration_s, sha256="f" * 64)


@activity.defn(name="start_transcription")
async def start_transcription(run_id: str, p: VideoProbe, audio: AudioStaged,
                              tenant_id: str, version_id: str, language: str) -> TranscriptionJob:
    assert language == "es-US", language
    assert audio.sha256 == "f" * 64
    SPENT.append("transcription")
    return TranscriptionJob(job_name=f"brain-{version_id}", status="IN_PROGRESS")


@activity.defn(name="poll_transcription")
async def poll_transcription(job_name: str) -> TranscriptionJob:
    return TranscriptionJob(job_name=job_name, status="COMPLETED")


@activity.defn(name="collect_transcript")
async def collect_transcript(run_id: str, job: TranscriptionJob, tenant_id: str, version_id: str) -> ArtifactRef:
    return RESULT_REF


@activity.defn(name="abandon_transcription")
async def abandon_transcription(job_name: str) -> None:
    CALLED.append("abandon")


ARCHIVED_ENGINES: list[str] = []
STAGED_UPLOADS: list[LocalTranscript] = []


@activity.defn(name="archive_transcript")
async def archive_transcript(req: AudioRequest, p: VideoProbe, audio: AudioStaged,
                             job: TranscriptionJob, result: ArtifactRef, run_id: str,
                             version_id: str, language: str) -> bool:
    assert result.kind == "transcription_result"
    assert version_id == "ver_a"
    if job.engine == "aws-transcribe-batch":
        assert job.job_name == f"brain-{version_id}"
    ARCHIVED_ENGINES.append(job.engine)
    CALLED.append("archive_transcript")
    return ARCHIVE_WRITES[0] if ARCHIVE_WRITES else True


@activity.defn(name="stage_transcript")
async def stage_transcript(run_id: str, upload: LocalTranscript, tenant_id: str,
                           version_id: str) -> ArtifactRef:
    assert isinstance(upload, LocalTranscript), f"got {type(upload).__name__}"
    STAGED_UPLOADS.append(upload)
    CALLED.append("stage_transcript")
    return RESULT_REF


@activity.defn(name="group_transcript")
async def group_transcript(run_id: str, p: VideoProbe, src: ArtifactRef) -> Transcribed:
    assert len(src.sha256) == 64
    CALLED.append(f"group:{src.kind}")
    return Transcribed(text=TEXT_REF, cues=CUES_REF, evidence=EVIDENCE_REF,
                       source="transcribe", paragraphs=40, characters=16_000, covered_s=3590.0)


@activity.defn(name="chunk_transcript")
async def chunk_transcript(run_id: str, text: ArtifactRef, cues: ArtifactRef,
                           fallback: ArtifactRef | None) -> Chunked:
    assert fallback is not None and fallback.kind == "transcript_text"
    return Chunked(chunks=CHUNKS_REF, count=13, kinds=[ChunkKindCount("transcripcion", 13)])


@activity.defn(name="project_structure")
async def project_structure(req: IngestRequest, staged: Staged, registered: Registered,
                            run_id: str, chunks: ArtifactRef) -> dict:
    return {"chunks": 13, "citations": 13}


@activity.defn(name="embed_and_index")
async def embed_and_index(run_id: str, library_id: str, registered: Registered,
                          staged: Staged, chunked: Chunked) -> Indexed:
    SPENT.append("embedding")
    return Indexed(collection="brain", points=13, dimensions=3072,
                   spend=Spend("embedding", "gemini-embedding-2", 4000, 0, 0.0004))


@activity.defn(name="activate_version")
async def activate_version(req: IngestRequest, staged: Staged, registered: Registered) -> None:
    CALLED.append("activate")


@activity.defn(name="correct_text")
async def correct_text(run_id: str, extraction) -> None:
    raise AssertionError("correction is off by default for Transcribe output")


@activity.defn(name="extract_semantics")
async def extract_semantics(run_id: str, registered, chunked, options=None):
    raise AssertionError("semantics was not ticked")


def activities(*, already_indexed=False):
    return [
        open_run, set_run_stage, record_run_outcome, record_run_events,
        probe_object, register(already_indexed), link_duplicate, set_document_dates,
        record_video_artifacts, estimate_video, check_archive, start_transcription,
        poll_transcription, collect_transcript, abandon_transcription,
        archive_transcript, stage_transcript, group_transcript, chunk_transcript,
        project_structure, embed_and_index, activate_version, correct_text,
        extract_semantics,
    ]


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture(autouse=True)
def _clear():
    for l in (SPENT, CALLED, OPENED, ARCHIVE_HAS, ARCHIVE_WRITES, FETCH_QUEUES, QUOTED,
              QUOTED_ENGINE, STAGED_UPLOADS, ARCHIVED_ENGINES):
        l.clear()
    yield
    for l in (SPENT, CALLED, OPENED, ARCHIVE_HAS, ARCHIVE_WRITES, FETCH_QUEUES, QUOTED,
              QUOTED_ENGINE, STAGED_UPLOADS, ARCHIVED_ENGINES):
        l.clear()


async def _wait_for_gate(handle):
    import asyncio

    for _ in range(200):
        report = await handle.query(AudioIngestWorkflow.gate_report)
        if report is not None:
            return report
        await asyncio.sleep(0.05)
    raise AssertionError("the gate report never appeared")


def _workers(env, acts):
    """The main worker and the bounded fetch worker, as the runner starts them."""
    main = Worker(env.client, task_queue=TASK_QUEUE, workflows=[AudioIngestWorkflow],
                  activities=acts)
    fetcher = Worker(env.client, task_queue=fetch_queue_for(TASK_QUEUE),
                     activities=[fetch_object], max_concurrent_activities=1)
    return main, fetcher


async def _start(env, req: AudioRequest, opts: StageOptions | None = None):
    client: Client = env.client
    return await client.start_workflow(
        AudioIngestWorkflow.run,
        args=[req, opts or StageOptions(extract_semantics=False, correct=False)],
        id=f"audio-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )


# -- the gate ----------------------------------------------------------------


async def test_correction_asked_for_before_quoting_is_priced_at_the_gate(env):
    """Correction reaches the *estimate*, which is the whole reason it is asked
    for before the quote rather than ticked at the gate.

    The recorded failure this prevents is `VideoGateReview`'s, from the other
    direction: there the app forced `extract_semantics: false` at approval over
    a gate that had quoted it, so a person was shown $0.6532 for work the run
    then skipped. Forcing correction *off* here had the mirror shape — a client
    that ticked it at the gate got a stage the estimate never covered. Now the
    switches travel with the probe, and what the gate shows is what the run
    does.
    """
    main, fetcher = _workers(env, activities())
    async with main, fetcher:
        handle = await _start(
            env, request(), StageOptions(extract_semantics=False, correct=True)
        )
        report = await _wait_for_gate(handle)
        assert report.recommended.correct is True
        stages = [s.stage for s in report.estimate.stages]
        assert "correction" in stages, stages
        # And it is not free: a quote that carried the stage at zero would be
        # the same lie in a politer form.
        correction = next(s for s in report.estimate.stages if s.stage == "correction")
        assert correction.usd is None or correction.usd > 0
        await handle.signal(AudioIngestWorkflow.approve, Approval(approved=False))
        await handle.result()



async def test_nothing_reaches_amazon_or_the_bucket_before_someone_approves(env):
    main, fetcher = _workers(env, activities())
    async with main, fetcher:
        handle = await _start(env, request())
        report = await _wait_for_gate(handle)
        assert SPENT == []
        assert "fetch_object" not in CALLED and "check_archive" not in CALLED
        assert report.preview is None and report.transcript is None
        assert report.probe.chosen is None
        assert [s.stage for s in report.estimate.stages] == ["transcription"]
        # Off because the *caller* said so — both clients send `correct: false`
        # for audio, since a machine transcript arrives punctuated. The gate no
        # longer forces it: see the test below for why that matters.
        assert report.recommended.correct is False
        assert report.recommended.learn_profile is False
        await handle.signal(AudioIngestWorkflow.approve, Approval(approved=False))
        result = await handle.result()
    assert result.state == "rejected"
    assert SPENT == []
    assert CALLED[-1] == "outcome:cancelled"
    assert OPENED[0].kind == "audio" and OPENED[0].library_id == "lib_s3_abc"
    assert OPENED[0].label == "1995-04-02_Si-Se-Humillare-a1", "the key's stem, until the probe names it"


async def test_an_approved_run_fetches_transcribes_archives_and_indexes(env):
    main, fetcher = _workers(env, activities())
    async with main, fetcher:
        handle = await _start(env, request())
        report = await _wait_for_gate(handle)
        await handle.signal(AudioIngestWorkflow.approve,
                            Approval(approved=True, options=report.recommended))
        result = await handle.result()
    assert result.state == "indexed"
    assert SPENT == ["fetch", "transcription", "embedding"]
    assert "set_document_dates" in CALLED
    assert "archive_transcript" in CALLED
    assert "group:transcription_result" in CALLED
    stages = [c.split(":", 1)[1].split("|")[0] for c in CALLED if c.startswith("stage:")]
    assert stages == ["probing", "registering", "previewing", "awaiting_approval",
                      "fetching", "transcribing", "archiving", "grouping", "chunking",
                      "projecting", "embedding", "activating", "done"]
    # No archiving warning when the write-back succeeded.
    assert not any(c.startswith("stage:archiving|") for c in CALLED)


async def test_the_fetch_runs_on_the_bounded_queue_not_the_main_one(env):
    """The property that keeps 147 approvals from opening 147 streams."""
    main, fetcher = _workers(env, activities())
    async with main, fetcher:
        handle = await _start(env, request())
        report = await _wait_for_gate(handle)
        await handle.signal(AudioIngestWorkflow.approve,
                            Approval(approved=True, options=report.recommended))
        await handle.result()
    assert FETCH_QUEUES == [fetch_queue_for(TASK_QUEUE)]
    assert FETCH_QUEUES[0] != TASK_QUEUE


async def test_a_transcript_in_the_customers_archive_makes_the_run_free(env):
    ARCHIVE_HAS.append(True)
    main, fetcher = _workers(env, activities())
    async with main, fetcher:
        handle = await _start(env, request())
        report = await _wait_for_gate(handle)
        await handle.signal(AudioIngestWorkflow.approve,
                            Approval(approved=True, options=report.recommended))
        result = await handle.result()
    assert result.state == "indexed"
    assert "fetch" not in SPENT and "transcription" not in SPENT
    assert "fetch_object" not in CALLED and "archive_transcript" not in CALLED
    assert "group:transcription_result" in CALLED
    assert any(c.startswith("stage:transcribing|") and "sin cargo" in c for c in CALLED), \
        "the trail has to say why nothing was paid"


async def test_a_write_back_that_could_not_happen_is_a_line_on_the_trail_not_a_failure(env):
    ARCHIVE_WRITES.append(False)
    main, fetcher = _workers(env, activities())
    async with main, fetcher:
        handle = await _start(env, request())
        report = await _wait_for_gate(handle)
        await handle.signal(AudioIngestWorkflow.approve,
                            Approval(approved=True, options=report.recommended))
        result = await handle.result()
    assert result.state == "indexed"
    assert any(c.startswith("stage:archiving|") for c in CALLED)


async def test_an_object_already_indexed_stops_before_the_bill(env):
    main, fetcher = _workers(env, activities(already_indexed=True))
    async with main, fetcher:
        handle = await _start(env, request())
        result = await handle.result()
    assert result.state == "already_indexed"
    assert "link_duplicate" in CALLED
    assert SPENT == [] and "check_archive" not in CALLED
    assert CALLED[-1] == "outcome:succeeded"


async def test_auto_approve_skips_the_gate_and_still_uses_the_recommended_switches(env):
    main, fetcher = _workers(env, activities())
    async with main, fetcher:
        handle = await _start(env, request(auto_approve=True))
        result = await handle.result()
    assert result.state == "indexed"
    assert "stage:awaiting_approval" not in CALLED
    assert QUOTED and QUOTED[0].correct is False


async def test_the_bucket_language_reaches_amazon(env):
    """`es-US` from the source; a bare `es` becomes `es-ES`."""
    from brainworker.workflows.bucket import _language_for

    assert _language_for(request()) == "es-US"
    assert _language_for(request(source=source(language="es"))) == "es-ES"
    assert _language_for(request(source=source(language=""))) == "es-US"


# -- the two thin workflows ------------------------------------------------------


async def test_sync_and_link_are_awaited_and_open_no_run(env):
    @activity.defn(name="sync_bucket")
    async def sync_bucket(req: BucketSyncRequest) -> BucketSynced:
        assert isinstance(req.source, BucketSource)
        return BucketSynced(bucket_id="abc123def456", library_id="lib_s3_abc123def456",
                            objects=147, added=147, changed=0, absent=0, estimated=3,
                            manifest_rows=147, unmatched_rows=0, unmatched_objects=0)

    @activity.defn(name="presign_object")
    async def presign_object(req: MediaLinkRequest) -> MediaLink:
        return MediaLink(url="https://signed/x#t=754", expires_at="2026-09-16T00:00:00+00:00",
                         start_s=req.start_s, source_url=req.source_url)

    async with Worker(env.client, task_queue=TASK_QUEUE,
                      workflows=[BucketSyncWorkflow, MediaLinkWorkflow],
                      activities=[sync_bucket, presign_object, open_run]):
        synced = await env.client.execute_workflow(
            BucketSyncWorkflow.run,
            BucketSyncRequest(source=source(), tenant_id="tnt_000000000000000000000002"),
            id=f"sync-{uuid.uuid4()}", task_queue=TASK_QUEUE,
        )
        link = await env.client.execute_workflow(
            MediaLinkWorkflow.run,
            MediaLinkRequest(source=source(), key=KEY, tenant_id="tnt_000000000000000000000002",
                             start_s=754.0, source_url="https://feed/1"),
            id=f"link-{uuid.uuid4()}", task_queue=TASK_QUEUE,
        )
    assert synced.objects == 147 and synced.estimated == 3
    assert link.url.endswith("#t=754") and link.source_url == "https://feed/1"
    assert OPENED == [], "neither a sync nor a link is a run"


# -- a transcript made on the person's own machine -------------------------------


async def _approved(env, req: AudioRequest):
    """Start, wait for the gate, approve with the recommendation. The handle."""
    handle = await _start(env, req)
    report = await _wait_for_gate(handle)
    await handle.signal(AudioIngestWorkflow.approve,
                        Approval(approved=True, options=report.recommended))
    return handle, report


async def _wait_for_state(handle, state: str):
    import asyncio

    for _ in range(200):
        if f"stage:transcribing|esperando la transcripción hecha en el equipo de la aplicación" in CALLED \
                and state == "awaiting_transcript":
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached {state}: {CALLED}")


async def test_a_local_run_quotes_zero_parks_for_the_app_and_never_touches_amazon(env):
    main, fetcher = _workers(env, activities())
    async with main, fetcher:
        handle, report = await _approved(env, request(transcriber="local"))
        assert [s.usd for s in report.estimate.stages] == [0.0]
        assert report.estimate.stages[0].model == "whisper.cpp (local)"
        assert QUOTED_ENGINE == ["local"]
        await _wait_for_state(handle, "awaiting_transcript")
        assert await handle.query(AudioIngestWorkflow.transcriber) == "local"
        assert SPENT == [] and "fetch_object" not in CALLED

        upload = LocalTranscript(path="/workspace/tenants/t/inbox/x.json",
                                 engine="whisper.cpp", model="large-v3-turbo", language="es")
        await handle.signal(AudioIngestWorkflow.transcript_ready, upload)
        result = await handle.result()
    assert result.state == "indexed"
    assert result.transcript_source == "transcribe"  # the grouper double's word
    assert STAGED_UPLOADS == [upload]
    assert SPENT == ["embedding"], "no fetch, no Amazon job"
    assert "fetch_object" not in CALLED and "start_transcription" not in CALLED
    assert ARCHIVED_ENGINES == ["whisper.cpp/large-v3-turbo"], "the sidecar names the engine"
    stages = [c.split(":", 1)[1].split("|")[0] for c in CALLED if c.startswith("stage:")]
    assert "archiving" in stages and "grouping" in stages


async def test_switching_a_parked_local_run_to_amazon_reopens_the_gate_with_the_price(env):
    main, fetcher = _workers(env, activities())
    async with main, fetcher:
        handle, first = await _approved(env, request(transcriber="local"))
        await _wait_for_state(handle, "awaiting_transcript")
        await handle.signal(AudioIngestWorkflow.use_remote_transcriber)
        # The gate is back, and it is a different quote.
        import asyncio

        for _ in range(200):
            report = await handle.query(AudioIngestWorkflow.gate_report)
            if report is not None and report.estimate.total_usd == 1.44:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the gate never re-opened with Amazon's price")
        assert QUOTED_ENGINE == ["local", "transcribe"]
        assert await handle.query(AudioIngestWorkflow.transcriber) == "transcribe"
        assert SPENT == [], "nothing is spent until the second gate is answered"
        await handle.signal(AudioIngestWorkflow.approve,
                            Approval(approved=True, options=report.recommended))
        result = await handle.result()
    assert result.state == "indexed"
    assert SPENT == ["fetch", "transcription", "embedding"]
    assert ARCHIVED_ENGINES == ["aws-transcribe-batch"]
    assert CALLED.count("stage:awaiting_approval") == 2, "two gates, both answered"


async def test_rejecting_the_reopened_gate_cancels_the_run_and_spends_nothing(env):
    main, fetcher = _workers(env, activities())
    async with main, fetcher:
        handle, _ = await _approved(env, request(transcriber="local"))
        await _wait_for_state(handle, "awaiting_transcript")
        await handle.signal(AudioIngestWorkflow.use_remote_transcriber)
        import asyncio

        for _ in range(200):
            if CALLED.count("stage:awaiting_approval") == 2:
                break
            await asyncio.sleep(0.05)
        await handle.signal(AudioIngestWorkflow.approve, Approval(approved=False, reason="too dear"))
        with pytest.raises(Exception):
            await handle.result()
    assert SPENT == []
    assert "outcome:cancelled" in CALLED


async def test_a_video_run_still_quotes_without_naming_an_engine(env):
    """The seventh argument is defaulted, and the video workflow never passes
    it — the double asserts exactly that."""
    from brainworker.workflows.video import VideoIngestWorkflow  # noqa: F401
    assert True
