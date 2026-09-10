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
from unittest import mock

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

#: `tnt_` plus 24 hex, which is the only shape `Paths.for_tenant` accepts — a
#: path segment built from an unchecked string is how a workspace gets escaped.
TNT = "tnt_aaaaaaaaaaaaaaaaaaaaaaaa"
TNT_OTHER = "tnt_bbbbbbbbbbbbbbbbbbbbbbbb"


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


# --- who is being refused: the video, or us ----------------------------------


def test_a_bot_check_is_not_the_video_being_unavailable():
    """The production message, classified.

    Every `DownloadError` used to collapse into `video_unavailable`, which is
    right for a private, deleted or geo-blocked video and wrong for this one:
    measured 2026-09-05, `yq6uVBsVkeQ` probes fine from a residential IP in
    2.6 s and is refused from the EC2 egress IP `34.218.169.144`. The video is
    available; the caller is blocked, and the remedy is nothing to do with the
    video.

    Plain strings, no yt-dlp — the classification is what is worth asserting.
    """
    production = (
        "ERROR: [youtube] yq6uVBsVkeQ: Sign in to confirm you're not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication."
    )
    assert vid._download_error_kind(production) == "youtube_refused_this_host"
    assert vid._download_error_kind(
        "ERROR: [youtube] x: HTTP Error 429: Too Many Requests"
    ) == "youtube_refused_this_host"


def test_a_video_that_really_is_gone_keeps_its_own_kind():
    """The distinction only pays if the other half still lands where it did."""
    for message in (
        "ERROR: [youtube] x: Video unavailable",
        "ERROR: [youtube] x: This video is private",
        "ERROR: [youtube] x: The uploader has not made this video available "
        "in your country",
    ):
        assert vid._download_error_kind(message) == "video_unavailable", message


def test_a_caption_track_is_asked_for_more_than_once_because_giving_up_costs_money():
    """An empty return here sets `chosen = None`, and that is the branch that
    pays Amazon.

    Measured 2026-09-05: the `timedtext` endpoint refused one address on six
    attempts across 25 minutes while serving the *same* URLs to another host at
    200. One try and a shrug turns a transient throttle into a transcription
    bill.
    """
    attempts = []

    def flaky(url, timeout):  # noqa: ARG001 - matching urlopen's shape
        attempts.append(url)
        if len(attempts) < 3:
            raise OSError("HTTP Error 429: Too Many Requests")
        return _Body(b"WEBVTT\n")

    import urllib.request

    with mock.patch.object(urllib.request, "urlopen", flaky), \
            mock.patch.object(vid, "CAPTION_BACKOFF", (0.0, 0.0)):
        assert vid._download_caption("https://example.invalid/t", "es") == b"WEBVTT\n"
    assert len(attempts) == 3


def test_a_caption_track_that_never_answers_still_falls_back_rather_than_failing():
    """Falling back is right — it is a run that has not spent anything, and the
    gate still shows the bill before anybody approves it. Only the number of
    tries changed."""
    import urllib.request

    def refused(url, timeout):  # noqa: ARG001
        raise OSError("HTTP Error 429: Too Many Requests")

    with mock.patch.object(urllib.request, "urlopen", refused), \
            mock.patch.object(vid, "CAPTION_BACKOFF", (0.0, 0.0)):
        assert vid._download_caption("https://example.invalid/t", "es") == b""


