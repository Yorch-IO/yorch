"""A channel's catalogue, from the YouTube Data API v3.

Why this and not yt-dlp, which this repository already bundles and already
drives: **the call that is bot-checked is the player API, not the network.**
Measured 2026-09-05 from EC2, `yt-dlp extract_info` is refused there while every
signed URL is served (`doc/VIDEO.md` has the table). The Data API is a keyed
REST endpoint and is refused nowhere, so a catalogue fetched through it works
from the worker wherever the worker runs — which is the property that lets the
channel screen work without the desktop-side fetch split that a *video* needs.

It is also the only source that carries a video's **description**, and the
description is half of what the preselection pass reads.

Three things this module does that `youtube_explorer.py` — the script it
replaces — did not:

* **No `search.list` fallback when a handle does not resolve.** That call costs
  100 quota units against this module's 1, and it answers with the channel
  Google thinks you meant. A channel indexed under the name of a different one
  fails nowhere at all: every video in it is real, every transcript is real, and
  the corpus is simply not what it says it is. That is the same shape of damage
  `videosource.check_resolved` exists to refuse, so it is refused here too — an
  unresolvable handle is an error with a message, never a guess.
* **`videos.list` for the duration.** `playlistItems` does not carry one, which
  is why that script's CSV has an empty duration column for this provider. The
  duration is not a nicety: it is what `videosource.projected_characters` turns
  into a character count, and therefore what the cost of reading a channel is
  quoted from before anything is downloaded.
* **`contentDetails.videoPublishedAt`, not `snippet.publishedAt`.** On a
  playlist item the second one is when the video was *added to the playlist*.
  For an uploads playlist the two usually agree and are not contracted to.

Quota, because it is the one resource here that runs out: the default project
allowance is 10,000 units a day. `channels.list` is 1, `playlistItems.list` is 1
per page of 50, and `videos.list` is 1 per 50 ids — so a 2,000-video channel
costs about 81 units to catalogue completely. :attr:`Client.units` reports what
a call actually spent, so a caller can say so rather than discover it.

Stdlib only. `worker/pyproject.toml` keeps a "no compiler in the image"
property, and a JSON-over-HTTPS client does not need a dependency to have one.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Iterator, Sequence

from .videosource import HOSTS

API_BASE = "https://www.googleapis.com/youtube/v3"

#: The API's own maximum for both list calls, and therefore the page size that
#: costs the fewest quota units per video.
PAGE_SIZE = 50

#: How long a single request may take. Generous rather than tight: a page of 50
#: is one round trip and a channel sync is not something anybody is watching a
#: spinner for.
TIMEOUT_S = 30.0

#: `P3DT4H5M6S` and every shorter form of it, including the bare `PT0S` a
#: premiere that has not aired reports.
_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

#: A channel id is `UC` and 22 characters of URL-safe base64. Pinned for the
#: reason `videosource._ID_RE` is: this string becomes a directory name and a
#: library id, so a lax pattern lets a path segment be built from an unchecked
#: one.
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

#: What YouTube accepts as a handle: letters, digits, dot, dash, underscore.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{3,30}$")

#: Titles `playlistItems` returns for an entry whose video is no longer
#: readable. They are not errors and not gaps — the id is still in the playlist
#: and the video is not fetchable — so they are dropped rather than reported.
_UNAVAILABLE_TITLES = frozenset({"Private video", "Deleted video"})


class NotAChannelUrl(ValueError):
    """A string that is not a link to a YouTube channel."""


class YouTubeError(RuntimeError):
    """A Data API failure, classified so the UI can offer a fix.

    `kind` is the machine-readable half and the app's guidance map keys on
    exactly these strings, so adding one here means adding advice there.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ChannelLookup:
    """Which of the three ways of naming a channel a URL used.

    Separate from the request so the parsing can be tested without a key and
    without a network, which is where every shape this has to accept lives.
    """

    #: ``id``, ``handle`` or ``username``.
    kind: str
    value: str


@dataclass(frozen=True)
class ChannelRef:
    """A channel, resolved. Everything here is stable except the title."""

    channel_id: str
    title: str
    handle: str
    description: str
    uploads_playlist_id: str
    url: str


