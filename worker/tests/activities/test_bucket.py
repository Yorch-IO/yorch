"""The bucket activities, against real artifacts and a fake S3.

`s3source`'s network half is replaced with an in-memory bucket, so what is
measured here is what the pure tests cannot: that a sync writes what a probe
then reads, that a probe refuses what Transcribe would refuse after the
upload, that a fetch refuses an object whose bytes moved since the quote, and
that the archive's sidecar names exactly the identity `check_archive` looks
for — so the free path is taken for the same bytes and never for a
re-upload under the same key.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import struct

import pytest
from temporalio.exceptions import ApplicationError

from brainworker import s3source
from brainworker.activities import bucket as bkt
from brainworker.artifacts import ArtifactStore
from brainworker.bucketstore import BucketStore
from brainworker.pipeline import (
    AudioRequest,
    AudioStaged,
    LocalTranscript,
    BucketObject,
    BucketSource,
    BucketSyncRequest,
    MediaLinkRequest,
    TranscriptionJob,
    VideoProbe,
)

RUN = "audio-test-1"
TNT = "tnt_aaaaaaaaaaaaaaaaaaaaaaaa"
KEY = "audios/ivoox/1995-04-02_Si-Se-Humillare-a1.mp3"


# --- a bucket in memory --------------------------------------------------------


def mp3_bytes(seconds: int, kbps: int = 128) -> bytes:
    """A CBR stream: a header every frame, no Xing, so the duration is
    `bytes × 8 / bitrate` and exact."""
    header = bytes([0xFF, 0xFB, 0x90, 0x44])  # MPEG-1 L3 128 kbps 44.1 kHz stereo
    frame = header + b"\x00" * 413
    n = seconds * kbps * 1000 // 8 // len(frame)
    return frame * n


def m4a_bytes(seconds: int) -> bytes:
    def atom(kind, payload):
        return struct.pack(">I", 8 + len(payload)) + kind + payload
    mvhd = atom(b"mvhd", b"\x00" * 12 + struct.pack(">II", 1000, seconds * 1000) + b"\x00" * 80)
    return atom(b"ftyp", b"M4A \x00\x00\x00\x00") + atom(b"moov", mvhd) + atom(b"mdat", b"\x7f" * 5000)


class FakeS3:
    """Enough of the S3 client surface for these activities, plus a ledger of
    what was written so the archive can be asserted on."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)
        self.puts: list[str] = []
        self.gets: list[str] = []

    def _etag(self, key: str) -> str:
        import hashlib
        return hashlib.md5(self.objects[key]).hexdigest()

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for k in s3.objects if k.startswith(Prefix))
                for i in range(0, len(keys), 2):  # two per page, to exercise paging
                    yield {"Contents": [
                        {"Key": k, "ETag": f'"{s3._etag(k)}"', "Size": len(s3.objects[k]),
                         "LastModified": None}
                        for k in keys[i:i + 2]
                    ]}
        return P()

    def get_object(self, Bucket, Key, Range=None):
        if Key not in self.objects:
            raise _client_error("NoSuchKey")
        self.gets.append(Key)
        data = self.objects[Key]
        if Range:
            spec = Range.split("=")[1]
            if spec.startswith("-"):
                data = data[-int(spec[1:]):]
            else:
                a, b = spec.split("-")
                data = data[int(a):int(b) + 1]

        class Body:
            def read(self, n=None):
                return data if n is None else data[:n]

            def iter_chunks(self, chunk_size):
                for i in range(0, len(data), chunk_size):
                    yield data[i:i + chunk_size]
        return {"Body": Body()}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _client_error("NoSuchKey")
        return {"ETag": f'"{self._etag(Key)}"', "ContentLength": len(self.objects[Key]), "Metadata": {}}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body
        self.puts.append(Key)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://signed.example/{Params['Key']}?X-Amz-Expires={ExpiresIn}"

    def get_bucket_location(self, Bucket):
        return {"LocationConstraint": "us-west-2"}


def _client_error(code: str):
    class ClientError(Exception):
        response = {"Error": {"Code": code, "Message": code}}
    return ClientError(code)


