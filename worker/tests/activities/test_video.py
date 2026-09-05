"""The video activities, against real artifacts and fake networks.

The arithmetic is asserted next door in `tests/unit/test_videosource.py` and in
the engine's own `test_transcript.py`. What this file measures is the part those
cannot: that the artifacts a real run writes are readable by the activity that
reads them next, and that the two most expensive mistakes — charging Amazon
twice, and indexing a transcript whose timestamps have shifted — are prevented
where the money and the drift actually are.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from docagent import transcript as dt

from brainworker import videosource
from brainworker.activities import video as vid
from brainworker.artifacts import ArtifactStore
from brainworker.pipeline import (
    AudioStaged,
    CaptionTrack,
    StageOptions,
    VideoProbe,
)

RUN = "video-test-1"
VID = "dQw4w9WgXcQ"


@pytest.fixture
def workspace(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv("BRAIN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAIN_DATABASE_URL", "postgresql://brain:x@127.0.0.1:1/brain")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def probe(with_captions: bool = True, duration: int = 600) -> VideoProbe:
    track = CaptionTrack(language="es", kind="manual", ext="vtt")
    return VideoProbe(
        video_id=VID, canonical_url=f"https://youtu.be/{VID}",
        source_key=f"youtube/{VID}", title="Charla", channel="Canal",
        duration_s=duration, upload_date="20260101",
        content_sha256="e" * 64, identity_basis="basis",
        tracks=[track] if with_captions else [],
        chosen=track if with_captions else None,
    )


def vtt(n: int = 300) -> bytes:
    """A caption file long enough to produce several chunks, with unique tokens
    so a chunk's text names the cues inside it."""
    out = ["WEBVTT", ""]
    at = 0.0
    for i in range(n):
        start, end = at, at + 0.5
        at += 0.6 if i % 30 else 3.0
        word = f"tok{i}." if i % 9 == 8 else f"tok{i}"
        out += [f"{_ts(start)} --> {_ts(end)}", word, ""]
    return "\n".join(out).encode("utf-8")


def _ts(s: float) -> str:
    return f"{int(s)//3600:02d}:{int(s)//60%60:02d}:{s%60:06.3f}"


# --- grouping and chunking, the pair that carries the clock -------------------


async def test_grouping_writes_a_paragraph_stream_its_own_cue_table_indexes(workspace):
    store = ArtifactStore(workspace, RUN)
    source = store.write_bytes("captions", vtt())

    got = await vid.group_transcript(RUN, probe(), source)

    assert got.source == "captions:es:manual"
    assert got.paragraphs > 3
    table = store.read_json(got.cues)["paragraphs"]
    assert len(table) == got.paragraphs

    # The invariant everything downstream rests on, asserted against the
    # engine's own splitter rather than re-derived.
    from docagent.chunk import split_paragraphs

    assert len(split_paragraphs(store.read_bytes(got.text))) == got.paragraphs


async def test_a_chunk_is_stamped_with_when_its_own_cues_were_spoken(workspace):
    store = ArtifactStore(workspace, RUN)
    source = store.write_bytes("captions", vtt())
    got = await vid.group_transcript(RUN, probe(), source)

    chunked = await vid.chunk_transcript(RUN, got.text, got.cues, got.text)
    rows = list(store.iter_jsonl(chunked.chunks))
    assert len(rows) > 1

    spoken = {c.text.rstrip("."): c for c in dt.parse_vtt(vtt())}
    for row in rows:
        assert row["start_s"] <= row["end_s"]
        for token in row["text"].split():
            cue = spoken[token.rstrip(".")]
            assert row["start_s"] <= cue.start_s and cue.end_s <= row["end_s"], (
                f"chunk {row['index']} spans [{row['start_s']}, {row['end_s']}] "
                f"but holds {token!r} spoken at [{cue.start_s}, {cue.end_s}]"
            )


