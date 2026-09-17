"""Activities for audio in a customer's S3 bucket: sync, probe, fetch, archive.

The judgement is next door in :mod:`brainworker.s3source` and
:mod:`brainworker.audioprobe`, both pure. This module is what touches AWS,
the workspace and the catalog, under the same three rules
:mod:`brainworker.activities.video` states in its own docstring: every network
call goes through ``asyncio.to_thread``, nothing polls in a loop, and boto3 is
imported inside the function that uses it.

**Whose credentials do what.** Everything that reads the customer's bucket
runs inside the customer's role, assumed with their tenant id as the
ExternalId. Everything that writes to *this deployment's* bucket — the staged
audio Transcribe reads, the results it writes — runs on the host's own role
through the video module's helpers, unchanged. The one object that crosses is
the audio, streamed through the worker's disk: `fetch_object` reads with
theirs and `_upload` writes with ours, exactly the shape `fetch_audio` has for
a media URL. Zero-copy was rejected because a job started under the
customer's role would run and bill in their account.

**What a retry costs.** `sync_bucket` skips the header read for a key it
already knows with the same etag and size, so a retry re-walks the listing at
S3's listing price and reads no object twice. `fetch_object` finds the staged
copy by version, like `fetch_audio`. `archive_transcript` HEADs before it PUTs.
And `check_archive` is the one that can make a whole run free: a transcript
found in the customer's archive whose sidecar names this object's identity is
used instead of starting a job.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import pathlib
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.exceptions import ApplicationError

from docagent import transcript as dt

from .. import audioprobe, s3source
from ..artifacts import ArtifactRef, ArtifactStore
from ..bucketstore import BucketStore, StoredBucket, merge_objects
from ..catalog import Catalog
from ..pipeline import (
    AudioRequest,
    AudioStaged,
    BucketObject,
    BucketSyncRequest,
    BucketSynced,
    Estimate,
    LocalTranscript,
    MediaLink,
    MediaLinkRequest,
    Registered,
    StageOptions,
    TranscriptionJob,
    VideoProbe,
)
from ..s3source import BucketAccessError, UnusableBucketSource
from .ingest import RECORD_TIMEOUT, _record, _settings
from .video import (
    HEARTBEAT_INTERVAL,
    LOCAL_TRANSCRIBER,
    MAX_AUDIO_BYTES,
    MIN_FREE_BYTES,
    TRANSCRIBE_FORMATS,
    _aws,
    _fail,
    _find_staged_audio,
    _heartbeat,
    _key,
    _upload,
    estimate_timed,
)

log = logging.getLogger(__name__)

#: Re-exported for the workflow, which reads the engine name off this module.
__all__ = ["LOCAL_TRANSCRIBER"]

#: Amazon Transcribe's batch ceilings. Refused at probe, before a gate exists,
#: so a five-hour tape is a row that says why rather than a job that fails
#: after the upload.
TRANSCRIBE_MAX_SECONDS = 4 * 3600
TRANSCRIBE_MAX_BYTES = 2 * 1024**3
#: Header reads in flight during a sync. Bounded because a page is a thousand
#: objects and two ranged GETs each; unbounded, that is two thousand sockets.
SYNC_READERS = 8
#: How many objects between heartbeats during a sync.
SYNC_HEARTBEAT_EVERY = 25
#: The sidecar's own shape, bumped when a reader could not absorb a change.
ARCHIVE_SCHEMA = 1


def _refused(e: Exception) -> Exception:
    """A bucket error as a non-retryable failure carrying its kind — twice.

    In `type`, which is what `workflows.timed.failure_of` reads to name a
    failed *run*; and in `details[0]`, which is what the paid plane's
    `describeFailure` reads to answer an *awaited* workflow — the sync and the
    link — with a `kind` a client's guidance map knows. `exporting._refused`
    carries it the second way only, because nothing awaits a run.
    """
    if isinstance(e, BucketAccessError):
        return ApplicationError(str(e), e.kind, type=e.kind, non_retryable=True)
    if isinstance(e, UnusableBucketSource):
        return ApplicationError(str(e), "bucket_source_invalid",
                                type="bucket_source_invalid", non_retryable=True)
    return e


def _store(settings, tenant_id: str) -> BucketStore:
    return BucketStore(settings.paths.for_tenant(tenant_id).buckets)


# --- sync ---------------------------------------------------------------------


@activity.defn(name="sync_bucket")
async def sync_bucket(request: BucketSyncRequest) -> BucketSynced:
    """Catalogue the bucket: list, read every header, join the manifest.

    Free — S3 requests only. Saves after every page so an interruption keeps
    what was walked, and skips the two ranged reads for an object it already
    knows with the same etag and size, which is what makes a retry cheap: the
    listing is re-walked at listing prices and no object is read twice.

    Runs on a thread with a heartbeat on the loop, for the reason every
    network-bound activity here does.
    """
    settings = _settings()
    try:
        source = s3source.checked(request.source)
    except UnusableBucketSource as e:
        raise _refused(e) from e
    store = _store(settings, request.tenant_id)
    stored = store.register(
        source,
        library_name=request.library_name,
        library_id=request.library_id,
    )
    # The library row, so the picker on every other tab can name it before a
    # single object has been quoted — a bucket *is* a library. The same write
    # the channel sync makes for the same reason, and best-effort like it:
    # the first `register_document` would create the row anyway.
    _ensure_library(settings, stored.library_id, stored.library_name,
                    request.tenant_id, source.language)

    progress: dict[str, object] = {"bytes": 0, "done": False}
    beat = asyncio.create_task(_heartbeat(progress))
    try:
        return await asyncio.to_thread(_sync, store, stored, request, progress)
    except (BucketAccessError, UnusableBucketSource) as e:
        raise _refused(e) from e
    finally:
        progress["done"] = True
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat


def _ensure_library(settings, library_id: str, name: str, tenant_id: str, language: str) -> None:
    try:
        with Catalog(settings.database_url, pooled=False, timeout=RECORD_TIMEOUT) as catalog:
            catalog.ensure_library(
                library_id, name, tenant_id=tenant_id,
                language=(language.split("-")[0] or "es"),
            )
    except Exception as e:  # noqa: BLE001 - the row is made at the first registration anyway
        log.warning("could not create the library row for %s: %s", library_id, e)


def _sync(
    store: BucketStore, stored: StoredBucket, request: BucketSyncRequest, progress: dict
) -> BucketSynced:
    source = stored.source
    session = s3source.customer_session(source, request.tenant_id)
    s3 = s3source.s3_client(session, source)
    known = {o.key: o for o in store.objects(stored.bucket_id)}
    warnings: list[str] = []

    manifest = s3source.Manifest()
    if source.manifest_key:
        try:
            data = s3source.get_bytes(s3, source.bucket, source.manifest_key)
            manifest = s3source.parse_manifest(data, source.manifest_map)
            warnings.extend(manifest.warnings)
        except BucketAccessError as e:
            # A manifest that cannot be read costs the catalogue its titles,
            # not its existence. The screen shows the warning.
            warnings.append(f"manifest {source.manifest_key!r}: {e.kind}: {e}")

    found: list[BucketObject] = []
    added = changed = absent = 0
    merged = list(known.values())
    with ThreadPoolExecutor(max_workers=SYNC_READERS) as pool:
        for page in s3source.list_pages(s3, source.bucket, source.prefix):
            rows = [r for r in page if s3source.is_candidate(r["key"], source)]
            page_objects = list(pool.map(
                lambda r: _describe_object(s3, source, r, known.get(r["key"])), rows
            ))
            found.extend(page_objects)
            progress["bytes"] = len(found)
            merged, added, changed, absent = merge_objects(
                list(known.values()), found, complete=False
            )
            # Every page lands on disk, so a sync that dies here keeps it.
            store.write_objects(stored.bucket_id, merged, complete=False)

    merged, added, changed, absent = merge_objects(list(known.values()), found, complete=True)
    merged, unmatched_rows, unmatched_objects = s3source.join_manifest(
        [replace(o, warnings=[w for w in o.warnings if not w.startswith("manifest")])
         for o in merged],
        manifest, source,
    )
    merged = [s3source.describe(o, source) for o in merged]
    estimated = sum(1 for o in merged if o.available and o.duration_estimated)
    store.write_objects(
        stored.bucket_id, merged, complete=True,
        estimated=estimated, manifest_rows=manifest.count,
        unmatched_rows=unmatched_rows, unmatched_objects=unmatched_objects,
        warnings=warnings,
    )
    return BucketSynced(
        bucket_id=stored.bucket_id,
        library_id=stored.library_id,
        objects=sum(1 for o in merged if o.available),
        added=added, changed=changed, absent=absent,
        estimated=estimated,
        manifest_rows=manifest.count,
        unmatched_rows=unmatched_rows,
        unmatched_objects=unmatched_objects,
        warnings=warnings,
    )


def _describe_object(s3, source, row: dict, known: BucketObject | None) -> BucketObject:
    """One object's row, reading its headers only when the bytes are new."""
    if known is not None and known.etag == row["etag"] and known.size == row["size"]:
        # Same bytes: what was read from them still holds, container unknown
        # included — a text file does not become audio without its etag moving.
        return replace(known, last_modified=row["last_modified"], available=True)
    obj = BucketObject(
        key=row["key"], etag=row["etag"], size=row["size"],
        last_modified=row["last_modified"],
    )
    try:
        head = s3source.head_bytes(s3, source.bucket, obj.key)
    except BucketAccessError as e:
        obj.warnings.append(f"could not read the object's head: {e.kind}")
        obj.duration_s = int(audioprobe.probe("", b"", obj.size).duration_s)
        obj.duration_estimated = True
        return obj
    container = s3source.sniff_container(head)
    tail = body = b""
    if container and audioprobe.needs_tail(container, head):
        with contextlib.suppress(BucketAccessError):
            tail = s3source.tail_bytes(s3, source.bucket, obj.key)
    if container and (at := audioprobe.needs_body(container, head)):
        with contextlib.suppress(BucketAccessError):
            body = s3source.body_bytes(s3, source.bucket, obj.key, at)
    probed = audioprobe.probe(container, head, obj.size, tail, body)
    obj.container = container
    obj.duration_s = int(round(probed.duration_s))
    obj.duration_estimated = probed.estimated
    if not container:
        obj.warnings.append("container_unknown")
    elif probed.estimated:
        obj.warnings.append(f"duration_estimated:{probed.method}")
    ext = s3source.basename(obj.key).rsplit(".", 1)[-1].lower() if "." in obj.key else ""
    if container and ext and TRANSCRIBE_FORMATS.get(ext) not in (None, TRANSCRIBE_FORMATS.get(container)):
        obj.warnings.append(f"extension_lies:.{ext} is {container}")
    if obj.duration_s > TRANSCRIBE_MAX_SECONDS:
        obj.warnings.append("too_long_for_transcribe")
    if obj.size > TRANSCRIBE_MAX_BYTES or obj.size > MAX_AUDIO_BYTES:
        obj.warnings.append("too_large_for_transcribe")
    return obj