@pytest.fixture
def workspace(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv("BRAIN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAIN_DATABASE_URL", "postgresql://brain:x@127.0.0.1:1/brain")
    monkeypatch.setenv("BRAIN_AWS_REGION", "us-west-2")
    monkeypatch.setenv("BRAIN_TRANSCRIBE_BUCKET", "ours")
    monkeypatch.setenv("BRAIN_TRANSCRIBE_USD_PER_MINUTE", "0.024")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


MANIFEST = (
    "archivo,titulo,fecha_predica,fecha_pub,fuente,url_mp3\n"
    "1995-04-02_Si-Se-Humillare-a1.mp3,Si Se Humillare,1995-04-02,2019-06-01,ivoox,https://feed/1\n"
    "2021-05-06_Domingo.mp3,Domingo,2021-05-06,2021-05-06,anchor,https://feed/2\n"
)


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    fake = FakeS3({
        KEY: mp3_bytes(60),
        "audios/anchor/2021-05-06_Domingo.mp3": m4a_bytes(90),  # an M4A wearing .mp3
        "audios/notes.txt": b"not audio at all, a text file with some length to it",
        "audios/ivoox/": b"",  # a directory marker
        "metadatos/manifiesto.csv": MANIFEST.encode("utf-8"),
    })
    monkeypatch.setattr(s3source, "customer_session", lambda source, external_id: object())
    monkeypatch.setattr(s3source, "s3_client", lambda session, source: fake)
    return fake


def source(**kw) -> BucketSource:
    base = dict(bucket="tenant-bucket", prefix="audios/",
                role_arn="arn:aws:iam::123456789012:role/reader",
                manifest_key="metadatos/manifiesto.csv",
                manifest_map={"file": "archivo", "title": "titulo", "recorded": "fecha_predica",
                              "published": "fecha_pub", "source": "fuente", "url": "url_mp3"},
                archive_prefix="transcripciones/")
    base.update(kw)
    return BucketSource(**base)


def sync_request(**kw) -> BucketSyncRequest:
    return BucketSyncRequest(source=source(**kw), tenant_id=TNT)


# --- sync -------------------------------------------------------------------------


def test_sync_lists_reads_headers_joins_the_manifest_and_saves(workspace, s3):
    synced = asyncio.run(bkt.sync_bucket(sync_request()))
    assert synced.objects == 3, "two recordings and a text file; the marker and the manifest are not objects"
    assert synced.added == 3 and synced.manifest_rows == 2
    store = BucketStore(workspace / "tenants" / TNT / "buckets")
    rows = {o.key: o for o in store.objects(synced.bucket_id)}
    a = rows[KEY]
    assert (a.container, a.duration_s, a.duration_estimated) == ("mp3", 60, False)
    assert (a.title, a.recorded_at, a.published_at, a.source, a.url) == \
        ("Si Se Humillare", "1995-04-02", "2019-06-01", "ivoox", "https://feed/1")
    d = rows["audios/anchor/2021-05-06_Domingo.mp3"]
    assert (d.container, d.duration_s) == ("mp4", 90)
    assert any(w.startswith("extension_lies") for w in d.warnings), "an M4A wearing .mp3 is flagged"
    t = rows["audios/notes.txt"]
    assert t.container == "" and t.duration_estimated is True
    assert "container_unknown" in t.warnings
    assert t.title == "notes" and t.source == ""
    assert synced.estimated == 1
    assert synced.unmatched_rows == 0 and synced.unmatched_objects == 1
    stored = store.read(synced.bucket_id)
    assert stored is not None and stored.complete is True
    assert stored.library_id == s3source.library_id_for("tenant-bucket", "audios/")


def test_a_second_sync_reads_no_known_object_twice(workspace, s3):
    asyncio.run(bkt.sync_bucket(sync_request()))
    before = len(s3.gets)
    synced = asyncio.run(bkt.sync_bucket(sync_request()))
    assert synced.added == 0 and synced.changed == 0
    # Only the manifest is re-read; no ranged GET on any known object.
    assert s3.gets[before:] == ["metadatos/manifiesto.csv"]


def test_a_changed_object_is_re_read_and_a_vanished_one_is_marked_absent(workspace, s3):
    first = asyncio.run(bkt.sync_bucket(sync_request()))
    s3.objects[KEY] = mp3_bytes(120)
    del s3.objects["audios/notes.txt"]
    synced = asyncio.run(bkt.sync_bucket(sync_request()))
    assert (synced.changed, synced.absent) == (1, 1)
    store = BucketStore(workspace / "tenants" / TNT / "buckets")
    rows = {o.key: o for o in store.objects(first.bucket_id)}
    assert rows[KEY].duration_s == 120
    assert rows["audios/notes.txt"].available is False, "marked, never deleted"


def test_an_unreadable_manifest_costs_the_titles_not_the_sync(workspace, s3):
    del s3.objects["metadatos/manifiesto.csv"]
    synced = asyncio.run(bkt.sync_bucket(sync_request()))
    assert synced.objects == 3
    assert any("manifest" in w for w in synced.warnings)


def test_a_bad_source_is_refused_before_anything_is_listed(workspace, s3):
    with pytest.raises(ApplicationError) as e:
        asyncio.run(bkt.sync_bucket(BucketSyncRequest(
            source=source(bucket="Not Valid"), tenant_id=TNT)))
    assert e.value.type == "bucket_source_invalid"


# --- probe ----------------------------------------------------------------------


def audio_request(key: str = KEY, obj: BucketObject | None = None, **kw) -> AudioRequest:
    base = dict(library_id="lib_s3_x", source=source(), key=key, tenant_id=TNT, object=obj)
    base.update(kw)
    return AudioRequest(**base)


def test_probe_reads_the_object_and_writes_the_identity_it_hashed(workspace, s3):
    p = asyncio.run(bkt.probe_object(audio_request(), RUN))
    assert p.chosen is None and p.tracks == []
    assert p.duration_s == 60
    assert p.source_key == f"s3/tenant-bucket/{KEY}"
    assert p.canonical_url == f"s3://tenant-bucket/{KEY}"
    assert p.title == "1995-04-02_Si-Se-Humillare-a1", "the stem, with no manifest row on the request"
    assert p.upload_date == "1995-04-02", "the date at the head of the key"
    import hashlib
    assert p.content_sha256 == hashlib.sha256(p.identity_basis.encode()).hexdigest()
    assert p.probe_ref is not None and p.probe_ref.kind == "audio_probe"
    written = ArtifactStore(workspace, RUN).read_json(p.probe_ref)
    assert written["identity_basis"] == p.identity_basis
    assert written["container"] == "mp3"


def test_probe_prefers_the_catalogue_row_the_request_carried(workspace, s3):
    row = BucketObject(key=KEY, etag="stale", size=1, last_modified="", container="mp3",
                       duration_s=3600, title="Si Se Humillare", author="El predicador",
                       recorded_at="1995-04-02", url="https://feed/1")
    p = asyncio.run(bkt.probe_object(audio_request(obj=row), RUN))
    assert p.title == "Si Se Humillare" and p.channel == "El predicador"
    # The row's etag did not match the object, so its duration was not trusted
    # and the headers were read again — and the trail says so.
    assert p.duration_s == 60
    assert "object_changed_since_sync" in p.warnings


def test_probe_refuses_what_transcribe_would_refuse_after_the_upload(workspace, s3, monkeypatch):
    with pytest.raises(ApplicationError) as e:
        asyncio.run(bkt.probe_object(audio_request(key="audios/notes.txt"), RUN))
    assert e.value.type == "audio_container_unknown"

    monkeypatch.setattr(bkt, "TRANSCRIBE_MAX_SECONDS", 30)
    with pytest.raises(ApplicationError) as e:
        asyncio.run(bkt.probe_object(audio_request(), RUN))
    assert e.value.type == "audio_too_long"

    with pytest.raises(ApplicationError) as e:
        asyncio.run(bkt.probe_object(audio_request(key="audios/missing.mp3"), RUN))
    assert e.value.type == "object_not_found"


# --- fetch ---------------------------------------------------------------------


def test_fetch_streams_hashes_sniffs_and_stages_under_our_key(workspace, s3, monkeypatch):
    uploaded: list[tuple[str, str]] = []
    monkeypatch.setattr(bkt, "_find_staged_audio", lambda settings, prefix: None)
    monkeypatch.setattr(bkt, "_upload", lambda settings, path, key, on_bytes=None:
                        uploaded.append((path, key)))
    p = asyncio.run(bkt.probe_object(audio_request(), RUN))
    staged = asyncio.run(bkt.fetch_object(RUN, audio_request(), p, "ver_x"))
    import hashlib
    assert staged.sha256 == hashlib.sha256(s3.objects[KEY]).hexdigest()
    assert staged.media_format == "mp3" and staged.bytes == len(s3.objects[KEY])
    assert uploaded and uploaded[0][1].endswith("/ver_x.mp3")
    assert staged.s3_uri == "s3://ours/transcribe/" + TNT + "/ver_x.mp3"
    assert not pathlib.Path(uploaded[0][0]).exists(), "the download is deleted either way"


def test_fetch_refuses_an_object_that_changed_since_the_quote(workspace, s3, monkeypatch):
    monkeypatch.setattr(bkt, "_find_staged_audio", lambda settings, prefix: None)
    p = asyncio.run(bkt.probe_object(audio_request(), RUN))
    s3.objects[KEY] = mp3_bytes(61)
    with pytest.raises(ApplicationError) as e:
        asyncio.run(bkt.fetch_object(RUN, audio_request(), p, "ver_x"))
    assert e.value.type == "object_changed_since_quote"


def test_fetch_reuses_what_a_previous_attempt_staged(workspace, s3, monkeypatch):
    monkeypatch.setattr(bkt, "_find_staged_audio",
                        lambda settings, prefix: ("s3://ours/transcribe/t/ver_x.mp3", 999, "mp3"))
    p = asyncio.run(bkt.probe_object(audio_request(), RUN))
    before = len(s3.gets)
    staged = asyncio.run(bkt.fetch_object(RUN, audio_request(), p, "ver_x"))
    assert staged.reused is True and len(s3.gets) == before


# --- the archive ------------------------------------------------------------------


def _staged() -> AudioStaged:
    return AudioStaged(s3_uri="s3://ours/transcribe/t/ver_x.mp3", media_format="mp3",
                       bytes=100, seconds=60, sha256="f" * 64)


def test_archive_writes_the_transcript_and_a_sidecar_naming_the_identity(workspace, s3):
    p = asyncio.run(bkt.probe_object(audio_request(), RUN))
    store = ArtifactStore(workspace, RUN)
    data = json.dumps({"results": {"items": []}}).encode()
    result = store.write_bytes("transcription_result", data)
    job = TranscriptionJob(job_name="brain-ver_x", status="COMPLETED")
    wrote = asyncio.run(bkt.archive_transcript(
        audio_request(), p, _staged(), job, result, RUN, "ver_x", "es-US"))
    assert wrote is True
    tkey, mkey = bkt.archive_keys(source(), KEY)
    assert tkey == "transcripciones/ivoox/1995-04-02_Si-Se-Humillare-a1.mp3.transcribe.json"
    assert s3.objects[tkey] == data
    sidecar = json.loads(s3.objects[mkey])
    assert sidecar["content_sha256"] == p.content_sha256
    assert sidecar["audio_sha256"] == "f" * 64
    assert sidecar["job_name"] == "brain-ver_x" and sidecar["language"] == "es-US"
    assert sidecar["model"] == "aws-transcribe-batch"

    # A second write for the same identity is a no-op.
    before = list(s3.puts)
    again = asyncio.run(bkt.archive_transcript(
        audio_request(), p, _staged(), job, result, RUN, "ver_x", "es-US"))
    assert again is False and s3.puts == before


def test_check_archive_reuses_the_same_bytes_and_never_a_reupload(workspace, s3):
    p = asyncio.run(bkt.probe_object(audio_request(), RUN))
    assert asyncio.run(bkt.check_archive(audio_request(), p, RUN)) is None

    store = ArtifactStore(workspace, RUN)
    data = json.dumps({"results": {"items": [{"type": "pronunciation"}]}}).encode()
    result = store.write_bytes("transcription_result", data)
    asyncio.run(bkt.archive_transcript(
        audio_request(), p, _staged(), TranscriptionJob("brain-ver_x", "COMPLETED"),
        result, RUN, "ver_x", "es-US"))

    found = asyncio.run(bkt.check_archive(audio_request(), p, "audio-test-2"))
    assert found is not None and found.kind == "transcription_result"
    assert ArtifactStore(workspace, "audio-test-2").read_bytes(found) == data

    # Same key, new bytes: a new identity, and the archived transcript is for
    # the old one. Not reused.
    s3.objects[KEY] = mp3_bytes(61)
    p2 = asyncio.run(bkt.probe_object(audio_request(), "audio-test-3"))
    assert p2.content_sha256 != p.content_sha256
    assert asyncio.run(bkt.check_archive(audio_request(), p2, "audio-test-3")) is None


def test_check_archive_ignores_a_file_that_is_not_transcribe_output(workspace, s3):
    p = asyncio.run(bkt.probe_object(audio_request(), RUN))
    tkey, mkey = bkt.archive_keys(source(), KEY)
    s3.objects[mkey] = json.dumps({"content_sha256": p.content_sha256}).encode()
    s3.objects[tkey] = b"this is not json"
    assert asyncio.run(bkt.check_archive(audio_request(), p, RUN)) is None


def test_archive_is_off_when_the_prefix_is_empty(workspace, s3):
    req = audio_request(source=source(archive_prefix=""))
    p = asyncio.run(bkt.probe_object(req, RUN))
    assert asyncio.run(bkt.check_archive(req, p, RUN)) is None
    store = ArtifactStore(workspace, RUN)
    result = store.write_bytes("transcription_result", b"{}")
    wrote = asyncio.run(bkt.archive_transcript(
        req, p, _staged(), TranscriptionJob("j", "COMPLETED"), result, RUN, "ver_x", "es-US"))
    assert wrote is False and s3.puts == []


# --- the link ----------------------------------------------------------------------


def test_presign_carries_the_offset_as_a_fragment(workspace, s3):
    link = asyncio.run(bkt.presign_object(MediaLinkRequest(
        source=source(), key=KEY, tenant_id=TNT, start_s=754.4, source_url="https://feed/1")))
    assert link.url.startswith("https://signed.example/") and link.url.endswith("#t=754")
    assert link.source_url == "https://feed/1" and link.start_s == 754.4
    assert link.expires_at

    with pytest.raises(ApplicationError) as e:
        asyncio.run(bkt.presign_object(MediaLinkRequest(
            source=source(), key="audios/none.mp3", tenant_id=TNT)))
    assert e.value.type == "object_not_found"


# --- writing the correction back ---------------------------------------------


def test_the_correction_is_named_by_version_and_the_raw_transcript_is_not():
    """Two different facts, named two different ways.

    A raw transcript is a fact about the audio: the same bytes give the same
    transcript, which is why its key carries no version and why a re-import can
    reuse it. A correction depends on the model, the prompt version and what
    `verify` refused, so two runs over one recording can legitimately differ —
    and overwriting would throw the comparison away.
    """
    from brainworker.activities.bucket import archive_keys, correction_keys

    source = BucketSource(bucket="b", prefix="audios/",
                          archive_prefix="transcripciones/",
                          correction_prefix="correcciones/")
    raw, meta = archive_keys(source, "audios/1994/sermon.mp3")
    assert raw == "transcripciones/1994/sermon.mp3.transcribe.json"
    assert meta == "transcripciones/1994/sermon.mp3.meta.json"

    timed, text, report = correction_keys(source, "audios/1994/sermon.mp3", "ver_a1")
    assert timed == "correcciones/1994/sermon.mp3.corregido.ver_a1.json"
    assert text == "correcciones/1994/sermon.mp3.corregido.ver_a1.txt"
    assert report == "correcciones/1994/sermon.mp3.correccion.ver_a1.json"
    # A second version lands beside the first rather than over it.
    again, _, _ = correction_keys(source, "audios/1994/sermon.mp3", "ver_b2")
    assert again != timed


def test_the_archived_correction_carries_the_times_the_flat_text_has_not():
    """`corrected.txt` is one stream of prose with no times in it.

    The times live in `chunks.jsonl`, which is also the unit a citation points
    at — so the archived paragraphs are composed from the chunks rather than
    copied from the text. Without this the customer's bucket would hold a wall
    of words nobody can take back to the recording, which is the one thing an
    audio archive is for.
    """
    from brainworker.activities.bucket import timed_paragraphs

    rows = [
        {"index": 0, "start_s": 6.82, "end_s": 86.24, "char_from": 0,
         "char_to": 1071, "text": " volver a decir lo que el domingo se dijo ",
         "overlap": "NO DEBE SALIR", "embed_text": "TAMPOCO"},
        {"index": 1, "start_s": 86.24, "end_s": 120.0, "char_from": 1071,
         "char_to": 1500, "text": ""},
    ]
    out = timed_paragraphs(rows)
    assert len(out) == 1, "a chunk with no text is not a paragraph"
    assert out[0]["start_s"] == 6.82 and out[0]["end_s"] == 86.24
    assert out[0]["text"] == "volver a decir lo que el domingo se dijo"
    # The two fields that must never reach a reader: the previous chunk's tail,
    # and the breadcrumb-prefixed string that exists only to be embedded.
    assert "overlap" not in out[0] and "embed_text" not in out[0]


def test_nothing_is_written_when_the_customer_asked_for_no_corrections(monkeypatch):
    """`correction_prefix` empty is a refusal, and it is the default.

    The raw transcript and the correction have separate switches because they
    are different kinds of thing — one is what a machine heard, the other is
    model output a verifier judged — and a customer can want the first in their
    bucket and not the second.
    """
    from brainworker.activities.bucket import archive_correction

    from brainworker.pipeline import Registered

    fake = FakeS3({})
    monkeypatch.setattr(s3source, "s3_client", lambda *a, **k: fake)
    source = BucketSource(bucket="b", prefix="audios/")
    assert source.correction_prefix == ""
    request = AudioRequest(library_id="lib", source=source, key="audios/a.mp3",
                           tenant_id="tnt_x")
    registered = Registered(document_id="doc_a", version_id="ver_a", created=True,
                            already_indexed=False)
    wrote = asyncio.run(archive_correction(request, registered, "audio-1"))
    assert wrote is False
    assert fake.puts == [], "an empty prefix must not reach the bucket at all"


def test_the_correction_reaches_the_bucket_with_its_times_and_its_report(
    tmp_path, monkeypatch
):
    """The *writing* path, end to end against a fake bucket and a real store.

    Its own test because the refusal above returns before a single artifact is
    read, so it exercises none of this — which is how a store built without a
    run id passed a green suite and would have failed on the first real
    recording.
    """
    from brainworker.activities.bucket import archive_correction
    from brainworker.pipeline import Registered

    run_id = "audio-777"
    store = ArtifactStore(tmp_path, run_id)
    text = store.write_text("corrected_text", "Primer párrafo.\n\nSegundo párrafo.")
    chunks = store.write_jsonl("chunks", [
        {"index": 0, "start_s": 1.5, "end_s": 9.0, "char_from": 0, "char_to": 16,
         "text": "Primer párrafo.", "overlap": "NO SALE"},
        {"index": 1, "start_s": 9.0, "end_s": 20.0, "char_from": 17, "char_to": 34,
         "text": "Segundo párrafo."},
    ])
    report = store.write_json("correction_report",
                              {"paragraphs": 2, "rejected": [{"reason": "proper_noun"}]})

    class FakeCatalog:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def artifacts(self, rid):
            assert rid == run_id
            return [
                {"name": "corrected_text", "rel_path": text.path,
                 "sha256": text.sha256, "size_bytes": text.bytes},
                {"name": "chunks", "rel_path": chunks.path,
                 "sha256": chunks.sha256, "size_bytes": chunks.bytes},
                {"name": "correction_report", "rel_path": report.path,
                 "sha256": report.sha256, "size_bytes": report.bytes},
            ]

    monkeypatch.setattr(bkt, "Catalog", FakeCatalog)
    monkeypatch.setattr(bkt, "_settings", lambda: type(
        "S", (), {"workspace": tmp_path, "database_url": "postgresql:///x"})())
    fake = FakeS3({})
    # The real arity, deliberately: a double that takes `*args` accepted the
    # call that forgot the assumed-role session, and the first real recording
    # is where that would have surfaced.
    monkeypatch.setattr(s3source, "s3_client", lambda session, source: fake)
    monkeypatch.setattr(s3source, "customer_session", lambda source, external_id: object())

    source = BucketSource(bucket="b", prefix="audios/", correction_prefix="correcciones/")
    request = AudioRequest(library_id="lib", source=source,
                           key="audios/1994/sermon.mp3", tenant_id="tnt_x")
    registered = Registered(document_id="doc_a", version_id="ver_a1",
                            created=True, already_indexed=False)
    assert asyncio.run(archive_correction(request, registered, run_id)) is True

    assert fake.puts == [
        "correcciones/1994/sermon.mp3.corregido.ver_a1.json",
        "correcciones/1994/sermon.mp3.corregido.ver_a1.txt",
        "correcciones/1994/sermon.mp3.correccion.ver_a1.json",
    ]
    timed = json.loads(fake.objects[fake.puts[0]])
    assert timed["version_id"] == "ver_a1" and timed["run_id"] == run_id
    assert [p["start_s"] for p in timed["paragraphs_timed"]] == [1.5, 9.0]
    # The previous chunk's tail must never reach a reader.
    assert "NO SALE" not in fake.objects[fake.puts[0]].decode()
    assert fake.objects[fake.puts[1]].decode().startswith("Primer párrafo.")
    told = json.loads(fake.objects[fake.puts[2]])
    assert told["report"]["rejected"][0]["reason"] == "proper_noun"


# --- the transcript has to be of the recording, not of itself ----------------


def _transcript(cues: list[tuple[int, int, str]]) -> bytes:
    return json.dumps({"transcription": [
        {"offsets": {"from": d, "to": h}, "text": t} for d, h, t in cues]}).encode()


def _inbox(workspace: pathlib.Path, data: bytes) -> pathlib.Path:
    inbox = workspace / "tenants" / TNT / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / "subida.transcript.json"
    path.write_bytes(data)
    return path


def test_a_transcript_that_latched_is_refused_before_anything_is_paid_for(workspace):
    """The shape of the two that reached production before anybody measured.

    `whisper.cpp` feeds the text it has produced into the next window, so once
    it repeats a line it reads that back and emits it until the audio ends. The
    document is valid, the size is ordinary and the process exits 0 — and the
    run that accepts it goes on to pay for correction and semantics over
    fabricated text. The real one repeated «¿Alguien más quiere orar conmigo?»
    **167 times** across the closing three minutes, replacing the altar call.
    """
    cues = [(i * 2000, i * 2000 + 2000, f"frase {i}") for i in range(20)]
    cues += [(40_000 + i * 1000, 41_000 + i * 1000, "¿Alguien más quiere orar conmigo?")
             for i in range(167)]
    path = _inbox(workspace, _transcript(cues))

    with pytest.raises(ApplicationError) as refusal:
        asyncio.run(bkt.stage_transcript(
            "audio-1", LocalTranscript(path=str(path), engine="whisper.cpp",
                                       model="large-v3-turbo", language="es"),
            TNT, "ver_a"))
    assert refusal.value.type == "transcript_loops"
    assert "167 veces" in str(refusal.value)
    # The remedy is in the refusal, because the person reading it is the one
    # who can apply it.
    assert "-mc 0" in str(refusal.value)
    assert not path.exists(), "the inbox is cleared whichever way it goes"


def test_a_preacher_who_repeats_a_line_is_not_a_latch(workspace):
    """Eleven of the thirteen reach a run of up to 7 and are perfectly good.

    A threshold that refused those would refuse the corpus, which is why the
    numbers come from the measurement and not from taste.
    """
    cues = [(i * 2000, i * 2000 + 2000, f"frase {i}") for i in range(60)]
    for i in range(7):                      # the healthiest corpus's worst run
        cues[20 + i] = (cues[20 + i][0], cues[20 + i][1], "gloria a Dios")
    path = _inbox(workspace, _transcript(cues))

    ref = asyncio.run(bkt.stage_transcript(
        "audio-2", LocalTranscript(path=str(path), engine="whisper.cpp",
                                   model="large-v3-turbo", language="es"),
        TNT, "ver_a"))
    assert ref.kind == "transcription_result"
    assert not path.exists()