async def test_the_rows_carry_the_paragraph_range_the_timestamp_came_from(workspace):
    """So a stored timestamp can be re-checked against the cue table later,
    rather than only at the moment it was derived."""
    store = ArtifactStore(workspace, RUN)
    got = await vid.group_transcript(RUN, probe(), store.write_bytes("captions", vtt()))
    chunked = await vid.chunk_transcript(RUN, got.text, got.cues, got.text)

    table = store.read_json(got.cues)["paragraphs"]
    for row in store.iter_jsonl(chunked.chunks):
        assert row["start_s"] == table[row["para_from"]]["start_s"]
        assert row["end_s"] == table[row["para_to"]]["end_s"]


async def test_a_correction_that_moved_the_paragraph_count_falls_back_and_says_so(
    workspace,
):
    """A wrong timestamp is worse than a missing correction: it is an
    unverifiable citation that looks verifiable, and the only way a reader finds
    out is by clicking it."""
    store = ArtifactStore(workspace, RUN)
    got = await vid.group_transcript(RUN, probe(), store.write_bytes("captions", vtt()))

    # What a correction looks like when it has split one paragraph in two.
    broken = store.read_bytes(got.text).replace(b" ", b"\n\n", 1)
    corrupted = store.write_bytes("corrected_text", broken)

    chunked = await vid.chunk_transcript(RUN, corrupted, got.cues, got.text)

    # It indexed the uncorrected stream, and every timestamp is still right.
    table = store.read_json(got.cues)["paragraphs"]
    for row in store.iter_jsonl(chunked.chunks):
        assert row["start_s"] == table[row["para_from"]]["start_s"]


async def test_chunking_refuses_rather_than_guessing_when_there_is_no_fallback(
    workspace,
):
    store = ArtifactStore(workspace, RUN)
    got = await vid.group_transcript(RUN, probe(), store.write_bytes("captions", vtt()))
    broken = store.read_bytes(got.text).replace(b" ", b"\n\n", 1)
    corrupted = store.write_bytes("corrected_text", broken)

    with pytest.raises(videosource.TimeAlignmentLost):
        await vid.chunk_transcript(RUN, corrupted, got.cues, None)


async def test_an_empty_transcript_is_refused_before_anything_is_indexed(workspace):
    store = ArtifactStore(workspace, RUN)
    source = store.write_bytes("captions", b"WEBVTT\n\n")
    with pytest.raises(Exception, match="transcript"):
        await vid.group_transcript(RUN, probe(), source)


# --- what the gate quotes ----------------------------------------------------


async def test_captions_add_no_transcription_row_to_the_estimate(workspace):
    got = await vid.estimate_video(probe(), StageOptions(), 16_000, 13)
    assert "transcription" not in [s.stage for s in got.stages]


async def test_without_captions_transcription_leads_the_estimate_and_its_total(
    workspace, monkeypatch
):
    monkeypatch.setenv("BRAIN_AWS_REGION", "us-west-2")
    monkeypatch.setenv("BRAIN_TRANSCRIBE_BUCKET", "bucket")
    monkeypatch.setenv("BRAIN_TRANSCRIBE_USD_PER_MINUTE", "0.024")

    got = await vid.estimate_video(probe(False, duration=600), StageOptions(), 9600, 8)
    first = got.stages[0]
    assert first.stage == "transcription"
    # Seconds of audio, not tokens: putting the duration in a token column would
    # make it a term in every total the UI sums.
    assert (first.input_tokens, first.output_tokens) == (0, 0)
    assert first.usd == pytest.approx(0.24)
    # Exact rather than a range — the bill is duration times a published rate.
    assert first.usd == first.usd_high
    assert got.total_usd >= 0.24


async def test_an_unpriced_deployment_reports_no_price_rather_than_zero(
    workspace, monkeypatch
):
    monkeypatch.setenv("BRAIN_AWS_REGION", "us-west-2")
    monkeypatch.setenv("BRAIN_TRANSCRIBE_BUCKET", "bucket")
    got = await vid.estimate_video(probe(False), StageOptions(), 9600, 8)
    assert got.stages[0].usd is None
    assert "transcription" in got.unpriced_stages


# --- the one place a retry must not charge again -----------------------------


