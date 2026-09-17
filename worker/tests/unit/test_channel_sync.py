"""Cataloguing a channel of any size: page by page, nothing lost, nothing
re-bought.

`tmp_path` for the store and a scripted `open_url` for the API — the same
double `test_youtube.py` uses — so every page, every failure and every quota
unit here is a number the test set up. No network.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from brainworker import channelstore as cs
from brainworker import youtube as y
from brainworker.channel import sync as chsync
from brainworker.youtube import ChannelRef

CHANNEL = "UCabcdefghijklmnopqrstuv"


def _ref() -> ChannelRef:
    return ChannelRef(
        channel_id=CHANNEL,
        title="Casa Sobre la Roca",
        handle="casarocachannel",
        description="",
        uploads_playlist_id="UUabc",
        url="https://www.youtube.com/@casarocachannel",
    )


def _item(vid: str) -> dict:
    return {
        "snippet": {"title": f"Video {vid}", "description": "d", "resourceId": {"videoId": vid}},
        "contentDetails": {"videoId": vid, "videoPublishedAt": "2026-01-01T00:00:00Z"},
    }


def _uploads(ids: list[str], next_page: str = "") -> dict:
    page: dict = {"items": [_item(v) for v in ids]}
    if next_page:
        page["nextPageToken"] = next_page
    return page


def _durations(ids: list[str]) -> dict:
    return {
        "items": [
            {"id": v, "contentDetails": {"duration": "PT45M"}, "snippet": {"liveBroadcastContent": "none"}}
            for v in ids
        ]
    }


QUOTA = {"__raise__": "quotaExceeded"}


class Fake:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if not self.pages:
            raise AssertionError(f"unscripted request: {url}")
        page = self.pages.pop(0)
        if "__raise__" in page:
            body = json.dumps({"error": {"errors": [{"reason": page["__raise__"]}]}}).encode()
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(body))
        return json.dumps(page).encode("utf-8")

    @property
    def hydrations(self) -> int:
        return sum(1 for u in self.urls if "/videos?" in u)

    @property
    def listings(self) -> int:
        return sum(1 for u in self.urls if "/playlistItems?" in u)


def _client(pages: list[dict]) -> tuple[y.Client, Fake]:
    fake = Fake(pages)
    return y.Client(api_key="k", open_url=fake), fake


@pytest.fixture
def store(tmp_path) -> cs.ChannelStore:
    return cs.ChannelStore(tmp_path / "channels")


A, B, C = ["a" * 11], ["b" * 11], ["c" * 11]


# --- page by page ------------------------------------------------------------


def test_every_page_is_on_disk_before_the_next_is_asked_for(store):
    # The reason the loop lives in its own module. A failure on page three
    # leaves pages one and two exactly as a success would have.
    client, _ = _client([_uploads(A, "p2"), _durations(A), _uploads(B, "p3"), _durations(B), QUOTA])
    with pytest.raises(y.YouTubeError) as e:
        chsync.sync_channel(store, client, _ref())
    assert e.value.kind == "youtube_quota_exceeded"
    kept = {v.video_id: v.duration_s for v in store.videos(CHANNEL)}
    assert kept == {A[0]: 2700, B[0]: 2700}
    # And it says so: the next sync must walk on, not stop at what it knows.
    assert store.read(CHANNEL).complete is False


def test_only_new_videos_are_hydrated(store):
    # A known duration is already paid for. On a channel that has not uploaded
    # since the last sync, that is zero `videos.list` calls.
    client, fake = _client([_uploads(A + B), _durations(A + B)])
    chsync.sync_channel(store, client, _ref())
    assert fake.hydrations == 1

    client, fake = _client([_uploads(C + A + B), _durations(C)])
    report = chsync.sync_channel(store, client, _ref())
    assert fake.hydrations == 1
    assert report.added == 1
    assert report.hydrated == 1
    # `_durations(C)` answered for C alone; a request naming A or B would have
    # been a second, unscripted call.
    assert {v.video_id: v.duration_s for v in store.videos(CHANNEL)} == {
        A[0]: 2700, B[0]: 2700, C[0]: 2700,
    }


def test_the_walk_stops_at_the_first_known_page_once_the_catalogue_is_complete(store):
    client, _ = _client([_uploads(A, "p2"), _durations(A), _uploads(B), _durations(B)])
    first = chsync.sync_channel(store, client, _ref())
    assert first.complete is True and first.stopped_early is False

    # Page one is entirely known and there is a page two on offer: not taken.
    client, fake = _client([_uploads(A, "p2")])
    second = chsync.sync_channel(store, client, _ref())
    assert second.stopped_early is True
    assert second.complete is True
    assert fake.listings == 1 and fake.hydrations == 0
    assert second.units == 1


def test_an_incomplete_catalogue_never_stops_early(store):
    # The old 500-video cap left every catalogue in this state: its first
    # pages known perfectly, nothing after them. Stopping at a known page
    # would leave it capped for ever.
    client, _ = _client([_uploads(A, "p2"), _durations(A)])
    chsync.sync_channel(store, client, _ref(), limit=1)
    assert store.read(CHANNEL).complete is False

    client, fake = _client([_uploads(A, "p2"), _uploads(B), _durations(B)])
    report = chsync.sync_channel(store, client, _ref())
    assert report.stopped_early is False
    assert report.complete is True
    assert fake.listings == 2
    assert sorted(v.video_id for v in store.videos(CHANNEL)) == A + B


def test_a_limit_leaves_the_catalogue_incomplete(store):
    client, _ = _client([_uploads(A, "p2"), _durations(A)])
    report = chsync.sync_channel(store, client, _ref(), limit=1)
    assert report.complete is False
    assert store.read(CHANNEL).complete is False


# --- absences ------------------------------------------------------------------


def test_a_full_sync_marks_what_it_did_not_meet_and_drops_nothing(store):
    client, _ = _client([_uploads(A + B), _durations(A + B)])
    chsync.sync_channel(store, client, _ref())

    client, _ = _client([_uploads(A)])
    report = chsync.sync_channel(store, client, _ref(), full=True)
    assert report.unavailable == 1
    assert report.stopped_early is False
    by_id = {v.video_id: v.available for v in store.videos(CHANNEL)}
    assert by_id == {A[0]: True, B[0]: False}


def test_a_video_seen_again_becomes_available_again(store):
    # Made private and then public again — or a full sync that ran against a
    # flaky listing. The playlist is the authority and the mark follows it.
    client, _ = _client([_uploads(A + B), _durations(A + B)])
    chsync.sync_channel(store, client, _ref())
    client, _ = _client([_uploads(A)])
    chsync.sync_channel(store, client, _ref(), full=True)
    client, _ = _client([_uploads(A + B)])
    report = chsync.sync_channel(store, client, _ref(), full=True)
    assert report.unavailable == 0
    assert all(v.available for v in store.videos(CHANNEL))


def test_an_incremental_sync_never_marks_an_absence(store):
    # It stops at the first known page, so it has not looked at the rest, and
    # a partial answer is not a statement about the videos it did not mention.
    client, _ = _client([_uploads(A + B), _durations(A + B)])
    chsync.sync_channel(store, client, _ref())
    client, _ = _client([_uploads(A)])
    report = chsync.sync_channel(store, client, _ref())
    assert report.unavailable == 0
    assert all(v.available for v in store.videos(CHANNEL))


def test_a_full_sync_cut_short_marks_nothing(store):
    # A `limit`, or an error on page two: the walk did not reach the end, so
    # what it did not meet may simply be further down.
    client, _ = _client([_uploads(A + B), _durations(A + B)])
    chsync.sync_channel(store, client, _ref())
    client, _ = _client([_uploads(A, "p2")])
    report = chsync.sync_channel(store, client, _ref(), full=True, limit=1)
    assert report.unavailable == 0
    assert all(v.available for v in store.videos(CHANNEL))

    client, _ = _client([_uploads(A, "p2"), QUOTA])
    with pytest.raises(y.YouTubeError):
        chsync.sync_channel(store, client, _ref(), full=True)
    assert all(v.available for v in store.videos(CHANNEL))
    assert store.read(CHANNEL).complete is False


# --- the store's half ------------------------------------------------------------


def test_a_catalogue_from_before_the_flags_reads_as_incomplete_and_available(tmp_path):
    # Every file written under the 500-video cap. The next sync walks to the
    # end once, and every video in it is offered.
    store = cs.ChannelStore(tmp_path / "channels")
    directory = store.dir_for(CHANNEL)
    directory.mkdir(parents=True)
    (directory / cs.CHANNEL_FILE).write_text(json.dumps({
        "schema": 1,
        "channel": {"channel_id": CHANNEL, "title": "t", "handle": "", "description": "",
                    "uploads_playlist_id": "UUabc", "url": ""},
        "synced_at": "2026-09-16T00:00:00+00:00", "video_count": 1, "units_spent": 3,
        "library_id": f"lib_yt_{CHANNEL}",
    }))
    (directory / cs.VIDEOS_FILE).write_text(json.dumps({
        "schema": 1, "channel_id": CHANNEL,
        "videos": [{"video_id": A[0], "title": "t", "description": "", "published_at": "2026-01-01T00:00:00Z",
                    "duration_s": 600, "live_state": "none", "thumbnail": ""}],
    }))
    assert store.read(CHANNEL).complete is False
    assert store.videos(CHANNEL)[0].available is True


def test_save_keeps_a_complete_catalogue_complete(tmp_path):
    # A partial fetch merged into a complete catalogue is a union, so nothing
    # about completeness changed; `save` with no opinion leaves the flag.
    store = cs.ChannelStore(tmp_path / "channels")
    store.write(_ref(), [], complete=True)
    stored = store.save(_ref(), [])
    assert stored.complete is True
    assert store.save(_ref(), [], complete=False).complete is False