# --- quote ----------------------------------------------------------------------


@activity.defn(name="estimate_audio")
async def estimate_audio(
    probe: VideoProbe,
    options: StageOptions,
    characters: int,
    chunk_count: int,
    characters_high: int,
    run_id: str,
    transcriber: str,
) -> Estimate:
    """`estimate_video` with an engine: Amazon's rate, or a real zero for a
    transcript the desktop app makes on the person's own machine.

    Its own activity rather than a seventh parameter on `estimate_video`,
    because Temporal maps payloads onto parameters by count and the video
    workflow passes six — see `estimate_video`'s docstring. Every parameter
    here is required for the same reason: the bucket workflow passes all
    seven, always.
    """
    return estimate_timed(probe, options, characters, chunk_count, characters_high,
                          run_id, transcriber)


# --- probe ----------------------------------------------------------------------


@activity.defn(name="probe_object")
async def probe_object(request: AudioRequest, run_id: str) -> VideoProbe:
    """The identity and the duration, for free — on the host that keeps them.

    The catalogue row the request carries is a *claim* about the object; the
    HEAD made here is the fact, and the two are compared: an etag that moved
    since the sync is a different object than the one the person ticked, so
    the row's duration is not trusted and the headers are read again.

    Produces a `VideoProbe` with no tracks and no chosen caption, which is the
    exact shape the rest of the video path already handles as "there is
    nothing to preview until the money is spent". The type is reused rather
    than renamed so the gate report, its route, the Rust proxy and both
    clients' gate views stay untouched; `identity_basis` says what the fields
    mean here.
    """
    settings = _aws()
    try:
        source = s3source.checked(request.source)
    except UnusableBucketSource as e:
        raise _refused(e) from e
    store = ArtifactStore(settings.workspace, run_id)
    try:
        obj = await asyncio.to_thread(_probe, source, request)
    except (BucketAccessError, UnusableBucketSource) as e:
        raise _refused(e) from e

    if obj.size == 0:
        raise _fail("object_empty", f"{obj.key!r} has no bytes")
    if not obj.container:
        raise _fail(
            "audio_container_unknown",
            f"{obj.key!r} does not start like any container Transcribe reads",
        )
    if obj.duration_s > TRANSCRIBE_MAX_SECONDS:
        raise _fail(
            "audio_too_long",
            f"{obj.duration_s // 60} min exceeds Transcribe's four-hour ceiling",
        )
    if obj.size > TRANSCRIBE_MAX_BYTES or obj.size > MAX_AUDIO_BYTES:
        raise _fail("audio_too_large", f"{obj.size // 1024**2} MiB de audio")

    basis = s3source.identity_basis(source.bucket, obj.key, obj.etag, obj.size)
    title = (request.title or obj.title or s3source.stem(obj.key)).strip()
    author = request.author or obj.author or ""
    warnings = list(obj.warnings)
    probe = VideoProbe(
        video_id=obj.key,
        canonical_url=s3source.canonical_url_for(source.bucket, obj.key),
        source_key=s3source.source_key_for(source.bucket, obj.key),
        title=title,
        channel=author,
        duration_s=obj.duration_s,
        upload_date=obj.recorded_at or obj.published_at or "",
        content_sha256=s3source.content_sha256_for(source.bucket, obj.key, obj.etag, obj.size),
        identity_basis=basis,
        tracks=[],
        chosen=None,
        warnings=warnings,
    )
    probe_ref = store.write_json("audio_probe", {
        "bucket": source.bucket, "key": obj.key, "etag": obj.etag, "size": obj.size,
        "container": obj.container, "duration_s": obj.duration_s,
        "duration_estimated": obj.duration_estimated,
        "title": title, "author": author, "recorded_at": obj.recorded_at,
        "published_at": obj.published_at, "source": obj.source, "url": obj.url,
        "identity_basis": basis, "content_sha256": probe.content_sha256,
        "warnings": warnings,
    })
    return replace(probe, probe_ref=probe_ref)


