"""The channel catalogue on disk: it deduplicates, it survives a partial fetch,
and it refuses an id that must not become a path segment.

`tmp_path`, no stores, no network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brainworker import channelstore as cs
from brainworker.config import Paths
from brainworker.graph.schema import LEGACY_TENANT_ID
from brainworker.youtube import ChannelRef, ChannelVideo

CHANNEL = "UCabcdefghijklmnopqrstuv"
OTHER = "UCzyxwvutsrqponmlkjihgfe"


def _ref(channel_id: str = CHANNEL, title: str = "Casa Sobre la Roca") -> ChannelRef:
    return ChannelRef(
        channel_id=channel_id,
        title=title,
        handle="casarocachannel",
        description="Predicaciones",
        uploads_playlist_id="UUabc",
        url="https://www.youtube.com/@casarocachannel",
    )


def _v(vid: str, **kw) -> ChannelVideo:
    base = dict(title=vid, description="d", published_at="2026-01-01T00:00:00Z")
    base.update(kw)
    return ChannelVideo(video_id=vid, **base)


def _store(tmp_path) -> cs.ChannelStore:
    return cs.ChannelStore(tmp_path / "channels")


# --- identity ----------------------------------------------------------------


def test_the_library_id_keeps_the_channel_id_case():
    # A YouTube channel id is case-sensitive URL-safe base64. Folding the case
    # would put two different channels in one library, and every document in it
    # would be real — so nothing downstream could see the merge.
    upper = "UCABCDEFGHIJKLMNOPQRSTUV"
    assert upper.lower() == CHANNEL.lower()  # the two differ only in case
    assert cs.library_id_for(CHANNEL) == f"lib_yt_{CHANNEL}"
    assert cs.library_id_for(upper) == f"lib_yt_{upper}"
    assert cs.library_id_for(upper) != cs.library_id_for(CHANNEL)


@pytest.mark.parametrize(
    "bad", ["", "..", "../../etc", "UCshort", "not-a-channel", "UC" + "a" * 23]
)
def test_an_id_that_could_escape_the_workspace_is_refused(bad, tmp_path):
    with pytest.raises(cs.UnusableChannelId):
        cs.library_id_for(bad)
    with pytest.raises(cs.UnusableChannelId):
        _store(tmp_path).dir_for(bad)


# --- round trip --------------------------------------------------------------


def test_a_saved_channel_reads_back_whole(tmp_path):
    store = _store(tmp_path)
    stored = store.save(_ref(), [_v("aaaaaaaaaaa", duration_s=600)], units_spent=3)
    assert stored.video_count == 1
    assert stored.units_spent == 3
    assert stored.library_id == f"lib_yt_{CHANNEL}"

    again = store.read(CHANNEL)
    assert again is not None
    assert again.channel.title == "Casa Sobre la Roca"
    assert again.channel.uploads_playlist_id == "UUabc"
    assert again.video_count == 1
    assert [v.video_id for v in store.videos(CHANNEL)] == ["aaaaaaaaaaa"]
    assert store.videos(CHANNEL)[0].duration_s == 600


def test_reading_a_channel_that_was_never_synced_is_not_an_error(tmp_path):
    store = _store(tmp_path)
    assert store.read(CHANNEL) is None
    assert store.videos(CHANNEL) == []
    assert store.list() == []


# --- the property the acceptance criterion names ------------------------------


def test_two_consecutive_syncs_do_not_duplicate(tmp_path):
    store = _store(tmp_path)
    videos = [_v("aaaaaaaaaaa"), _v("bbbbbbbbbbb")]
    store.save(_ref(), videos)
    stored = store.save(_ref(), videos)
    assert stored.video_count == 2
    assert len(store.videos(CHANNEL)) == 2


def test_a_partial_sync_keeps_what_it_did_not_mention(tmp_path):
    # A limit, or a quota that ran out on page four. Neither is a statement that
    # the rest of the channel has gone.
    store = _store(tmp_path)
    store.save(_ref(), [_v("aaaaaaaaaaa"), _v("bbbbbbbbbbb")])
    store.save(_ref(), [_v("ccccccccccc")])
    assert {v.video_id for v in store.videos(CHANNEL)} == {
        "aaaaaaaaaaa",
        "bbbbbbbbbbb",
        "ccccccccccc",
    }


def test_a_later_sync_never_loses_a_duration_already_paid_for(tmp_path):
    store = _store(tmp_path)
    store.save(_ref(), [_v("aaaaaaaaaaa", duration_s=4573)])
    store.save(_ref(), [_v("aaaaaaaaaaa")])  # listed again, not yet hydrated
    assert store.videos(CHANNEL)[0].duration_s == 4573


def test_a_retitled_channel_updates_without_forking(tmp_path):
    store = _store(tmp_path)
    store.save(_ref(title="Casa Sobre la Roca"), [_v("aaaaaaaaaaa")])
    store.save(_ref(title="Casa Sobre la Roca Oficial"), [_v("aaaaaaaaaaa")])
    assert len(store.list()) == 1
    assert store.read(CHANNEL).channel.title == "Casa Sobre la Roca Oficial"


# --- listing -----------------------------------------------------------------


def test_listing_is_newest_sync_first(tmp_path):
    store = _store(tmp_path)
    store.save(
        _ref(OTHER, "Otro"), [], now=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    store.save(_ref(), [], now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert [s.channel.channel_id for s in store.list()] == [CHANNEL, OTHER]


def test_one_unreadable_catalogue_does_not_take_the_listing_down(tmp_path):
    # Syncing again is free, so a corrupt file must cost its own channel and
    # nothing else — a picker that cannot render is worse than a row missing.
    store = _store(tmp_path)
    store.save(_ref(), [_v("aaaaaaaaaaa")])
    broken = store.dir_for(OTHER)
    broken.mkdir(parents=True)
    (broken / cs.CHANNEL_FILE).write_text("{ not json", encoding="utf-8")
    assert [s.channel.channel_id for s in store.list()] == [CHANNEL]


def test_a_truncated_videos_file_reads_as_empty_rather_than_raising(tmp_path):
    store = _store(tmp_path)
    store.save(_ref(), [_v("aaaaaaaaaaa")])
    (store.dir_for(CHANNEL) / cs.VIDEOS_FILE).write_text('{"videos": [', encoding="utf-8")
    assert store.videos(CHANNEL) == []


def test_nothing_partial_is_left_behind(tmp_path):
    store = _store(tmp_path)
    store.save(_ref(), [_v("aaaaaaaaaaa")])
    names = sorted(p.name for p in store.dir_for(CHANNEL).iterdir())
    assert names == [cs.CHANNEL_FILE, cs.VIDEOS_FILE]


# --- where it sits on the volume ---------------------------------------------


def test_the_catalogue_is_scoped_to_the_organisation(tmp_path):
    # Unlike `runs/`, whose tenant blindness `config.Paths` records as a known
    # crossing. There is nothing here to migrate, so scoping it costs nothing.
    paths = Paths(tmp_path)
    legacy = paths.for_tenant(LEGACY_TENANT_ID).channels
    other = paths.for_tenant("tnt_0123456789abcdef01234567").channels
    assert legacy == tmp_path / "channels"
    assert other == tmp_path / "tenants" / "tnt_0123456789abcdef01234567" / "channels"
    assert not paths.for_tenant(LEGACY_TENANT_ID).contains(other)