class _Body:
    """The two methods `urlopen`'s context manager needs."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._data


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
        RUN, probe(False), AUDIO, TNT, "ver_1", "es-ES"
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
        RUN, probe(False), AUDIO, TNT, "ver_1", "es-ES"
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
            RUN, probe(False), AUDIO, TNT, "ver_1", "es-ES"
        )


# --- audio the client downloaded ---------------------------------------------


class FakeS3:
    """S3 as `stage_audio` uses it: a listing and an upload, nothing else."""

    def __init__(self, already: str | None = None):
        self.already = already
        self.uploaded: list[tuple[str, str]] = []

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        if self.already is None:
            return {}
        return {"Contents": [{"Key": self.already, "Size": 4096}]}

    def upload_file(self, Filename, Bucket, Key, **kw):
        self.uploaded.append((Filename, Key))
        self.callback = kw.get("Callback")


def _staged(workspace: pathlib.Path, tenant: str, name: str = "audio.m4a") -> pathlib.Path:
    """A file where `POST /videos/audio` puts one, for the tenant that asked."""
    from brainworker.activities.ingest import _settings

    inbox = _settings().paths.for_tenant(tenant).inbox
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_bytes(b"\x00" * 4096)
    return path


async def test_audio_the_client_uploaded_is_put_where_transcribe_can_read_it(
    aws, monkeypatch
):
    """The half of the split that could not stay on the client.

    A `googlevideo` media URL carries the address that resolved it and answers
    403 anywhere else — measured — so when the app made the `extract_info` call
    the app is also the only thing that can download the audio. What it cannot
    do is write to S3: that needs the instance role, which a laptop does not
    have and must not be given. So it uploads through the plane and this
    activity, on the host that holds the role, takes the one remaining step.
    """
    s3 = FakeS3()
    monkeypatch.setattr(vid, "_client", lambda s, service: s3)
    path = _staged(pathlib.Path(vid._settings().workspace), TNT)

    staged = await vid.stage_audio(RUN, str(path), probe(False), TNT, "ver_1")

    assert staged.s3_uri == f"s3://bucket/transcribe/{TNT}/ver_1.m4a"
    assert staged.media_format == "mp4"
    assert staged.bytes == 4096
    assert staged.reused is False
    assert len(s3.uploaded) == 1
    assert callable(s3.callback), (
        "boto3's per-part callback is what the heartbeat reports. Without it "
        "this activity beats a progress figure of 0 for however long a "
        "gigabyte takes, and a zero that means 'not measured' is the one shape "
        "this codebase keeps refusing to print"
    )
    assert not path.exists(), (
        "the staged file is deleted whether or not the upload worked, exactly "
        "as `fetch_audio` deletes its own download: it is up to a gigabyte on "
        "the same volume as the corpus"
    )
    assert aws == [], "moving bytes into S3 is not a provider call and books nothing"


async def test_a_retry_finds_the_audio_already_in_s3_and_does_not_read_the_file(
    aws, monkeypatch
):
    """Idempotency matters more here than in `fetch_audio`, and for a reason
    that has no counterpart there: a retry cannot re-download, because the file
    the client staged was deleted the first time round. The object is keyed on
    the version, so the lookup is what makes the second attempt free."""
    s3 = FakeS3(already=f"transcribe/{TNT}/ver_1.m4a")
    monkeypatch.setattr(vid, "_client", lambda s, service: s3)
    # Inside the organisation's tree and not on disk, which is exactly the
    # state a retry finds: containment is checked **before** the reuse lookup,
    # deliberately, because the reuse branch unlinks the path it was given and
    # an unchecked one there would be a cross-tenant delete.
    gone = vid._settings().paths.for_tenant(TNT).inbox / "gone.m4a"

    staged = await vid.stage_audio(RUN, str(gone), probe(False), TNT, "ver_1")

    assert staged.reused is True
    assert staged.s3_uri == f"s3://bucket/transcribe/{TNT}/ver_1.m4a"
    assert s3.uploaded == []


async def test_a_path_outside_the_organisations_own_tree_is_refused(aws, monkeypatch):
    """The guard `stage_source` already makes for a document import, made here
    for the same reason: the string arrives over HTTP, and a string becomes a
    path. The route derives the filename itself, so a well-behaved client never
    names anything else — and a badly behaved one is refused rather than
    reading another organisation's inbox."""
    s3 = FakeS3()
    monkeypatch.setattr(vid, "_client", lambda s, service: s3)
    elsewhere = _staged(pathlib.Path(vid._settings().workspace), TNT_OTHER)

    with pytest.raises(Exception) as caught:
        await vid.stage_audio(RUN, str(elsewhere), probe(False), TNT, "ver_1")

    assert caught.value.type == "audio_outside_workspace"
    assert s3.uploaded == []
    assert elsewhere.exists(), "a refused path is somebody else's file, not ours to delete"


