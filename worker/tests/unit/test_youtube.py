"""The channel catalogue's parsing, its quota arithmetic, and its refusals.

No network: `Client.open_url` is injected, which is the whole reason it is a
parameter. What is worth asserting here is the *mapping* — from the six shapes a
channel link arrives in to one lookup, from an ISO 8601 duration to seconds, and
from an HTTP body to the advice a person acts on — and a test that needed a key
and a network to check any of those would run rarely enough to be worth nothing.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from brainworker import youtube as y

CHANNEL = "UCabcdefghijklmnopqrstuv"


# --- what a link names -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,kind,value",
    [
        (f"https://www.youtube.com/channel/{CHANNEL}", "id", CHANNEL),
        (f"youtube.com/channel/{CHANNEL}", "id", CHANNEL),
        ("https://www.youtube.com/@Casarocachannel", "handle", "Casarocachannel"),
        ("https://www.youtube.com/@Casarocachannel/videos", "handle", "Casarocachannel"),
        ("https://m.youtube.com/@Casarocachannel", "handle", "Casarocachannel"),
        ("https://www.youtube.com/c/CasaSobreLaRoca", "handle", "CasaSobreLaRoca"),
        ("https://www.youtube.com/user/casaroca", "username", "casaroca"),
        ("@Casarocachannel", "handle", "Casarocachannel"),
        ("Casarocachannel", "handle", "Casarocachannel"),
        ("  https://www.youtube.com/@Casarocachannel  ", "handle", "Casarocachannel"),
        (CHANNEL, "id", CHANNEL),
    ],
)
def test_channel_lookup_reads_every_shape(raw, kind, value):
    got = y.channel_lookup(raw)
    assert (got.kind, got.value) == (kind, value)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://vimeo.com/@someone",
        "https://youtube.com.evil.example/@someone",
        "https://www.youtube.com/",
        "https://www.youtube.com/channel/not-a-channel-id",
        "@a",
    ],
)
def test_channel_lookup_refuses_what_is_not_a_channel(raw):
    with pytest.raises(y.NotAChannelUrl):
        y.channel_lookup(raw)


def test_a_video_link_is_not_a_channel_link():
    # "index this channel" and "index this video" are different requests with a
    # screen each. Quietly treating a video URL as its channel would start a
    # hundred runs for somebody who pasted one.
    with pytest.raises(y.NotAChannelUrl):
        y.channel_lookup("https://youtu.be/dQw4w9WgXcQ")
    with pytest.raises(y.NotAChannelUrl):
        y.channel_lookup("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


# --- duration ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("PT1H16M13S", 4573),
        ("PT45M10S", 2710),
        ("PT5M", 300),
        ("PT50S", 50),
        ("PT0S", 0),
        ("P1DT2H", 93600),
        ("PT2H", 7200),
        ("", 0),
        ("nonsense", 0),
    ],
)
def test_parse_iso8601_duration(text, seconds):
    assert y.parse_iso8601_duration(text) == seconds


# --- the fake API ------------------------------------------------------------


class Fake:
    """An `open_url` that answers from a script and records what was asked."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if not self.pages:
            raise AssertionError(f"unscripted request: {url}")
        return json.dumps(self.pages.pop(0)).encode("utf-8")


def _item(vid: str, *, title: str = "", published: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "snippet": {
            "title": title or f"Video {vid}",
            "description": f"descripción de {vid}",
            "publishedAt": "2020-01-01T00:00:00Z",
            "resourceId": {"videoId": vid},
            "thumbnails": {"medium": {"url": f"https://i.ytimg.com/vi/{vid}/mq.jpg"}},
        },
        "contentDetails": {"videoId": vid, "videoPublishedAt": published},
    }


def _client(pages: list[dict]) -> tuple[y.Client, Fake]:
    fake = Fake(pages)
    return y.Client(api_key="k", open_url=fake), fake


def test_a_client_with_no_key_refuses_before_any_request():
    with pytest.raises(y.YouTubeError) as e:
        y.Client(api_key="")
    assert e.value.kind == "youtube_key_missing"


