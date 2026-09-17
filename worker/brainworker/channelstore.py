"""What a channel holds, kept on the workspace rather than in the catalogue.

**Why this is not a table.** The catalogue of a channel is *derived data*: it is
refetchable for one quota unit per fifty videos, it changes whenever the channel
does, and nothing downstream joins against it. Putting it in Postgres would buy
a migration, three models on two planes and a second place for a video's title
to live, in exchange for nothing a JSON file on the volume does not already do.

**What is deliberately absent from it: whether a video is indexed.** That is
Postgres' answer, through `document.source_key = youtube/<id>` inside the
channel's library, and it stays the only answer. The original plan for this
feature proposed an `index-state.json` beside the catalogue; a second record of
one fact is a second record that can disagree with the first without anything
failing, which is the mistake root `CLAUDE.md` records about sourcing the
overview's books from the catalogue and its edges from the graph.

**A channel is a library.** `retrieve.search` narrows by equality on
`library_id`, `document_id` or `version_id` and the graph expansion narrows by
library alone, so "ask only this channel" *is* "ask this channel's library" and
no other arrangement satisfies it. :func:`library_id_for` is where that mapping
lives, once.

Layout, under `Paths.for_tenant(t).channels`:

```
<channel_id>/channel.json    the channel, when it was synced, what it cost
<channel_id>/videos.json     every video known about it
```

Two files rather than one because listing channels must not parse two thousand
videos to render a picker.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .youtube import ChannelRef, ChannelVideo, _CHANNEL_ID_RE, merge_catalogue

CHANNEL_FILE = "channel.json"
VIDEOS_FILE = "videos.json"

#: Bumped when the shape on disk changes in a way a reader cannot absorb. A file
#: from the future is left alone and reported rather than parsed optimistically:
#: it is somebody's record, and the catalogue can always be refetched.
SCHEMA = 1


class UnusableChannelId(ValueError):
    """A channel id that must not become a path segment."""


@dataclass(frozen=True)
class StoredChannel:
    """A channel as the store holds it."""

    channel: ChannelRef
    #: ISO 8601, UTC. When the catalogue was last written.
    synced_at: str
    #: How many videos `videos.json` holds.
    video_count: int
    #: Quota units the last sync spent, so a caller can report it rather than
    #: discover the day's allowance is gone.
    units_spent: int = 0
    library_id: str = ""
    schema: int = SCHEMA
    #: Whether a sync has walked the uploads playlist to its end. Until it has,
    #: the catalogue is "the most recent N" and an incremental sync must not
    #: stop at the first page it already knows — every catalogue written under
    #: the old 500-video cap is exactly that case, and a file from before the
    #: field reads `False`, so the next sync walks to the end once.
    complete: bool = False


def library_id_for(channel_id: str) -> str:
    """The library a channel's videos are indexed into.

    The channel id verbatim, **not lowercased**: a YouTube channel id is
    case-sensitive URL-safe base64, so folding the case would let two different
    channels land in one library — which is a merge nothing downstream could see,
    because every document in it would be real.
    """
    return f"lib_yt_{_checked(channel_id)}"


def _checked(channel_id: str) -> str:
    """The id, or a refusal. A path segment built from an unchecked string is how
    a workspace gets escaped, so this runs before the id is ever joined to a
    path — the same rule, and the same reason, as `Paths.run_dir`."""
    if not _CHANNEL_ID_RE.match(channel_id or ""):
        raise UnusableChannelId(f"not a YouTube channel id: {channel_id!r}")
    return channel_id


@dataclass(frozen=True)
class ChannelStore:
    """The catalogues under one organisation's workspace."""

    root: pathlib.Path

    def dir_for(self, channel_id: str) -> pathlib.Path:
        return self.root / _checked(channel_id)

    # -- reading

    def list(self) -> list[StoredChannel]:
        """Every channel this organisation has synced, most recent first.

        Reads only `channel.json`, never the videos. A directory whose file is
        missing or unreadable is skipped rather than raising: one corrupt
        catalogue must not take the picker down with it, and the remedy —
        syncing again — is free.
        """
        if not self.root.is_dir():
            return []
        out: list[StoredChannel] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            stored = self._read_channel(entry / CHANNEL_FILE)
            if stored is not None:
                out.append(stored)
        return sorted(out, key=lambda s: s.synced_at, reverse=True)

    def read(self, channel_id: str) -> StoredChannel | None:
        return self._read_channel(self.dir_for(channel_id) / CHANNEL_FILE)

    def videos(self, channel_id: str) -> list[ChannelVideo]:
        path = self.dir_for(channel_id) / VIDEOS_FILE
        payload = _load(path)
        if payload is None:
            return []
        rows = payload.get("videos") or []
        return [_video_of(row) for row in rows if isinstance(row, dict)]

    # -- writing

    def save(
        self,
        ref: ChannelRef,
        videos: list[ChannelVideo],
        *,
        units_spent: int = 0,
        now: datetime | None = None,
        complete: bool | None = None,
    ) -> StoredChannel:
        """Merge a fetch into what is already known and write both files.

        The merge is `youtube.merge_catalogue`, which is a union rather than a
        replacement: a sync is allowed to be partial — a limit, a quota that ran
        out mid-page — and a partial answer is not a statement that the videos it
        did not mention have gone.

        `complete` left as `None` keeps what the channel already recorded: a
        partial fetch into a complete catalogue does not make it incomplete.
        """
        merged = merge_catalogue(self.videos(ref.channel_id), videos)
        if complete is None:
            current = self.read(ref.channel_id)
            complete = current.complete if current is not None else False
        return self.write(
            ref, merged, units_spent=units_spent, now=now, complete=complete
        )

    def write(
        self,
        ref: ChannelRef,
        merged: list[ChannelVideo],
        *,
        units_spent: int = 0,
        now: datetime | None = None,
        complete: bool = False,
    ) -> StoredChannel:
        """Write an already-merged catalogue and its channel file.

        The page-by-page sync calls this once per page with the list it is
        accumulating in memory, rather than `save`, which would re-read and
        re-parse the whole file for every fifty videos — on a channel of five
        thousand that is a hundred parses of a three-megabyte file for nothing.
        """
        directory = self.dir_for(ref.channel_id)
        directory.mkdir(parents=True, exist_ok=True)
        _dump(
            directory / VIDEOS_FILE,
            {"schema": SCHEMA, "channel_id": ref.channel_id,
             "videos": [asdict(v) for v in merged]},
        )
        stored = StoredChannel(
            channel=ref,
            synced_at=(now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            video_count=len(merged),
            units_spent=units_spent,
            library_id=library_id_for(ref.channel_id),
            complete=complete,
        )
        _dump(directory / CHANNEL_FILE, asdict(stored))
        return stored

    # -- internals

    @staticmethod
    def _read_channel(path: pathlib.Path) -> StoredChannel | None:
        payload = _load(path)
        if payload is None:
            return None
        channel = payload.get("channel")
        if not isinstance(channel, dict):
            return None
        try:
            return StoredChannel(
                channel=ChannelRef(**{
                    k: channel.get(k, "") for k in
                    ("channel_id", "title", "handle", "description",
                     "uploads_playlist_id", "url")
                }),
                synced_at=str(payload.get("synced_at") or ""),
                video_count=int(payload.get("video_count") or 0),
                units_spent=int(payload.get("units_spent") or 0),
                library_id=str(payload.get("library_id") or ""),
                schema=int(payload.get("schema") or SCHEMA),
                complete=bool(payload.get("complete", False)),
            )
        except (TypeError, ValueError):
            return None


def _video_of(row: dict) -> ChannelVideo:
    return ChannelVideo(
        video_id=str(row.get("video_id") or ""),
        title=str(row.get("title") or ""),
        description=str(row.get("description") or ""),
        published_at=str(row.get("published_at") or ""),
        duration_s=int(row.get("duration_s") or 0),
        live_state=str(row.get("live_state") or "none"),
        thumbnail=str(row.get("thumbnail") or ""),
        available=bool(row.get("available", True)),
    )


def _load(path: pathlib.Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _dump(path: pathlib.Path, payload: dict) -> None:
    # Write to a sibling and rename, for the reason `ArtifactStore.write_bytes`
    # gives: a process killed mid-write would otherwise leave a truncated file
    # that parses as far as it goes.
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