def _probe(source, request: AudioRequest) -> BucketObject:
    session = s3source.customer_session(source, request.tenant_id)
    s3 = s3source.s3_client(session, source)
    head = s3source.head_object(s3, source.bucket, request.key)
    if head is None:
        raise BucketAccessError("object_not_found", f"no object at {request.key!r}")
    row = {"key": request.key, "etag": head["etag"], "size": head["size"],
           "last_modified": ""}
    known = request.object
    obj = _describe_object(s3, source, row, known)
    if known is not None and (known.etag != head["etag"] or known.size != head["size"]):
        obj.warnings.append("object_changed_since_sync")
    # The manifest's fields ride on the request's row; a re-read of the CSV
    # per run would be a request per object for data the sync already joined.
    if known is not None:
        for name in ("title", "author", "recorded_at", "published_at", "source", "url"):
            if not getattr(obj, name) and getattr(known, name):
                setattr(obj, name, getattr(known, name))
    return s3source.describe(obj, source)


# --- archive: the transcript already in the customer's bucket -----------------


def archive_keys(source, key: str) -> tuple[str, str]:
    """Where the raw transcript and its sidecar live in the customer's bucket."""
    rel = s3source.relative_key(key, source.prefix)
    base = f"{s3source.normalise_prefix(source.archive_prefix)}{rel}"
    return f"{base}.transcribe.json", f"{base}.meta.json"


