"""Activities for indexing a video: probe, transcribe, group, chunk.

Everything that touches the network or a store. The judgement is next door in
:mod:`brainworker.videosource` and :mod:`docagent.transcript`, both pure and both
tested without any of this — the same split ``auditversion.py`` makes, and for
the same reason: what is worth asserting about a timestamp is arithmetic, and a
test that needed a video to check it would run rarely enough to be worth nothing.

Three rules this module exists inside, none of them obvious from one function:

- **Every network call goes through ``asyncio.to_thread``.** yt-dlp and botocore
  are synchronous. An ``async def`` activity that calls one directly holds the
  worker's only event loop for the whole call, which is not a slow activity but
  a frozen *worker* — and the heartbeat that would report it cannot be flushed,
  because flushing is what the blocked loop was going to do.
- **Nothing here polls in a loop.** The host stops nightly at 23:00 with no
  start schedule, and a running activity is worker-local state that a stop
  destroys, retried from the top. The waiting is a ``workflow.sleep`` in
  :mod:`brainworker.workflows.video`; ``poll_transcription`` is single-shot and
  short so it can never be the thing in flight when the host goes down.
- **yt-dlp and boto3 are imported inside the functions that use them.** The
  house pattern (``correct_text`` does it for the engine), and here it also
  means this module imports on a machine that has neither, so the pure paths and
  the workflow tests do not need them installed.
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

from temporalio import activity
from temporalio.exceptions import ApplicationError

from dataclasses import replace

from .. import videosource
from ..artifacts import ArtifactRef, ArtifactStore
from ..indexing import chunk_row as indexing_chunk_row
from ..pipeline import (
    AudioStaged,
    CaptionTrack,
    ChunkKindCount,
    Chunked,
    Estimate,
    Preview,
    Spend,
    StageEstimate,
    StageOptions,
    Transcribed,
    TranscriptionJob,
    VideoInfo,
    VideoProbe,
    VideoRequest,
)
from .ingest import _record, _settings, estimate_for
from .paid import _charge

log = logging.getLogger(__name__)

#: How often the audio download reports progress to Temporal.
HEARTBEAT_INTERVAL = 5.0

#: Refuse to download when the volume has less than this much room left.
#:
#: `/srv/brain` is 60 GiB and holds Docker's data-root, the four stores and the
#: corpus. A four-hour download is a couple of hundred megabytes, which is
#: nothing until the disk is nearly full and then it is the thing that fills it.
MIN_FREE_BYTES = 4 * 1024**3

#: Amazon Transcribe's own ceiling is 2 GB / 4 hours; this is well under it and
#: exists to fail fast rather than after a long download.
MAX_AUDIO_BYTES = 1024**3

#: Container formats Amazon Transcribe reads directly.
#:
#: The whole point of the list: if yt-dlp can hand us one of these untouched,
#: **no transcode runs and the image needs no ffmpeg** — which is the difference
#: between the current worker image and one carrying ~80 MB of apt that
#: `worker/Dockerfile` deliberately has no compiler for.
TRANSCRIBE_FORMATS = {"m4a": "mp4", "mp4": "mp4", "webm": "webm", "ogg": "ogg",
                      "opus": "ogg", "mp3": "mp3", "flac": "flac", "wav": "wav"}


def _higher(a: float | None, b: float | None) -> float | None:
    """The larger of two prices, treating "no price known" as no opinion.

    None is not zero — `price_for` returns it for a model with no published
    rate — so a stage nobody can price must not come back priced at the other
    end's figure.
    """
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _fail(kind: str, message: str) -> ApplicationError:
    """A permanent failure, so Temporal stops retrying it.

    An age-restricted video, a members-only one and a bad URL are all decisions
    that will not change on the second attempt, and three retries against them
    is three times the wait before somebody is told why.
    """
    return ApplicationError(message, type=kind, non_retryable=True)


# --- probing -----------------------------------------------------------------


@activity.defn(name="resolve_video")
async def resolve_video(request: VideoRequest) -> VideoInfo:
    """Ask YouTube what this video is. **The one call that has to run elsewhere.**

    Split out of `probe_video` for one measured reason: on 2026-09-05 this call
    succeeded from a residential IP in 2.6 s and was refused from the EC2 egress
    IP `34.218.169.144` — "Sign in to confirm you're not a bot" — while the same
    host fetched every signed caption URL at 200 and served the `youtube.com`
    watch page at 200. It is the player API that is bot-checked, not the network.
    So this is what `VideoRequest.fetch_queue` routes to a worker on an address
    YouTube will answer, and everything else stays where the workspace is.

    It writes nothing and returns a **narrowed** record. The raw info dict is
    1,656,277 bytes on a real video; `VideoInfo` is a few kilobytes, which is the
    difference between a payload and a rule broken.

    The URL allowlist lives here because this is where yt-dlp is called — the
    SSRF guard belongs against the ~1800 extractors, not against a dataclass.
    The route checks it too, for the reason `retrieve.search` doubles up on
    `tenant_id`.
    """
    vid = _video_id_or_fail(request.url)
    info = await asyncio.to_thread(_extract_info, videosource.watch_url(vid))

    if info.get("is_live") or info.get("live_status") in {
        "is_upcoming", "is_live", "post_live",
    }:
        raise _fail("video_is_live", "un directo no tiene duración ni final")
    duration = int(info.get("duration") or 0)
    if duration <= 0:
        raise _fail("video_has_no_duration", "el vídeo no declara duración")

    tracks = _tracks(info)
    chosen = _choose_track(tracks, request.languages)
    return VideoInfo(
        video_id=vid,
        title=str(info.get("title") or ""),
        channel=str(info.get("uploader") or info.get("channel") or ""),
        duration_s=duration,
        upload_date=str(info.get("upload_date") or ""),
        format_id=str(info.get("format_id") or ""),
        tracks=tracks,
        chosen=chosen,
        caption_url=_caption_url(info, chosen) if chosen else "",
    )


@activity.defn(name="probe_video")
async def probe_video(
    request: VideoRequest, run_id: str, info: VideoInfo
) -> VideoProbe:
    """The identity, the caption bytes and the artifacts — on the host that keeps
    them.

    Takes what `resolve_video` learned rather than asking YouTube itself, which
    is what lets the two run on different workers. The caption download stays
    here on purpose and it is measured, not assumed: a caption URL carries
    `ip=0.0.0.0` and the EC2 host fetched every one of them at 200, including
    two that were being 429'd from the developer's own address. So no caption
    bytes ever cross a payload — the 4,573-second video in the report measures
    **582,176 bytes** of VTT, which is a fifth of Temporal's ceiling on one
    video and over it on a four-hour one.

    Its artifacts are still recorded by `record_video_artifacts` after
    `register_document`, and that is deliberate rather than left over. The run
    row does exist by now — `open_run` writes it before the first activity — but
    that write is **best-effort**, because bookkeeping must never fail a stage
    that has not spent anything. `_record` derives its tenant from the run and
    silently drops a write with no row to find, so recording after registration
    is the path that does not depend on a best-effort write having landed.
    """
    # Whoever produced this record, it is held to the same rule. `resolve_video`
    # satisfies it trivially; a `VideoInfo` that arrived in `VideoRequest.resolved`
    # from a client may not, and this is the activity that fetches its
    # `caption_url` — so the check belongs here, in front of the request, and
    # not only at the route that accepted it.
    try:
        videosource.check_resolved(request.url, info)
    except videosource.ResolutionNotTrusted as e:
        raise _fail("resolution_not_trusted", str(e)) from e

    vid = info.video_id
    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)

    duration = info.duration_s
    tracks = info.tracks
    chosen = info.chosen
    captions_ref: ArtifactRef | None = None
    caption_digest = ""
    warnings: list[str] = []

    if chosen is not None:
        data = await asyncio.to_thread(
            _download_caption, info.caption_url, chosen.language
        )
        if data:
            captions_ref = store.write_bytes("captions", data)
            caption_digest = captions_ref.sha256
        else:
            chosen = None
            warnings.append(
                "la pista de subtítulos anunciada no se pudo descargar; "
                "se transcribirá el audio"
            )
    # Deliberately no warning for "this video has no captions". `chosen = None`
    # already carries that fact, and the gate states it twice in its own words —
    # on the transcript line and in the paragraph explaining why there is
    # nothing to preview. A third copy, phrased more weakly than either, reads
    # as a separate problem. A track that was *advertised and would not
    # download* is different: that is surprising, and it stays above.

    # The identity, spelled out so the digest can be re-derived by hand from the
    # artifact rather than taken on trust. On the caption path the caption bytes
    # are in it and it is a true content hash; on the Transcribe path it is a
    # proxy over the facts that were free to learn — see `VideoProbe`.
    source = f"captions:{chosen.language}:{chosen.kind}" if chosen else "transcribe"
    basis = "\n".join([
        "youtube", vid, str(duration), info.upload_date,
        source, caption_digest or info.format_id,
    ])

    probe = VideoProbe(
        video_id=vid,
        canonical_url=videosource.watch_url(vid),
        source_key=videosource.source_key(vid),
        title=(request.title or info.title or f"YouTube {vid}").strip(),
        channel=info.channel,
        duration_s=duration,
        upload_date=info.upload_date,
        content_sha256=hashlib.sha256(basis.encode("utf-8")).hexdigest(),
        identity_basis=basis,
        tracks=tracks,
        chosen=chosen,
        warnings=warnings,
    )
    probe_ref = store.write_json("video_probe", {
        "video_id": vid, "title": probe.title, "channel": probe.channel,
        "duration_s": duration, "upload_date": probe.upload_date,
        "identity_basis": basis, "content_sha256": probe.content_sha256,
        "tracks": [t.__dict__ for t in tracks],
        "chosen": chosen.__dict__ if chosen else None,
        "captions": captions_ref.path if captions_ref else None,
    })
    return replace(probe, probe_ref=probe_ref, captions=captions_ref)


def _video_id_or_fail(url: str) -> str:
    try:
        return videosource.video_id(url)
    except videosource.NotAVideoUrl as e:
        # The allowlist inside `video_id` is also this module's SSRF guard:
        # yt-dlp ships ~1800 extractors and a `generic` one that will fetch an
        # arbitrary host, and unlike `stage_source` there is no `Paths.contains`
        # to inherit. Refusing here and again at the API route is the same
        # belt-and-braces `retrieve.search` keeps for `tenant_id`.
        raise _fail("not_a_video_url", str(e)) from e


def _ydl(**extra):
    from yt_dlp import YoutubeDL

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "noprogress": True, "extract_flat": False}
    opts.update(extra)
    return YoutubeDL(opts)


#: Phrases YouTube uses when it is refusing *this caller* rather than the video.
#:
#: Lowercase, matched against a lowercased message. Kept as data beside the
#: function so the classification can be tested without yt-dlp installed, which
#: is the same split the rest of this module makes.
_REFUSAL_MARKERS = (
    "confirm you're not a bot",
    "confirm you are not a bot",
    "sign in to confirm",
    "too many requests",
    "http error 429",
)


def _download_error_kind(message: str) -> str:
    """`video_unavailable` is a fact about the video; this may be about us.

    Every `DownloadError` used to collapse into `video_unavailable`, which is
    right for a private, deleted, age-gated or geo-blocked video — a decision
    that will not change on the second attempt — and wrong for the one that
    actually happened. Measured 2026-09-05: `yq6uVBsVkeQ` probes fine from a
    residential IP in 2.6 s and is refused from the EC2 egress IP
    `34.218.169.144` with "Sign in to confirm you're not a bot". The video is
    available; the caller is blocked. Different cause, different remedy, and the
    guidance a person reads is keyed on the kind.

    Still non-retryable. `_RETRY` is three attempts seconds apart and an IP block
    does not clear in three seconds; a policy shaped for a 429 — minutes of
    backoff — is a separate decision and would want measuring first.
    """
    low = message.lower()
    if any(marker in low for marker in _REFUSAL_MARKERS):
        return "youtube_refused_this_host"
    return "video_unavailable"


def _extract_info(url: str) -> dict:
    from yt_dlp.utils import DownloadError

    try:
        with _ydl(skip_download=True, writesubtitles=False) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except DownloadError as e:
        raise _fail(_download_error_kind(str(e)), str(e)) from e


def _tracks(info: dict) -> list[CaptionTrack]:
    """Every caption track the video offers, manual ones first.

    Manual before automatic is not a preference, it is a quality ordering: an
    auto track has no punctuation and carries the rolling duplicates
    `dedupe_rolling` exists to remove.
    """
    out: list[CaptionTrack] = []
    for kind, key in (("manual", "subtitles"), ("auto", "automatic_captions")):
        for lang, formats in (info.get(key) or {}).items():
            exts = {f.get("ext") for f in formats or []}
            ext = "vtt" if "vtt" in exts else next(iter(exts - {None}), "")
            if ext:
                out.append(CaptionTrack(language=lang, kind=kind, ext=ext))
    return out


def _choose_track(
    tracks: list[CaptionTrack], preferred: list[str]
) -> CaptionTrack | None:
    """Which track to read, or None when Amazon Transcribe has to run.

    Language preference first, then manual over automatic — in that order,
    because a human transcript in the wrong language answers no question, while
    a machine transcript in the right one answers most of them.

    **And the original language before any translation of it.** YouTube offers
    an automatic track in every language it can translate into, and marks the
    source one with an `-orig` suffix. Measured 2026-09-10 on `yq6uVBsVkeQ`, a
    76-minute talk in Spanish: **157 automatic tracks, returned in alphabetical
    order by code** — `ab`, `aa`, `af`, `ak`, `sq`, … — with the real one at
    `es-orig`. So a caller that expressed no preference used to be handed
    **Abkhazian**, a machine translation of a machine transcription, and the
    gate said so only as a two-letter code in a line of small print. That is
    the shape of thing that gets approved.

    Sorted rather than special-cased in the loop, because the rule holds inside
    a language preference too: `es` matches both `es` and `es-orig` by prefix,
    and where a video offers both, `es-orig` is the one that was not round
    tripped through a translator. The sort is **stable**, so it reorders
    nothing else — it decides ties, which is all it should do.
    """
    vtt = [t for t in tracks if t.ext == "vtt"]
    vtt.sort(key=lambda t: not t.language.endswith("-orig"))
    for lang in [*preferred, ""]:
        for kind in ("manual", "auto"):
            for t in vtt:
                if t.kind == kind and (not lang or t.language.startswith(lang)):
                    return t
    return None


#: How many times to ask for a caption track before giving up on it.
#:
#: **Not politeness — money.** An empty return here sets `chosen = None`, and
#: `chosen = None` is the branch that pays Amazon to transcribe the audio. So a
#: single transient 429 on a free text file silently buys a transcription.
#: Measured 2026-09-05: the `timedtext` endpoint refused this developer's
#: residential IP on six attempts across 25 minutes while serving the *same*
#: URLs to the EC2 host at 200 — so the throttle is per-IP, real, and lasts long
#: enough to matter.
CAPTION_ATTEMPTS = 3

#: Seconds before each retry, so three attempts span about half a minute. A 429
#: on this endpoint is not cleared by an immediate retry.
CAPTION_BACKOFF = (5.0, 20.0)


def _download_caption(url: str, language: str = "") -> bytes:
    """Fetch one caption track's bytes, or b"" if it cannot be had.

    Empty rather than raising: a track the listing advertised and the server
    will not serve is a reason to fall back to Transcribe, not a reason to fail
    a run that has not spent anything yet. But falling back is not free — it is
    the whole Transcribe bill — so this asks more than once first.

    Takes the URL rather than the info dict. The dict is 1.66 MB on a real video
    (measured on `yq6uVBsVkeQ`, which offers 161 automatic caption languages),
    and it does not cross a Temporal payload; the URL does. Caption URLs carry
    `ip=0.0.0.0` and are **not** bound to the address that resolved them, which
    is why this can run on a different host from `resolve_video` — measured, not
    assumed, and the opposite of a media URL.
    """
    import time
    import urllib.request

    if not url:
        return b""
    for attempt in range(1, CAPTION_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - any failure means "try again"
            log.warning(
                "caption download failed for %s (attempt %d/%d): %s",
                language or "?", attempt, CAPTION_ATTEMPTS, e,
            )
            if attempt < CAPTION_ATTEMPTS:
                time.sleep(CAPTION_BACKOFF[min(attempt - 1, len(CAPTION_BACKOFF) - 1)])
    return b""


def _caption_url(info: dict, track: CaptionTrack) -> str:
    """The signed URL for one track, out of the listing yt-dlp returned."""
    key = "subtitles" if track.kind == "manual" else "automatic_captions"
    for fmt in info.get(key, {}).get(track.language, []):
        if fmt.get("ext") == track.ext and fmt.get("url"):
            return str(fmt["url"])
    return ""


@activity.defn(name="record_video_artifacts")
async def record_video_artifacts(run_id: str, refs: list[ArtifactRef]) -> None:
    """Attach artifacts written before the run row existed.

    The same shape as `record_run_events`, for the same reason: `_record`
    derives its tenant from the run, so anything written by `probe_video` — which
    has to run first, because registration needs the identity it computes — would
    otherwise leave the file on disk and no row in the catalog.

    Takes the references `probe_video` returned, not kind names: a reference is
    a path, a hash and a size, and re-deriving one here would record whatever is
    on disk now rather than what the activity actually wrote.
    """
    for ref in refs:
        _record(run_id, ref.kind, ref)


# --- grouping: cues in, timed paragraphs out ---------------------------------


@activity.defn(name="group_transcript")
async def group_transcript(
    run_id: str, probe: VideoProbe, source_ref: ArtifactRef
) -> Transcribed:
    """Turn either transcript source into the paragraph stream and its clock.

    One activity for both paths, which is the whole reason ``grouping`` is a
    stage of its own: the two sources differ only in how the cues are parsed,
    and an artifact attributed to two different stages would make
    ``ARTIFACT_STAGES`` a lie on one of them.

    ``transcript_text`` and ``transcript`` are a **matched pair** — the byte
    stream and the paragraph-index-to-time table that indexes it — so they are
    written together here and read back through their refs, whose hashes are
    verified. A stale one of either is the drift everything else guards against.
    """
    from docagent import transcript as dt
    from docagent.extract import join_paragraphs

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    raw = store.read_bytes(source_ref)

    if source_ref.kind == "captions":
        cues = dt.parse_vtt(raw)
        # Only an automatic track scrolls. Applying the repair to a manual one
        # would eat a speaker legitimately repeating themselves.
        if probe.chosen is not None and probe.chosen.kind == "auto":
            cues = dt.dedupe_rolling(cues)
        source = f"captions:{probe.chosen.language}:{probe.chosen.kind}" \
            if probe.chosen else "captions"
    else:
        cues = dt.parse_transcribe(json.loads(raw.decode("utf-8")))
        source = "transcribe"

    if not cues:
        raise _fail("transcript_is_empty", "el transcript no contiene texto")

    groups = dt.group_cues(cues)
    paragraphs = dt.to_paragraphs(groups)
    table = videosource.table_from_groups(groups)
    videosource.assert_aligned(paragraphs, table)

    body = join_paragraphs(paragraphs)
    text_ref = _record(run_id, "transcript_text", store.write_bytes("transcript_text", body))
    cues_ref = _record(run_id, "transcript", store.write_json("transcript", {
        "video_id": probe.video_id,
        "source": source,
        "cues": len(cues),
        "paragraphs": [
            {"para": t.idx, "start_s": t.start_s, "end_s": t.end_s} for t in table
        ],
    }))
    # `Extraction` requires one, and the profile-collision check that reads it
    # never runs for a video — but writing a real one keeps a video's run
    # directory readable by exactly the tooling a book's is.
    evidence_ref = _record(run_id, "evidence", store.write_json("evidence", {
        "source": probe.canonical_url,
        "extractor": "youtube_captions" if source.startswith("captions") else "aws_transcribe",
        "pages": 1,
        "notes": [f"{len(cues)} cues grouped into {len(groups)} paragraphs"],
    }))
    return Transcribed(
        text=text_ref,
        cues=cues_ref,
        evidence=evidence_ref,
        source=source,
        paragraphs=len(groups),
        characters=len(body.decode("utf-8")),
        covered_s=max((g.end_s for g in groups), default=0.0),
    )


@activity.defn(name="preview_transcript")
async def preview_transcript(run_id: str, transcribed: Transcribed) -> Preview:
    """Chunk the transcript as it stands, so the gate can show real numbers.

    Only reachable on the caption path, where the text was free. Without
    captions there is nothing to preview until the money has been spent, and the
    gate says so rather than showing a fabricated count.
    """
    from docagent import transcript as dt
    from docagent.chunk import build_chunks, split_paragraphs

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    data = store.read_bytes(transcribed.text)
    chunks = build_chunks(data, split_paragraphs(data), dt.chunk_rules(), dt.classify)
    rows = [
        {"index": c.index, "kind": c.kind, "chapter": c.chapter, "section": c.section,
         "text": c.text, "char_from": c.char_from, "char_to": c.char_to}
        for c in chunks
    ]
    ref = _record(run_id, "preview_chunks", store.write_jsonl("preview_chunks", rows))
    kinds = {}
    for c in chunks:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    return Preview(
        text=transcribed.text,
        chunks=ref,
        chunk_count=len(chunks),
        kinds=[ChunkKindCount(k, n) for k, n in sorted(kinds.items())],
        characters=transcribed.characters,
        # Correction changes the text's length, so these are not the chunks that
        # will be indexed if it runs — the same rule the document gate obeys.
        chunks_are_final=False,
        warnings=[],
    )


@activity.defn(name="estimate_video")
async def estimate_video(
    probe: VideoProbe,
    options: StageOptions,
    characters: int,
    chunk_count: int,
    characters_high: int = 0,
) -> Estimate:
    """What this video will cost, transcription included.

    The pipeline half goes through `estimate_for`, the same arithmetic the
    document gate uses — a second implementation would eventually quote two
    different bills for one pipeline. Only the transcription row is new, and it
    is the first in this product that is not tokens times a multiplier.
    """
    settings = _settings()
    estimate = estimate_for(characters, chunk_count, options, None)

    if characters_high > characters:
        # Only the no-captions path sends this. The character count there is
        # projected from the video's duration, and it is the single most
        # uncertain input in the product — so the range has to say so rather
        # than quoting one number it cannot stand behind. Re-running the same
        # estimator at the top of the measured speech-rate range is how: the low
        # figure stays within reach of a typical video and the high one covers
        # the fastest, which is the pairing `OUTPUT_SPREAD` makes for semantics.
        wide = {row.stage: row for row in
                estimate_for(characters_high, chunk_count, options, None).stages}
        stages = [
            replace(
                row,
                output_tokens_high=max(
                    row.output_tokens_high, wide[row.stage].output_tokens_high
                ),
                usd_high=_higher(row.usd_high, wide[row.stage].usd_high),
            )
            for row in estimate.stages
        ]
        highs = [r.usd_high for r in stages if r.usd_high is not None]
        estimate = replace(
            estimate, stages=stages, total_usd_high=sum(highs) if highs else None
        )

    if probe.chosen is not None:
        return estimate  # captions are free; nothing to add

    usd = videosource.transcribe_usd(probe.duration_s, settings.aws.usd_per_minute)
    row = StageEstimate(
        stage="transcription",
        model="aws-transcribe-batch",
        # Zero, and true: Transcribe bills seconds of audio, not tokens. Putting
        # the duration in a token column would make it a term in every total the
        # UI sums.
        input_tokens=0,
        output_tokens=0,
        usd=usd,
        output_tokens_high=0,
        # Exact rather than a range: the bill is duration times a published
        # rate, and the same duration is what the charge is taken from, so the
        # two cannot disagree.
        usd_high=usd,
    )
    stages = [row, *estimate.stages]
    priced = [s.usd for s in stages if s.usd is not None]
    priced_high = [s.usd_high for s in stages if s.usd_high is not None]
    return Estimate(
        stages=stages,
        total_usd=sum(priced) if priced else None,
        total_usd_high=sum(priced_high) if priced_high else None,
        price_source=estimate.price_source,
        unpriced_stages=[s.stage for s in stages if s.usd is None],
    )


# --- Amazon Transcribe -------------------------------------------------------


def _aws():
    """The AWS config, refused early when the deployment cannot transcribe.

    Checked before the gate quotes a job, so "this stack has no bucket" is a
    refusal somebody reads while deciding rather than a failure forty minutes
    into a run they approved.
    """
    settings = _settings()
    if not settings.aws.configured:
        raise _fail(
            "transcribe_not_configured",
            "no hay región ni bucket de AWS configurados para transcribir",
        )
    return settings


def _key(settings, tenant_id: str, version_id: str, suffix: str) -> str:
    return f"{settings.aws.prefix}/{tenant_id}/{version_id}{suffix}"


def job_name_for(version_id: str) -> str:
    """The Transcribe job name for a version. Derived, never generated.

    This is what makes starting idempotent. A Temporal retry calls
    `StartTranscriptionJob` again, hits `ConflictException`, and reads it as
    *already started* — so one version can only ever have one job, and Amazon
    bills for it once. A uuid here would mean a retried attempt paying twice for
    the same audio.

    `ver_` plus 24 hex satisfies Amazon's `^[0-9a-zA-Z._-]{1,200}$`.
    """
    return f"brain-{version_id}"


@activity.defn(name="fetch_audio")
async def fetch_audio(
    run_id: str, probe: VideoProbe, tenant_id: str, version_id: str
) -> AudioStaged:
    """Download the audio and put it where Transcribe can read it.

    Costs nothing and takes minutes, which is the combination that needs a
    heartbeat. The counting happens on the download thread and the sending on
    the loop, because `activity.heartbeat` is loop-bound for an `async def`
    activity — temporalio installs its thread-safe wrapper only for *sync*
    activities run in an executor. Recording heartbeats from inside the blocking
    call and never flushing them is the exact failure `embed_and_index` was
    fixed for.
    """
    settings = _aws()
    scope = settings.paths.for_tenant(tenant_id)
    cache = scope.cache / "video"
    cache.mkdir(parents=True, exist_ok=True)

    key_prefix = _key(settings, tenant_id, version_id, "")
    existing = await asyncio.to_thread(_find_staged_audio, settings, key_prefix)
    if existing is not None:
        # The object is keyed on the version, so a retry — or a re-index after
        # the nightly stop — finds the audio already uploaded and skips the
        # download entirely. This is what makes the whole stage cheap to retry.
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
    path: pathlib.Path | None = None
    try:
        path = await asyncio.to_thread(
            _download_audio, probe.canonical_url, cache, version_id, progress
        )
        size = path.stat().st_size
        if size > MAX_AUDIO_BYTES:
            raise _fail("audio_too_large", f"{size // 1024**2} MiB de audio")
        ext = path.suffix.lstrip(".").lower()
        if ext not in TRANSCRIBE_FORMATS:
            raise _fail(
                "audio_format_unsupported",
                f"yt-dlp entregó .{ext}, que Transcribe no lee; "
                "transcodificar exigiría ffmpeg en la imagen",
            )
        key = _key(settings, tenant_id, version_id, f".{ext}")
        await asyncio.to_thread(_upload, settings, str(path), key)
        return AudioStaged(
            s3_uri=f"s3://{settings.aws.bucket}/{key}",
            media_format=TRANSCRIBE_FORMATS[ext],
            bytes=size,
            seconds=probe.duration_s,
        )
    finally:
        progress["done"] = True
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        # Whether or not this worked. The file is a transient on the same 60 GiB
        # volume as the corpus, and S3 now holds whatever mattered.
        if path is not None:
            with contextlib.suppress(OSError):
                os.unlink(path)


@activity.defn(name="stage_audio")
async def stage_audio(
    run_id: str,
    audio_path: str,
    probe: VideoProbe,
    tenant_id: str,
    version_id: str,
) -> AudioStaged:
    """Put audio the *caller* downloaded where Transcribe can read it.

    `fetch_audio`'s twin, and the division between them is the measured one: a
    `googlevideo` media URL carries the address that resolved it and answers 403
    anywhere else, so the download has to happen beside the `extract_info` that
    produced it. When that was the desktop app — because YouTube refuses this
    deployment's own address — the app has the bytes and cannot put them in S3,
    which needs the instance role. So it uploads them to the plane it is already
    authenticated to and this activity, on the host that holds the role, does
    the one step it can do.

    **The path is checked before it is opened**, with `Paths.contains` — the
    same guard `stage_source` makes for a document import, and needed for the
    same reason: the string arrives over HTTP and a string becomes a path.
    `POST /videos/audio` derives the filename itself and answers with it, so a
    well-behaved client never names anything else; a badly behaved one is
    refused here rather than reading another organisation's inbox.

    It is idempotent in the same way `fetch_audio` is and by the same lookup: the
    object is keyed on the version, so a retry finds it uploaded and returns
    `reused=True` without reading the file again. That matters more here than
    there, because the retry cannot re-download — the file the client staged is
    deleted once it is in S3.
    """
    settings = _aws()
    scope = settings.paths.for_tenant(tenant_id)
    path = pathlib.Path(audio_path)
    if not scope.contains(path):
        raise _fail(
            "audio_outside_workspace",
            f"{audio_path!r} no está en el espacio de trabajo de esta organización",
        )

    key_prefix = _key(settings, tenant_id, version_id, "")
    existing = await asyncio.to_thread(_find_staged_audio, settings, key_prefix)
    if existing is not None:
        uri, size, ext = existing
        with contextlib.suppress(OSError):
            os.unlink(path)
        return AudioStaged(s3_uri=uri, media_format=TRANSCRIBE_FORMATS.get(ext, ext),
                           bytes=size, seconds=probe.duration_s, reused=True)

    if not path.is_file():
        raise _fail("audio_missing", f"no hay fichero en {audio_path!r}")
    size = path.stat().st_size
    if size > MAX_AUDIO_BYTES:
        raise _fail("audio_too_large", f"{size // 1024**2} MiB de audio")
    ext = path.suffix.lstrip(".").lower()
    if ext not in TRANSCRIBE_FORMATS:
        # The same refusal `fetch_audio` makes and for the same reason:
        # transcoding would mean ffmpeg in the image, and the worker's
        # "no compiler, no toolchain" property is worth more than one format.
        raise _fail(
            "audio_format_unsupported",
            f"la aplicación subió .{ext}, que Transcribe no lee",
        )

    progress: dict[str, object] = {"bytes": 0, "done": False}
    beat = asyncio.create_task(_heartbeat(progress))

    def sent(n: int) -> None:
        progress["bytes"] = int(progress.get("bytes", 0)) + n

    try:
        key = _key(settings, tenant_id, version_id, f".{ext}")
        await asyncio.to_thread(_upload, settings, str(path), key, sent)
        return AudioStaged(
            s3_uri=f"s3://{settings.aws.bucket}/{key}",
            media_format=TRANSCRIBE_FORMATS[ext],
            bytes=size,
            seconds=probe.duration_s,
        )
    finally:
        progress["done"] = True
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        # Deleted whether or not the upload worked, exactly as `fetch_audio`
        # deletes its download. The inbox is on the same volume as the corpus
        # and this file is up to a gigabyte; a failed run must not leave one
        # behind, and a retry re-uploads from S3's own copy or is told the
        # client has to send it again.
        with contextlib.suppress(OSError):
            os.unlink(path)


@activity.defn(name="discard_audio")
async def discard_audio(audio_path: str, tenant_id: str) -> None:
    """Throw away audio the caller staged for a run that will not transcribe it.

    `stage_audio` deletes the file whether or not its upload worked, exactly as
    `fetch_audio` deletes its own download — but a run that is *rejected at the
    gate*, or that finds the video already indexed, never reaches it. The bytes
    are already on disk by then, because the download happens before the gate:
    it costs nothing in dollars, so it is not what the gate is guarding, and
    parking a workflow after approval until a laptop sends audio makes a run
    that stalls invisibly when the window is closed.

    Up to a gigabyte, on the volume that also holds the corpus, so this is worth
    a line. A staged file that outlives its run is not a *new* property — an
    upload through `POST /uploads` whose ingest is never started stays in the
    inbox in exactly the same way — but three orders of magnitude is enough of a
    difference to treat differently.

    Best-effort, like every other bookkeeping write here: a file that cannot be
    removed is a byte on a volume, and failing a run over it would be worse.
    Containment is still checked, because the argument is still a path that
    arrived over HTTP and this function's whole job is to delete something.
    """
    if not audio_path:
        return
    try:
        scope = _settings().paths.for_tenant(tenant_id)
        path = pathlib.Path(audio_path)
        if scope.contains(path):
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - never fail a run over a leftover file
        log.warning("could not discard staged audio at %s", audio_path)


async def _heartbeat(progress: dict) -> None:
    while not progress.get("done"):
        if activity.in_activity():
            activity.heartbeat(progress.get("bytes", 0))
        await asyncio.sleep(HEARTBEAT_INTERVAL)


def _download_audio(
    url: str, into: pathlib.Path, version_id: str, progress: dict
) -> pathlib.Path:
    """Audio only, and **no postprocessor**.

    `bestaudio` with no recode is what keeps ffmpeg out of the worker image.
    If nothing acceptable comes back, that is a refusal with a message rather
    than a silent pull of a build toolchain into a shipped container.
    """
    from yt_dlp.utils import DownloadError

    def hook(d: dict) -> None:
        progress["bytes"] = d.get("downloaded_bytes", 0)

    template = str(into / f"{version_id}.%(ext)s")
    try:
        with _ydl(
            format="bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
            outtmpl=template,
            progress_hooks=[hook],
            postprocessors=[],
        ) as ydl:
            info = ydl.extract_info(url, download=True)
            return pathlib.Path(ydl.prepare_filename(info))
    except DownloadError as e:
        # The same distinction as `_extract_info`, and it matters more here:
        # a media URL is bound to the IP that resolved it (measured — the
        # signed URL carries `ip=<resolver>` and answers 403 anywhere else), so
        # "refused" is a plausible outcome for reasons that have nothing to do
        # with the video.
        kind = _download_error_kind(str(e))
        raise _fail(
            "audio_unavailable" if kind == "video_unavailable" else kind, str(e)
        ) from e


def _client(settings, service: str):
    import boto3

    return boto3.client(service, region_name=settings.aws.region)


def _find_staged_audio(settings, key_prefix: str):
    """The audio already in S3 for this version, or None."""
    s3 = _client(settings, "s3")
    got = s3.list_objects_v2(Bucket=settings.aws.bucket, Prefix=key_prefix, MaxKeys=2)
    for obj in got.get("Contents", []) or []:
        ext = obj["Key"].rsplit(".", 1)[-1].lower()
        if ext in TRANSCRIBE_FORMATS:
            return f"s3://{settings.aws.bucket}/{obj['Key']}", int(obj["Size"]), ext
    return None


def _upload(settings, path: str, key: str, on_bytes=None) -> None:
    # `upload_file` is multipart and streaming. Nothing is read into memory: the
    # worker is capped at 2 GiB and offered to the OOM killer first.
    #
    # `on_bytes` is boto3's own per-part callback, and it is optional because
    # only one caller needs it: `fetch_audio` counts on the *download* thread
    # and has nothing left to report by the time it gets here, while
    # `stage_audio` does nothing but this — so without it that activity's
    # heartbeat would send a progress figure of 0 for however long a gigabyte
    # takes. A zero that means "not measured" is the one shape this codebase
    # keeps refusing to print.
    _client(settings, "s3").upload_file(
        path, settings.aws.bucket, key, Callback=on_bytes
    )


@activity.defn(name="start_transcription")
async def start_transcription(
    run_id: str,
    probe: VideoProbe,
    audio: AudioStaged,
    tenant_id: str,
    version_id: str,
    language: str,
) -> TranscriptionJob:
    """Start the job, and charge for it **only if this call created it**.

    This is the one place in the codebase where `_charge`'s premise — that a
    retried activity spent its money whether or not the attempt succeeded — is
    false. The job name is derived from the version, so a retry finds the job
    already running and Amazon bills once. Charging on the retry would
    double-report a bill incurred once.
    """
    settings = _aws()
    name = job_name_for(version_id)
    created, job = await asyncio.to_thread(
        _start_job, settings, name, audio, language,
        _key(settings, tenant_id, version_id, ".transcript.json"),
    )
    if created:
        usd = videosource.transcribe_usd(probe.duration_s, settings.aws.usd_per_minute)
        _charge(
            run_id,
            Spend(
                stage="transcription",
                model="aws-transcribe-batch",
                input_tokens=0,
                output_tokens=0,
                usd=usd,
            ),
            provider="aws",
        )
    return job


def _start_job(settings, name: str, audio: AudioStaged, language: str, out_key: str):
    client = _client(settings, "transcribe")
    try:
        got = client.start_transcription_job(
            TranscriptionJobName=name,
            Media={"MediaFileUri": audio.s3_uri},
            MediaFormat=audio.media_format,
            LanguageCode=language,
            # Our own bucket, not Amazon's. The default is a service-managed
            # bucket behind a pre-signed URL that expires, which is useless to a
            # workflow that may not be collected for hours — the host stops
            # nightly and nothing starts it again automatically.
            OutputBucketName=settings.aws.bucket,
            OutputKey=out_key,
        )
        return True, _job(got["TranscriptionJob"])
    except client.exceptions.ConflictException:
        # Already started, by a previous attempt of this same activity. Not a
        # failure, and explicitly not a second charge.
        got = client.get_transcription_job(TranscriptionJobName=name)
        return False, _job(got["TranscriptionJob"])


def _job(raw: dict) -> TranscriptionJob:
    return TranscriptionJob(
        job_name=raw["TranscriptionJobName"],
        status=raw["TranscriptionJobStatus"],
        transcript_uri=(raw.get("Transcript") or {}).get("TranscriptFileUri", ""),
        failure_reason=raw.get("FailureReason", ""),
        language=raw.get("LanguageCode", ""),
    )


@activity.defn(name="poll_transcription")
async def poll_transcription(job_name: str) -> TranscriptionJob:
    """One look at the job. Deliberately single-shot and short.

    The waiting is a `workflow.sleep` next door. An activity that polled in a
    loop would be the thing in flight when the host stops at 23:00, and Temporal
    would retry it from the top — re-downloading and re-uploading audio that is
    already in S3, or starting a second job. A timer is server-side state and
    survives the stop; a running activity is not.
    """
    settings = _aws()
    got = await asyncio.to_thread(
        lambda: _client(settings, "transcribe").get_transcription_job(
            TranscriptionJobName=job_name
        )
    )
    return _job(got["TranscriptionJob"])


@activity.defn(name="collect_transcript")
async def collect_transcript(
    run_id: str, job: TranscriptionJob, tenant_id: str, version_id: str
) -> ArtifactRef:
    """Read the finished transcript out of S3 into the run's artifacts."""
    settings = _aws()
    key = _key(settings, tenant_id, version_id, ".transcript.json")
    body = await asyncio.to_thread(
        lambda: _client(settings, "s3")
        .get_object(Bucket=settings.aws.bucket, Key=key)["Body"]
        .read()
    )
    store = ArtifactStore(settings.workspace, run_id)
    return _record(
        run_id, "transcription_result", store.write_bytes("transcription_result", body)
    )