@dataclass(frozen=True)
class ChannelVideo:
    """One video's metadata, which is a hypothesis about its content.

    Nothing here is evidence of what a video *says*. The preselection pass reads
    `title` and `description` and is contracted to treat them as a probability,
    and the topic pass exists because that is not enough. Keeping the two
    separate is the whole reason this dataclass does not carry a relevance.
    """

    video_id: str
    title: str
    description: str
    #: ISO 8601, from `contentDetails.videoPublishedAt` — when the video was
    #: published, not when it entered the playlist.
    published_at: str
    #: Seconds. ``0`` means "not hydrated yet" and is why :meth:`Client.hydrate`
    #: is a separate step rather than something a caller can forget to notice:
    #: a zero here would project a zero-cost transcription.
    duration_s: int = 0
    #: ``none``, ``live`` or ``upcoming``. A live or upcoming video is refused by
    #: `resolve_video` anyway; knowing here saves the probe.
    live_state: str = "none"
    thumbnail: str = ""
    #: ``False`` once a *complete* re-sync walked the whole uploads playlist
    #: and did not meet this id — deleted, or made private. Kept rather than
    #: dropped, because the video may already be indexed and the screen must
    #: still be able to say so. Appended and defaulted, so a catalogue written
    #: before the field existed reads every video as available.
    available: bool = True

    @property
    def url(self) -> str:
        return f"https://youtu.be/{self.video_id}"

    @property
    def hydrated(self) -> bool:
        return self.duration_s > 0 or self.live_state == "upcoming"


# --- pure parsing ------------------------------------------------------------


def channel_lookup(raw: str) -> ChannelLookup:
    """How to ask the API about the channel this string names.

    Accepts a bare ``@handle``, a bare handle, and the four URL shapes YouTube
    serves: ``/channel/UC…``, ``/@handle``, ``/c/Name`` and ``/user/Name``. The
    host allowlist is `videosource.HOSTS`, shared rather than copied.

    A URL that names a *video* is refused here rather than silently treated as
    its channel: "index this channel" and "index this video" are different
    requests and the app has a screen for each.
    """
    text = (raw or "").strip()
    if not text:
        raise NotAChannelUrl("empty")

    if "/" not in text and "." not in text.split("?")[0]:
        # A bare handle, with or without its at-sign.
        candidate = text.lstrip("@")
        if _CHANNEL_ID_RE.match(candidate):
            return ChannelLookup("id", candidate)
        if _HANDLE_RE.match(candidate):
            return ChannelLookup("handle", candidate)
        raise NotAChannelUrl(f"not a channel handle: {raw!r}")

    url = text if "//" in text else "https://" + text
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if host not in HOSTS:
        raise NotAChannelUrl(f"not a YouTube host: {parsed.netloc!r}")

    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise NotAChannelUrl(f"no channel in {raw!r}")

    head = parts[0]
    if head.startswith("@"):
        return _lookup_from(head[1:], raw)
    if head == "channel" and len(parts) > 1:
        if not _CHANNEL_ID_RE.match(parts[1]):
            raise NotAChannelUrl(f"not a channel id: {parts[1]!r}")
        return ChannelLookup("id", parts[1])
    if head == "user" and len(parts) > 1:
        return ChannelLookup("username", parts[1])
    if head == "c" and len(parts) > 1:
        return _lookup_from(parts[1], raw)
    raise NotAChannelUrl(f"no channel in {raw!r}")


def _lookup_from(value: str, raw: str) -> ChannelLookup:
    if _CHANNEL_ID_RE.match(value):
        return ChannelLookup("id", value)
    if _HANDLE_RE.match(value):
        return ChannelLookup("handle", value)
    raise NotAChannelUrl(f"not a channel handle: {raw!r}")


def parse_iso8601_duration(text: str) -> int:
    """``PT1H16M13S`` -> ``4573``. Whole seconds, and ``0`` for anything unparseable.

    Zero rather than an exception because the caller's alternative is dropping a
    video from the catalogue over a field it may not need — and `ChannelVideo`
    already treats zero as "not known", which surfaces as a video that cannot be
    quoted rather than one quoted at nothing.
    """
    m = _DURATION_RE.match((text or "").strip())
    if not m:
        return 0
    days, hours, minutes, seconds = (
        int(m.group(name) or 0) for name in ("days", "hours", "minutes", "seconds")
    )
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def channel_url(ref: ChannelRef) -> str:
    """The canonical link for a channel: its handle when it has one."""
    if ref.handle:
        return f"https://www.youtube.com/@{ref.handle.lstrip('@')}"
    return f"https://www.youtube.com/channel/{ref.channel_id}"