@activity.defn(name="check_archive")
async def check_archive(
    request: AudioRequest, probe: VideoProbe, run_id: str
) -> ArtifactRef | None:
    """A transcript this object already has in the customer's archive, or None.

    The one activity that can make a whole run free. The sidecar names the
    object's identity — the same etag-and-size proxy `content_sha256` is
    derived from — so a transcript written for these exact bytes is reused,
    and one written for a previous upload under the same key is not.
    Verified before it is trusted: the JSON has to parse to the shape
    `parse_transcribe` reads, or it is ignored with a warning rather than
    handed to the grouper.
    """
    settings = _settings()
    source = request.source
    if not source.archive_prefix:
        return None
    try:
        data = await asyncio.to_thread(_read_archive, source, request, probe)
    except (BucketAccessError, UnusableBucketSource) as e:
        # Reading the archive is an optimisation. A policy that grants no
        # read on it must not fail a run that can transcribe instead.
        log.warning("archive not readable for %s: %s", request.key, e)
        return None
    if data is None:
        return None
    try:
        dt.transcript_engine(json.loads(data))
    except (ValueError, dt.TranscriptError):
        log.warning("archived transcript for %s is not a transcript document", request.key)
        return None
    store = ArtifactStore(settings.workspace, run_id)
    return _record(run_id, "transcription_result",
                   store.write_bytes("transcription_result", data))