async def test_a_container_transcribe_cannot_read_is_refused_rather_than_recoded(
    aws, monkeypatch
):
    """Transcoding would mean ffmpeg in the worker image — about 80 MB of apt on
    a 60 GiB volume that also holds the corpus. The same refusal `fetch_audio`
    makes about a format yt-dlp handed it."""
    s3 = FakeS3()
    monkeypatch.setattr(vid, "_client", lambda s, service: s3)
    path = _staged(pathlib.Path(vid._settings().workspace), TNT, "audio.aiff")

    with pytest.raises(Exception) as caught:
        await vid.stage_audio(RUN, str(path), probe(False), TNT, "ver_1")

    assert caught.value.type == "audio_format_unsupported"
    assert s3.uploaded == []


# --- a record produced somewhere else ----------------------------------------


async def test_probing_refuses_a_resolved_record_that_is_not_for_this_url(workspace):
    """The check runs in the activity that fetches `caption_url`, not only at
    the route that accepted it — the same doubling `retrieve.search` keeps for
    `tenant_id`, and for the same reason: a route is one of several ways in.

    `probe_video` is where it belongs because this is the frame that makes the
    request. A guard at the route alone protects the route.
    """
    from brainworker.pipeline import VideoInfo, VideoRequest

    request = VideoRequest(library_id="lib_v", url=f"https://youtu.be/{VID}")
    other = VideoInfo(
        video_id="jNQXAC9IVRw",
        title="otro vídeo",
        channel="",
        duration_s=19,
        upload_date="20050424",
    )

    with pytest.raises(Exception) as caught:
        await vid.probe_video(request, RUN, other)

    assert caught.value.type == "resolution_not_trusted"
    assert "jNQXAC9IVRw" in str(caught.value)


# --- which caption track of 157 -------------------------------------------


def _auto(*languages: str) -> list[CaptionTrack]:
    return [CaptionTrack(language=l, kind="auto", ext="vtt") for l in languages]


def test_the_original_language_beats_a_translation_of_it():
    """The defect this rule exists for, with the real shape that produced it.

    Measured 2026-09-10 on `yq6uVBsVkeQ`, a 76-minute talk in Spanish:
    **157 automatic tracks, returned in alphabetical order by code** — `ab`,
    `aa`, `af`, `ak`, `sq`, … — and the one YouTube actually heard is
    `es-orig`. A caller expressing no preference got Abkhazian: a machine
    translation of a machine transcription, offered at the gate as a
    two-letter code in a line of small print.
    """
    tracks = _auto("ab", "aa", "af", "es", "es-orig", "sq")
    assert vid._choose_track(tracks, []).language == "es-orig"


def test_it_holds_inside_a_language_preference_too():
    # `es` matches both `es` and `es-orig` by prefix, and where a video offers
    # both, one of them has been round tripped through a translator.
    tracks = _auto("es", "es-orig")
    assert vid._choose_track(tracks, ["es"]).language == "es-orig"


def test_a_preference_still_wins_over_the_original():
    """The rule decides ties; it does not overrule the caller. A shelf of
    English videos asking for English must not be handed the Spanish source."""
    tracks = _auto("es-orig", "en")
    assert vid._choose_track(tracks, ["en"]).language == "en"


def test_a_human_transcript_still_beats_the_automatic_original():
    # Manual over automatic is a quality ordering and this does not disturb it:
    # an auto track has no punctuation and carries rolling duplicates.
    tracks = [
        CaptionTrack(language="es-orig", kind="auto", ext="vtt"),
        CaptionTrack(language="pt", kind="manual", ext="vtt"),
    ]
    chosen = vid._choose_track(tracks, [])
    assert (chosen.language, chosen.kind) == ("pt", "manual")


def test_a_video_with_no_original_marker_is_unchanged():
    # The sort is stable, so it reorders nothing when no track is marked.
    tracks = _auto("ab", "aa", "af")
    assert vid._choose_track(tracks, []).language == "ab"