def merge_catalogue(
    known: Sequence[ChannelVideo], fetched: Sequence[ChannelVideo]
) -> list[ChannelVideo]:
    """Known videos updated by a fetch, with nothing dropped for being absent.

    A sync may be partial — a `limit`, a quota that ran out mid-page, a network
    that failed on page four — and a partial answer is not a statement that the
    videos it does not mention are gone. So this is a union keyed on the video
    id, newest-first by publication, and a fetched row wins the fields it
    carries. **A hydrated duration is never overwritten by an unhydrated zero**,
    which is what lets `list_uploads` and `hydrate` be two calls without the
    second one having to happen before the first is stored.
    """
    by_id: dict[str, ChannelVideo] = {v.video_id: v for v in known}
    for video in fetched:
        current = by_id.get(video.video_id)
        if current is None:
            by_id[video.video_id] = video
            continue
        merged = video
        if not video.duration_s and current.duration_s:
            merged = replace(merged, duration_s=current.duration_s)
        if video.live_state == "none" and current.live_state != "none":
            merged = replace(merged, live_state=current.live_state)
        if not video.description and current.description:
            merged = replace(merged, description=current.description)
        # A fetched row was just seen on the playlist, so it is available
        # whatever the store said: `_video_of` builds every row that way and
        # nothing here has to undo a mark. The mark is only ever *set* by
        # `channel.sync` after a complete walk, on rows the walk did not meet.
        by_id[video.video_id] = merged
    return sorted(by_id.values(), key=lambda v: (v.published_at, v.video_id), reverse=True)


# --- the API -----------------------------------------------------------------