class _Conflict(Exception):
    """Stands in for botocore's ConflictException, which is generated at runtime."""


class FakeTranscribe:
    """A Transcribe client that already has the job, like a retry would find."""

    def __init__(self, already: bool):
        self.already = already
        self.started = 0

        class _Ex:
            ConflictException = _Conflict

        self.exceptions = _Ex()

    def start_transcription_job(self, **kw):
        if self.already:
            raise _Conflict("The requested job name already exists.")
        self.started += 1
        return {"TranscriptionJob": {
            "TranscriptionJobName": kw["TranscriptionJobName"],
            "TranscriptionJobStatus": "IN_PROGRESS",
        }}

    def get_transcription_job(self, TranscriptionJobName):
        return {"TranscriptionJob": {
            "TranscriptionJobName": TranscriptionJobName,
            "TranscriptionJobStatus": "IN_PROGRESS",
        }}


@pytest.fixture
def aws(workspace, monkeypatch):
    monkeypatch.setenv("BRAIN_AWS_REGION", "us-west-2")
    monkeypatch.setenv("BRAIN_TRANSCRIBE_BUCKET", "bucket")
    monkeypatch.setenv("BRAIN_TRANSCRIBE_USD_PER_MINUTE", "0.024")
    charges: list = []
    monkeypatch.setattr(vid, "_charge", lambda run, spend, provider="vertex":
                        charges.append((spend, provider)) or spend)
    return charges


AUDIO = AudioStaged(s3_uri="s3://bucket/k.m4a", media_format="mp4",
                    bytes=10, seconds=600)


async def test_starting_a_job_charges_once_for_the_audio(aws, monkeypatch):
    client = FakeTranscribe(already=False)
    monkeypatch.setattr(vid, "_client", lambda s, service: client)

    job = await vid.start_transcription(
        RUN, probe(False), AUDIO, "tnt_1", "ver_1", "es-ES"
    )

    assert job.job_name == "brain-ver_1"
    assert client.started == 1
    assert len(aws) == 1
    spend, provider = aws[0]
    assert provider == "aws"
    assert spend.stage == "transcription"
    assert spend.usd == pytest.approx(0.24)


async def test_a_retry_that_finds_the_job_already_started_does_not_charge_again(
    aws, monkeypatch
):
    """The one place `_charge`'s premise is false.

    Its docstring says a retried activity spent its tokens whether or not the
    attempt succeeded — true of a generation call, and false here: the job name
    is derived from the version, so Amazon has the job and billed for it once.
    Charging on the retry would double-report a bill incurred once.
    """
    client = FakeTranscribe(already=True)
    monkeypatch.setattr(vid, "_client", lambda s, service: client)

    job = await vid.start_transcription(
        RUN, probe(False), AUDIO, "tnt_1", "ver_1", "es-ES"
    )

    assert job.job_name == "brain-ver_1"
    assert job.status == "IN_PROGRESS"
    assert client.started == 0
    assert aws == [], "a retry must not charge for a job Amazon already has"


def test_the_job_name_is_derived_from_the_version_and_fits_amazons_pattern():
    import re

    name = vid.job_name_for("ver_0b71d21eeb3228f54437d9cf")
    assert name == "brain-ver_0b71d21eeb3228f54437d9cf"
    assert re.fullmatch(r"[0-9a-zA-Z._-]{1,200}", name)
    # Derived, so two attempts on one version cannot produce two jobs.
    assert vid.job_name_for("ver_x") == vid.job_name_for("ver_x")


async def test_transcribing_is_refused_when_the_deployment_has_no_bucket(
    workspace, monkeypatch
):
    """A refusal somebody reads while deciding, not a failure forty minutes into
    a run they approved."""
    monkeypatch.delenv("BRAIN_AWS_REGION", raising=False)
    monkeypatch.delenv("BRAIN_TRANSCRIBE_BUCKET", raising=False)
    with pytest.raises(Exception, match="transcribe_not_configured|AWS"):
        await vid.start_transcription(
            RUN, probe(False), AUDIO, "tnt_1", "ver_1", "es-ES"
        )