@activity.defn(name="abandon_transcription")
async def abandon_transcription(job_name: str) -> None:
    """Give up on a job, best-effort.

    Deleting matters because the name is derived from the version and Amazon
    keeps it for 90 days — so a job left in a failed state would make every
    later attempt on that version a `ConflictException` reporting the old
    failure. Best-effort because a run that already failed must not fail
    differently because the cleanup could not be reached.
    """
    try:
        settings = _aws()
        await asyncio.to_thread(
            lambda: _client(settings, "transcribe").delete_transcription_job(
                TranscriptionJobName=job_name
            )
        )
    except Exception as e:  # noqa: BLE001 - cleanup must not raise
        log.warning("could not delete transcription job %s: %s", job_name, e)


# --- chunking, where the clock reaches the rows ------------------------------


@activity.defn(name="chunk_transcript")
async def chunk_transcript(
    run_id: str,
    text_ref: ArtifactRef,
    cues_ref: ArtifactRef,
    fallback_ref: ArtifactRef | None = None,
) -> Chunked:
    """Chunk the transcript and stamp each chunk with when it was said.

    Both artifacts are read **through their refs**, whose hashes are verified —
    unlike `chunk_final`, which opens the corrected text by path. Here that
    matters: the text and the cue table have to be a matched pair, and a stale
    one of either produces confident timestamps pointing at the wrong moment.

    ``fallback_ref`` is the uncorrected stream. If correction moved the
    paragraph count despite the collapse in `correct_text`, this indexes that
    instead of failing, and says so. A wrong timestamp is worse than a missing
    correction: it is an unverifiable citation that looks verifiable, and the
    only way a reader finds out is by clicking it.
    """
    from docagent import transcript as dt
    from docagent.chunk import build_chunks, split_paragraphs

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)

    table = [
        videosource.ParagraphTime(r["para"], r["start_s"], r["end_s"])
        for r in store.read_json(cues_ref)["paragraphs"]
    ]
    data = store.read_bytes(text_ref)
    paras = split_paragraphs(data)
    warnings: list[str] = []

    if len(paras) != len(table) and fallback_ref is not None:
        warnings.append(
            "la corrección desalineó los párrafos; se indexó el transcript sin "
            "corregir para no mover las marcas de tiempo"
        )
        log.warning(
            "run %s: corrected transcript has %d paragraphs against %d timed "
            "groups; falling back to the uncorrected stream",
            run_id, len(paras), len(table),
        )
        data = store.read_bytes(fallback_ref)
        paras = split_paragraphs(data)

    # Raises rather than guessing. By here there is no stream left to fall back
    # to, and every timestamp derived from a shifted table is wrong.
    videosource.assert_aligned([p.text for p in paras], table)

    chunks = build_chunks(data, paras, dt.chunk_rules(), dt.classify)
    rows = []
    for c in chunks:
        start_s, end_s = videosource.span_for(c.para_from, c.para_to, table)
        rows.append(indexing_chunk_row(c, start_s=start_s, end_s=end_s))

    if lost := videosource.uncovered_paragraphs(chunks, table):
        # Not fatal, and not silent either. `build_chunks` discards a heading's
        # own text and a too-short leading run, both without an error;
        # `chunk_rules` is meant to make the first unreachable and the grouper's
        # merge rule the second, so anything here is one of them getting through.
        warnings.append(
            f"{len(lost)} párrafo(s) del transcript no llegaron a ningún fragmento"
        )
        log.warning("run %s: paragraphs reached no chunk: %s", run_id, lost[:20])

    ref = _record(run_id, "chunks", store.write_jsonl("chunks", rows))
    kinds: dict[str, int] = {}
    for c in chunks:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    return Chunked(
        chunks=ref,
        count=len(rows),
        kinds=[ChunkKindCount(k, n) for k, n in sorted(kinds.items())],
    )