def test_resolve_channel_reads_the_uploads_playlist():
    client, fake = _client(
        [
            {
                "items": [
                    {
                        "id": CHANNEL,
                        "snippet": {
                            "title": "Casa Sobre la Roca",
                            "customUrl": "@casarocachannel",
                            "description": "Predicaciones",
                        },
                        "contentDetails": {"relatedPlaylists": {"uploads": "UUabc"}},
                    }
                ]
            }
        ]
    )
    ref = client.resolve_channel(y.ChannelLookup("handle", "Casarocachannel"))
    assert ref.channel_id == CHANNEL
    assert ref.uploads_playlist_id == "UUabc"
    assert ref.handle == "casarocachannel"
    assert ref.url == "https://www.youtube.com/@casarocachannel"
    assert "forHandle=%40Casarocachannel" in fake.urls[0]
    assert client.units == 1


def test_an_unresolvable_handle_is_an_error_and_never_a_search():
    # The whole reason `search.list` is not a fallback: it costs 100 units
    # against 1 and answers with the channel Google thinks you meant. A channel
    # indexed under the name of a different one fails nowhere at all.
    client, fake = _client([{"items": []}, {"items": []}])
    with pytest.raises(y.YouTubeError) as e:
        client.resolve_channel(y.ChannelLookup("handle", "nobody"))
    assert e.value.kind == "channel_not_found"
    assert not any("search" in url for url in fake.urls)


def test_a_handle_that_only_resolves_as_a_legacy_username_still_resolves():
    # A channel from before handles existed can keep its custom URL as a legacy
    # username, so `forHandle` finds nothing and `forUsername` finds it. One
    # more unit, and still not a search.
    client, fake = _client(
        [
            {"items": []},
            {
                "items": [
                    {
                        "id": CHANNEL,
                        "snippet": {"title": "Antiguo", "customUrl": ""},
                        "contentDetails": {"relatedPlaylists": {"uploads": "UUold"}},
                    }
                ]
            },
        ]
    )
    ref = client.resolve_channel(y.ChannelLookup("handle", "casaroca"))
    assert ref.uploads_playlist_id == "UUold"
    assert "forHandle" in fake.urls[0] and "forUsername=casaroca" in fake.urls[1]
    assert client.units == 2
    # And with no handle of its own it still has a canonical link.
    assert ref.url == f"https://www.youtube.com/channel/{CHANNEL}"


def test_a_channel_with_no_uploads_playlist_is_refused():
    client, _ = _client(
        [{"items": [{"id": CHANNEL, "snippet": {}, "contentDetails": {}}]}]
    )
    with pytest.raises(y.YouTubeError) as e:
        client.resolve_channel(y.ChannelLookup("id", CHANNEL))
    assert e.value.kind == "channel_not_found"


def test_list_uploads_pages_until_the_limit():
    page1 = {
        "items": [_item(f"vid{i:08d}abc"[:11]) for i in range(50)],
        "nextPageToken": "T2",
    }
    page2 = {"items": [_item(f"w{i:08d}abc"[:11]) for i in range(50)]}
    client, fake = _client([page1, page2])
    videos = client.list_uploads("UUabc", 60)
    assert len(videos) == 60
    assert client.units == 2
    assert "pageToken=T2" in fake.urls[1]


def test_list_uploads_drops_what_it_cannot_read_without_spending_the_limit():
    items = [_item("aaaaaaaaaaa", title="Private video"), _item("bbbbbbbbbbb")]
    client, _ = _client([{"items": items}])
    videos = client.list_uploads("UUabc", 2)
    assert [v.video_id for v in videos] == ["bbbbbbbbbbb"]


def test_a_playlist_item_takes_its_date_from_the_video_not_the_playlist():
    # `snippet.publishedAt` is when the video entered the playlist. On an
    # uploads playlist the two usually agree and are not contracted to.
    item = _item("ccccccccccc", published="2019-07-04T10:00:00Z")
    client, _ = _client([{"items": [item]}])
    assert client.list_uploads("UUabc", 1)[0].published_at == "2019-07-04T10:00:00Z"


def test_hydrate_matches_by_id_rather_than_by_position():
    # `videos.list` omits an id it cannot serve instead of returning a hole, so
    # zipping the two lists shifts every duration after the first gap onto the
    # wrong video — a figure that is wrong and looks perfectly ordinary.
    client, _ = _client(
        [
            {
                "items": [
                    {
                        "id": "ccccccccccc",
                        "contentDetails": {"duration": "PT10M"},
                        "snippet": {"liveBroadcastContent": "none"},
                    }
                ]
            }
        ]
    )
    videos = [
        y.ChannelVideo(video_id="bbbbbbbbbbb", title="b", description="", published_at=""),
        y.ChannelVideo(video_id="ccccccccccc", title="c", description="", published_at=""),
    ]
    out = client.hydrate(videos)
    assert [v.video_id for v in out] == ["bbbbbbbbbbb", "ccccccccccc"]
    assert out[0].duration_s == 0
    assert out[1].duration_s == 600