def _read_archive(source, request: AudioRequest, probe: VideoProbe) -> bytes | None:
    session = s3source.customer_session(source, request.tenant_id)
    s3 = s3source.s3_client(session, source)
    transcript_key, meta_key = archive_keys(source, request.key)
    meta = s3source.head_object(s3, source.bucket, meta_key)
    if meta is None:
        return None
    sidecar = json.loads(s3source.get_bytes(s3, source.bucket, meta_key, limit=1024 * 64))
    if sidecar.get("content_sha256") != probe.content_sha256:
        return None
    return s3source.get_bytes(s3, source.bucket, transcript_key)


@activity.defn(name="archive_transcript")
async def archive_transcript(
    request: AudioRequest,
    probe: VideoProbe,
    audio: AudioStaged,
    job: TranscriptionJob,
    result: ArtifactRef,
    run_id: str,
    version_id: str,
    language: str,
) -> bool:
    """Write the raw transcript back into the customer's bucket. Best-effort.

    Two objects: the Transcribe JSON as collected, and a sidecar naming the
    audio it was made from — the identity proxy, the real sha256 of the bytes
    the fetch hashed, the job, the model and the version. That is what makes
    "never pay ASR for this twice" hold even after this deployment's own
    artifact is gone, and what `check_archive` reads.

    HEAD first: an archive that already holds this identity is left alone.
    Returns whether it wrote, and never raises — the transcript is safe as the
    run artifact either way, and a bucket policy that omitted `PutObject` must
    not fail a run that paid for its transcript. The workflow writes the
    outcome on the run's trail so a silent policy gap is a number on screen.
    """
    settings = _settings()
    source = request.source
    if not source.archive_prefix:
        return False
    try:
        data = ArtifactStore(settings.workspace, run_id).read_bytes(result)
        return await asyncio.to_thread(
            _write_archive, source, request, probe, audio, job, data, run_id,
            version_id, language,
        )
    except Exception as e:  # noqa: BLE001 - best-effort by contract
        log.warning("could not archive the transcript for %s: %s", request.key, e)
        return False


