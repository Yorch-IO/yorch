"""Cataloguing a channel of any size without losing what was fetched.

The first version fetched the whole playlist, then hydrated the whole list,
then saved — three steps in one request, and an exception in the second lost
the first. With a cap of 500 videos that was about twenty calls and nobody
noticed. Without a cap, a channel of five thousand videos is two hundred calls
in one request, and a quota that runs out on the hundredth would have thrown
away the ninety-nine before it. So the loop lives here and does four things a
page at a time:

- **Save after every page.** The catalogue on disk is always what has been
  fetched so far, and the exception that ends a sync leaves it that way.
  `merge_catalogue` is a union, so a partial walk never drops anything.
- **Hydrate only what is new.** A known video's duration is already paid for
  and `merge_catalogue` never overwrites a hydrated duration with an unhydrated
  zero, so the `videos.list` call is made for the new ids of each page and no
  others. On a channel that has not uploaded since the last sync that is zero
  calls.
- **Stop at the first page that is entirely known — but only when the catalogue
  is complete.** The playlist is newest-first, so once a page holds no new id,
  every later page is known too. The condition is the whole point: a catalogue
  written under the old 500-video cap knows its first ten pages perfectly and
  nothing after them, and stopping there would leave it capped for ever. Until
  a walk has reached the end once, an incremental sync keeps going.
- **Mark absences only after a complete walk, and only when asked.** A `full`
  sync ignores the early stop, walks to the end, and marks every known id it
  did not meet as unavailable — deleted or made private. A walk cut short by
  a limit or an error marks nothing, because a partial answer is not a
  statement about the videos it did not mention. Nothing is ever dropped: the
  video may already be indexed, and the screen must still be able to say so.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from ..channelstore import ChannelStore, StoredChannel
from ..youtube import ChannelRef, ChannelVideo, Client, merge_catalogue


@dataclass(frozen=True)
class SyncReport:
    """What a sync did, for the route to say and the screen to show."""

    channel: StoredChannel
    #: Videos the walk met on the playlist.
    fetched: int
    #: Of those, the ones the catalogue did not hold before.
    added: int
    #: `videos.list` rows filled in this sync — the new ones, never the known.
    hydrated: int
    pages: int
    #: The walk reached the end of the playlist, or stopped early on a
    #: catalogue that had already reached it once.
    complete: bool
    #: Stopped at the first fully-known page. Never on a full sync.
    stopped_early: bool
    #: Known videos a full walk did not meet, marked rather than dropped.
    unavailable: int
    #: Quota units this sync spent.
    units: int


def sync_channel(
    store: ChannelStore,
    client: Client,
    ref: ChannelRef,
    *,
    limit: int | None = None,
    hydrate: bool = True,
    full: bool = False,
    now: datetime | None = None,
) -> SyncReport:
    """Walk the uploads playlist, saving as it goes. See the module docstring.

    Raises whatever the client raises; by then every page before the failure
    is on disk, and the channel file says the catalogue is incomplete.
    """
    known = {v.video_id: v for v in store.videos(ref.channel_id)}
    before = store.read(ref.channel_id)
    was_complete = before is not None and before.complete
    may_stop = not full and was_complete

    current: list[ChannelVideo] = list(known.values())
    seen: set[str] = set()
    fetched = added = hydrated = pages = 0
    stopped_early = False
    reached_end = False

    walk = client.iter_uploads(ref.uploads_playlist_id, limit)
    for page in walk:
        pages += 1
        fetched += len(page)
        seen.update(v.video_id for v in page)
        new = [v for v in page if v.video_id not in known]
        added += len(new)
        if hydrate and new:
            new = client.hydrate(new)
            hydrated += len(new)
        by_id = {v.video_id: v for v in new}
        page = [by_id.get(v.video_id, v) for v in page]
        current = merge_catalogue(current, page)
        for v in new:
            known[v.video_id] = v
        # Written before the stop decision, so a page that turns out to be the
        # last is on disk the same as any other.
        store.write(
            ref, current, units_spent=client.units, now=now,
            complete=was_complete and not full,
        )
        if may_stop and not new:
            stopped_early = True
            break
    else:
        # The generator ran out: either the playlist ended or `limit` cut it.
        reached_end = limit is None or fetched < limit

    complete = reached_end or (stopped_early and was_complete)

    unavailable = 0
    if full and reached_end:
        marked: list[ChannelVideo] = []
        for v in current:
            if v.video_id in seen:
                marked.append(v if v.available else replace(v, available=True))
            else:
                unavailable += 1 if v.available else 0
                marked.append(replace(v, available=False))
        current = marked

    stored = store.write(
        ref, current, units_spent=client.units, now=now, complete=complete
    )
    return SyncReport(
        channel=stored,
        fetched=fetched,
        added=added,
        hydrated=hydrated,
        pages=pages,
        complete=complete,
        stopped_early=stopped_early,
        unavailable=unavailable,
        units=client.units,
    )