def test_hydrate_costs_one_unit_per_fifty():
    ids = [f"v{i:010d}"[:11] for i in range(120)]
    client, _ = _client([{"items": []}, {"items": []}, {"items": []}])
    client.hydrate(
        [y.ChannelVideo(video_id=i, title="", description="", published_at="") for i in ids]
    )
    assert client.units == 3


# --- failure classification --------------------------------------------------


def _http_error(code: int, reason: str, message: str = "") -> urllib.error.HTTPError:
    body = json.dumps(
        {"error": {"message": message, "errors": [{"reason": reason}]}}
    ).encode("utf-8")

    import io

    return urllib.error.HTTPError(
        "https://example", code, "err", {}, io.BytesIO(body)
    )


@pytest.mark.parametrize(
    "code,reason,kind",
    [
        (403, "quotaExceeded", "youtube_quota_exceeded"),
        (403, "dailyLimitExceeded", "youtube_quota_exceeded"),
        (403, "forbidden", "youtube_refused"),
        (400, "keyInvalid", "youtube_refused"),
        (500, "backendError", "youtube_unreadable"),
    ],
)
def test_a_failure_is_classified_by_the_reason_not_only_the_status(code, reason, kind):
    # The two 403s need opposite advice: a key that is not authorised is fixed
    # in the console once, a quota that ran out is fixed by waiting. Telling
    # somebody to check a key that is fine costs an afternoon.
    def raiser(_url: str) -> bytes:
        raise _http_error(code, reason)

    client = y.Client(api_key="k", open_url=raiser)
    with pytest.raises(y.YouTubeError) as e:
        client.resolve_channel(y.ChannelLookup("id", CHANNEL))
    assert e.value.kind == kind


def test_an_unreachable_api_is_not_a_refusal():
    def raiser(_url: str) -> bytes:
        raise urllib.error.URLError("no route to host")

    client = y.Client(api_key="k", open_url=raiser)
    with pytest.raises(y.YouTubeError) as e:
        client.resolve_channel(y.ChannelLookup("id", CHANNEL))
    assert e.value.kind == "youtube_unreachable"


# --- merging -----------------------------------------------------------------


def _v(vid: str, **kw) -> y.ChannelVideo:
    base = dict(title=vid, description="", published_at="2026-01-01T00:00:00Z")
    base.update(kw)
    return y.ChannelVideo(video_id=vid, **base)


def test_merge_keeps_a_known_video_a_partial_fetch_did_not_mention():
    # A `limit`, a quota that ran out mid-page, a network that failed on page
    # four: none of those is a statement that the rest of the channel is gone.
    known = [_v("aaaaaaaaaaa"), _v("bbbbbbbbbbb")]
    merged = y.merge_catalogue(known, [_v("ccccccccccc")])
    assert {v.video_id for v in merged} == {
        "aaaaaaaaaaa",
        "bbbbbbbbbbb",
        "ccccccccccc",
    }


def test_merge_does_not_duplicate_on_a_second_sync():
    known = [_v("aaaaaaaaaaa", duration_s=600)]
    merged = y.merge_catalogue(known, list(known))
    assert len(merged) == 1
    assert merged[0].duration_s == 600


def test_merge_never_overwrites_a_known_duration_with_an_unhydrated_zero():
    # Which is what lets `list_uploads` be stored before `hydrate` has run.
    known = [_v("aaaaaaaaaaa", duration_s=4573, live_state="none")]
    merged = y.merge_catalogue(known, [_v("aaaaaaaaaaa", duration_s=0)])
    assert merged[0].duration_s == 4573


def test_merge_orders_newest_first():
    merged = y.merge_catalogue(
        [_v("aaaaaaaaaaa", published_at="2020-01-01T00:00:00Z")],
        [_v("bbbbbbbbbbb", published_at="2026-09-01T00:00:00Z")],
    )
    assert [v.video_id for v in merged] == ["bbbbbbbbbbb", "aaaaaaaaaaa"]