def _open(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "company-brain/1.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return response.read()


@dataclass
class Client:
    """One API key and the quota it has spent.

    `open_url` is injected so the tests can drive every shape of response and
    every error this has to classify without a network. It is the only I/O in
    the module.
    """

    api_key: str
    open_url: Callable[[str], bytes] = _open
    #: Quota units spent by this instance, so a route can report what a sync
    #: cost rather than leaving it to be discovered when the day's allowance is
    #: gone.
    units: int = field(default=0)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise YouTubeError(
                "No hay clave de la YouTube Data API configurada.",
                kind="youtube_key_missing",
            )

    # -- requests

    def _get(self, path: str, params: dict[str, str], *, cost: int = 1) -> dict[str, Any]:
        query = urllib.parse.urlencode({**params, "key": self.api_key})
        url = f"{API_BASE}/{path}?{query}"
        self.units += cost
        try:
            raw = self.open_url(url)
        except urllib.error.HTTPError as e:  # noqa: PERF203 - one classifier
            raise self._classify(e) from e
        except urllib.error.URLError as e:
            raise YouTubeError(
                f"No se pudo consultar la API de YouTube: {e.reason}",
                kind="youtube_unreachable",
            ) from e
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise YouTubeError(
                "La API de YouTube devolvió algo que no es JSON.",
                kind="youtube_unreadable",
            ) from e

    @staticmethod
    def _classify(e: urllib.error.HTTPError) -> YouTubeError:
        """An HTTP failure, read for the reason Google puts in the body.

        The status alone cannot tell the two 403s apart, and they need opposite
        advice: a key that is not authorised for this API is fixed in the Cloud
        console once, and a quota that ran out is fixed by waiting until
        midnight Pacific. Telling a person to check their key when the key is
        fine is the kind of wrong advice that costs an afternoon.
        """
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the status still has to be reported
            pass
        reason, message = "", ""
        try:
            error = json.loads(body).get("error", {})
            message = str(error.get("message") or "")
            errors = error.get("errors") or []
            if errors:
                reason = str(errors[0].get("reason") or "")
        except Exception:  # noqa: BLE001
            pass

        if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            return YouTubeError(
                "Se agotó la cuota diaria de la YouTube Data API. "
                f"{message}".strip(),
                kind="youtube_quota_exceeded",
            )
        if e.code in (400, 401, 403):
            return YouTubeError(
                f"La YouTube Data API rechazó la clave o la petición: {message or e.reason}",
                kind="youtube_refused",
            )
        return YouTubeError(
            f"La YouTube Data API respondió {e.code}: {message or e.reason}",
            kind="youtube_unreadable",
        )

    # -- calls

    def resolve_channel(self, lookup: ChannelLookup) -> ChannelRef:
        """The channel this lookup names, or an error. Never a guess — see the
        module docstring on why there is no `search.list` fallback."""
        param = {"id": "id", "handle": "forHandle", "username": "forUsername"}[lookup.kind]
        value = lookup.value if lookup.kind != "handle" else f"@{lookup.value}"
        data = self._get(
            "channels", {"part": "snippet,contentDetails", param: value}
        )
        items = data.get("items") or []
        if not items and lookup.kind == "handle":
            # A pre-handle channel whose custom URL survived as a legacy
            # username. One more unit, and still not a search.
            data = self._get(
                "channels",
                {"part": "snippet,contentDetails", "forUsername": lookup.value},
            )
            items = data.get("items") or []
        if not items:
            raise YouTubeError(
                f"No existe un canal de YouTube llamado {lookup.value!r}.",
                kind="channel_not_found",
            )

        item = items[0]
        snippet = item.get("snippet") or {}
        uploads = (
            ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get(
                "uploads"
            )
            or ""
        )
        if not uploads:
            raise YouTubeError(
                "El canal no publica una lista de subidas, así que no se puede "
                "recorrer.",
                kind="channel_not_found",
            )
        ref = ChannelRef(
            channel_id=str(item.get("id") or ""),
            title=str(snippet.get("title") or ""),
            handle=str(snippet.get("customUrl") or "").lstrip("@"),
            description=str(snippet.get("description") or ""),
            uploads_playlist_id=str(uploads),
            url="",
        )
        return replace(ref, url=channel_url(ref))

    def iter_uploads(
        self, uploads_playlist_id: str, limit: int | None = None
    ) -> Iterator[list[ChannelVideo]]:
        """A channel's uploads, newest first, one page at a time, without durations.

        A generator rather than a list so the caller can act between pages —
        save what arrived, decide whether the rest is already known, stop. That
        is what lets a sync of a channel with thousands of videos keep every
        page it fetched when the quota runs out on the next one, instead of
        losing all of them to one exception at the end.

        `limit` is "the most recent N" and `None` is the whole playlist.
        Unavailable entries are dropped rather than counted against the limit: a
        channel with six private videos in its first page should still yield
        fifty. The last page may run past `limit`; `list_uploads` trims it and a
        caller that pages itself trims its own.
        """
        got = 0
        token = ""
        while limit is None or got < limit:
            wanted = PAGE_SIZE if limit is None else min(PAGE_SIZE, max(1, limit - got))
            params = {
                "part": "snippet,contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": str(wanted),
            }
            if token:
                params["pageToken"] = token
            data = self._get("playlistItems", params)
            items = data.get("items") or []
            if not items:
                break
            page = [v for v in (_video_of(item) for item in items) if v is not None]
            got += len(page)
            yield page
            token = str(data.get("nextPageToken") or "")
            if not token:
                break

    def list_uploads(
        self, uploads_playlist_id: str, limit: int | None = None
    ) -> list[ChannelVideo]:
        """Up to `limit` of a channel's uploads as one list. See `iter_uploads`."""
        out: list[ChannelVideo] = []
        for page in self.iter_uploads(uploads_playlist_id, limit):
            out.extend(page)
        return out if limit is None else out[:limit]

    def hydrate(self, videos: Sequence[ChannelVideo]) -> list[ChannelVideo]:
        """The same videos with their duration and live state filled in.

        One unit per 50, and the ids that come back are matched by id rather
        than by position: `videos.list` omits an id it cannot serve instead of
        returning a hole, so zipping the two lists would shift every duration
        after the first missing one onto the wrong video.
        """
        by_id = {v.video_id: v for v in videos}
        for batch in _batched([v.video_id for v in videos], PAGE_SIZE):
            data = self._get(
                "videos", {"part": "contentDetails,snippet", "id": ",".join(batch)}
            )
            for item in data.get("items") or []:
                vid = str(item.get("id") or "")
                current = by_id.get(vid)
                if current is None:
                    continue
                details = item.get("contentDetails") or {}
                snippet = item.get("snippet") or {}
                by_id[vid] = replace(
                    current,
                    duration_s=parse_iso8601_duration(str(details.get("duration") or "")),
                    live_state=str(snippet.get("liveBroadcastContent") or "none"),
                    description=str(snippet.get("description") or current.description),
                )
        return [by_id[v.video_id] for v in videos]


def _video_of(item: dict[str, Any]) -> ChannelVideo | None:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    vid = str(details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId") or "")
    if not vid:
        return None
    if str(snippet.get("title") or "") in _UNAVAILABLE_TITLES:
        return None
    thumbs = snippet.get("thumbnails") or {}
    best = thumbs.get("medium") or thumbs.get("default") or {}
    return ChannelVideo(
        video_id=vid,
        title=str(snippet.get("title") or ""),
        description=str(snippet.get("description") or ""),
        published_at=str(
            details.get("videoPublishedAt") or snippet.get("publishedAt") or ""
        ),
        thumbnail=str(best.get("url") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"),
    )


def _batched(items: Sequence[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])