def _write_archive(source, request, probe, audio, job, data: bytes, run_id,
                   version_id, language) -> bool:
    session = s3source.customer_session(source, request.tenant_id)
    s3 = s3source.s3_client(session, source)
    transcript_key, meta_key = archive_keys(source, request.key)
    existing = s3source.head_object(s3, source.bucket, meta_key)
    if existing is not None:
        sidecar = json.loads(s3source.get_bytes(s3, source.bucket, meta_key, limit=1024 * 64))
        if sidecar.get("content_sha256") == probe.content_sha256:
            return False
    sidecar = {
        "schema": ARCHIVE_SCHEMA,
        "bucket": source.bucket,
        "key": request.key,
        "identity_basis": probe.identity_basis,
        "content_sha256": probe.content_sha256,
        "audio_sha256": audio.sha256,
        "audio_bytes": audio.bytes,
        "duration_s": probe.duration_s,
        "version_id": version_id,
        "run_id": run_id,
        "job_name": job.job_name,
        "model": job.engine,
        "language": language,
        "transcript_sha256": hashlib.sha256(data).hexdigest(),
        "transcribed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    s3source.put_bytes(s3, source.bucket, transcript_key, data, "application/json")
    s3source.put_bytes(
        s3, source.bucket, meta_key,
        json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8"),
        "application/json",
    )
    return True


# --- a transcript the desktop app made ---------------------------------------------


@activity.defn(name="stage_transcript")
async def stage_transcript(
    run_id: str, upload: LocalTranscript, tenant_id: str, version_id: str
) -> ArtifactRef:
    """Take the transcript the app uploaded into the run's artifacts.

    `stage_audio`'s twin for the other direction: the app could not put the
    audio in S3 because it has no role, and it cannot write a run artifact
    because it has no workspace — so it uploads to the plane it is already
    signed in to, the file lands in the organisation's inbox, and this
    activity does the one step only the worker can do. The path is checked
    before it is opened, with `Paths.contains`, for the reason that guard
    exists everywhere: the string arrived over HTTP.

    The document has to *be* a transcript — `transcript_engine` refuses
    anything whose shape neither engine writes — before it becomes the
    artifact the grouper reads, because a grouper handed a stray file would
    fail three activities later with a message about cues.

    Idempotent under a retry in the way `stage_audio` is: the artifact is
    keyed on the run, and the inbox file is deleted whether or not the write
    landed, because a retry is told the app has to send it again rather than
    left reading a gigabyte-scale inbox it cannot clean.
    """
    settings = _settings()
    scope = settings.paths.for_tenant(tenant_id)
    path = pathlib.Path(upload.path)
    if not scope.contains(path):
        raise _fail(
            "transcript_outside_workspace",
            f"{upload.path!r} no está en el espacio de trabajo de esta organización",
        )
    try:
        data = path.read_bytes()
    except OSError as e:
        raise _fail("transcript_missing", f"no hay fichero en {upload.path!r}: {e}") from e
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    try:
        dt.transcript_engine(json.loads(data))
    except (ValueError, dt.TranscriptError) as e:
        raise _fail(
            "transcript_unreadable",
            f"lo subido no es un documento de transcripción que se sepa leer: {e}",
        ) from e
    store = ArtifactStore(settings.workspace, run_id)
    return _record(run_id, "transcription_result",
                   store.write_bytes("transcription_result", data))


# --- fetch ------------------------------------------------------------------------


@activity.defn(name="fetch_object")
async def fetch_object(
    run_id: str, request: AudioRequest, probe: VideoProbe, version_id: str
) -> AudioStaged:
    """Stream the object through the worker into our `transcribe/` prefix.

    Read with the customer's role, written with ours — the crossing the module
    docstring describes. Hashed on the way so the archive's sidecar can name
    the real bytes, and sniffed again so the staged key's suffix and the
    `MediaFormat` come from what was actually downloaded. Idempotent by the
    same lookup `fetch_audio` uses: the staged object is keyed on the version.
    """
    settings = _aws()
    tenant_id = request.tenant_id
    scope = settings.paths.for_tenant(tenant_id)
    cache = scope.cache / "audio"
    cache.mkdir(parents=True, exist_ok=True)

    key_prefix = _key(settings, tenant_id, version_id, "")
    existing = await asyncio.to_thread(_find_staged_audio, settings, key_prefix)
    if existing is not None:
        uri, size, ext = existing
        return AudioStaged(s3_uri=uri, media_format=TRANSCRIBE_FORMATS.get(ext, ext),
                           bytes=size, seconds=probe.duration_s, reused=True)

    free = shutil.disk_usage(cache).free
    if free < MIN_FREE_BYTES:
        raise _fail(
            "no_disk_for_audio",
            f"quedan {free // 1024**2} MiB en el volumen; no se descargará audio",
        )

    progress: dict[str, object] = {"bytes": 0, "done": False}
    beat = asyncio.create_task(_heartbeat(progress))
    path = cache / f"{version_id}.download"

    def got(n: int) -> None:
        progress["bytes"] = int(progress.get("bytes", 0)) + n

    try:
        try:
            digest, size = await asyncio.to_thread(
                _download, request, probe, str(path), got
            )
        except (BucketAccessError, UnusableBucketSource) as e:
            raise _refused(e) from e
        if size > MAX_AUDIO_BYTES:
            raise _fail("audio_too_large", f"{size // 1024**2} MiB de audio")
        with open(path, "rb") as f:
            head = f.read(4096)
        container = s3source.sniff_container(head)
        if container not in TRANSCRIBE_FORMATS:
            raise _fail(
                "audio_container_unknown",
                f"the downloaded bytes of {request.key!r} are not a container "
                "Transcribe reads",
            )
        key = _key(settings, tenant_id, version_id, f".{container}")
        await asyncio.to_thread(_upload, settings, str(path), key)
        return AudioStaged(
            s3_uri=f"s3://{settings.aws.bucket}/{key}",
            media_format=TRANSCRIBE_FORMATS[container],
            bytes=size,
            seconds=probe.duration_s,
            sha256=digest,
        )
    finally:
        progress["done"] = True
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        with contextlib.suppress(OSError):
            os.unlink(path)


def _download(request: AudioRequest, probe: VideoProbe, path: str, on_bytes):
    source = request.source
    session = s3source.customer_session(source, request.tenant_id)
    s3 = s3source.s3_client(session, source)
    head = s3source.head_object(s3, source.bucket, request.key)
    if head is None:
        raise BucketAccessError("object_not_found", f"no object at {request.key!r}")
    expected = s3source.content_sha256_for(source.bucket, request.key, head["etag"], head["size"])
    if expected != probe.content_sha256:
        # The bytes moved between the quote and the approval. The quote was
        # for a different object; refusing is the only honest answer.
        raise BucketAccessError(
            "object_changed_since_quote",
            f"{request.key!r} changed since it was quoted; sync and quote again",
        )
    return s3source.download(s3, source.bucket, request.key, path, on_bytes)


# --- dates on the document ---------------------------------------------------------


@activity.defn(name="set_document_dates")
async def set_document_dates(request: AudioRequest, registered: Registered) -> bool:
    """Write the manifest's dates and feed URL onto the document. Best-effort.

    Its own activity rather than a field on `IngestRequest`, whose fields the
    paid plane's `ingest.parity.spec.ts` compares against its DTO: threading a
    value only this path has through that contract would drag a DTO change
    with it. The same shape as `fill_document_metadata` — one small write
    after registration, unpooled, never failing the run.
    """
    obj = request.object
    if obj is None or not (obj.recorded_at or obj.published_at or obj.url or obj.source):
        return False
    settings = _settings()
    try:
        with Catalog(settings.database_url, pooled=False, timeout=RECORD_TIMEOUT) as catalog:
            return catalog.set_document_dates(
                registered.document_id,
                recorded_at=obj.recorded_at or None,
                published_at=obj.published_at or None,
                source_url=obj.url or None,
                source_name=obj.source or None,
                tenant_id=request.tenant_id,
            )
    except Exception as e:  # noqa: BLE001 - bookkeeping must not fail the run
        log.warning("could not set dates on %s: %s", registered.document_id, e)
        return False


# --- the link ------------------------------------------------------------------------


@activity.defn(name="presign_object")
async def presign_object(request: MediaLinkRequest) -> MediaLink:
    """A presigned GET for the object, minted now and good for under an hour.

    The `#t=` fragment is what a browser's media element reads as "start
    here", so the link a citation offers opens at the cited second.
    """
    try:
        source = s3source.checked(request.source)
        url = await asyncio.to_thread(_presign, source, request)
    except (BucketAccessError, UnusableBucketSource) as e:
        raise _refused(e) from e
    expires = datetime.now(timezone.utc) + timedelta(seconds=s3source.PRESIGN_SECONDS)
    fragment = f"#t={int(request.start_s)}" if request.start_s > 0 else ""
    return MediaLink(
        url=url + fragment,
        expires_at=expires.isoformat(timespec="seconds"),
        start_s=request.start_s,
        source_url=request.source_url,
    )


def _presign(source, request: MediaLinkRequest) -> str:
    session = s3source.customer_session(source, request.tenant_id)
    s3 = s3source.s3_client(session, source)
    if s3source.head_object(s3, source.bucket, request.key) is None:
        raise BucketAccessError("object_not_found", f"no object at {request.key!r}")
    return s3source.presigned_get(s3, source.bucket, request.key)
