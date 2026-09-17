"""Cataloguing a channel: free, quota-classified, and honest about what is indexed.

No Postgres and no network. `Catalog` is faked because these routes need three
of its methods, and the Data API client is driven through the injected
`open_url` that exists for exactly this.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from brainworker import channelstore, youtube
from brainworker.api import main
from brainworker.catalog.repo import Document, LibraryOwnedByAnother

CHANNEL = "UCabcdefghijklmnopqrstuv"
LIBRARY = f"lib_yt_{CHANNEL}"


class _FakeCatalog:
    """Only what these routes call."""

    def __init__(self) -> None:
        self.libraries_rows: list[dict] = []
        self.documents_rows: list[Document] = []
        self.active: dict[str, str | None] = {}
        self.ensured: list[tuple[str, str, str]] = []
        self.refuse_library = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ensure_library(self, library_id, name="", *, tenant_id, language="es"):
        if self.refuse_library:
            raise LibraryOwnedByAnother(library_id)
        self.ensured.append((library_id, name, tenant_id))
        return library_id

    def libraries(self, *, tenant_id):
        return list(self.libraries_rows)

    def documents(self, library_id, *, include_absent=False):
        return list(self.documents_rows)

    def active_version(self, document_id):
        return self.active.get(document_id)


def _document(source_key: str, doc_id: str) -> Document:
    return Document(
        id=doc_id,
        library_id=LIBRARY,
        folder_id=None,
        source_key=source_key,
        title="t",
        author=None,
        format="youtube",
        present=True,
        absent_since=None,
        tags=[],
        created_at=None,
        updated_at=None,
        source_path=None,
        tenant_id="tnt_000000000000000000000001",
    )


@pytest.fixture
def catalog(monkeypatch):
    fake = _FakeCatalog()
    monkeypatch.setattr(main, "Catalog", lambda *_, **__: fake)
    return fake


@pytest.fixture
def store(monkeypatch, tmp_path):
    made = channelstore.ChannelStore(tmp_path / "channels")
    monkeypatch.setattr(main, "_channel_store", lambda: made)
    return made


@pytest.fixture
def client():
    return TestClient(main.app)


def _api(pages: list[dict]):
    """An `open_url` that answers from a script, or raises the 403 a page names."""
    remaining = list(pages)

    def call(url: str) -> bytes:
        if not remaining:
            raise AssertionError(f"unscripted request: {url}")
        page = remaining.pop(0)
        if "__raise__" in page:
            import io
            import urllib.error

            body = json.dumps(
                {"error": {"errors": [{"reason": page["__raise__"]}], "message": "quota"}}
            ).encode("utf-8")
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(body))
        return json.dumps(page).encode("utf-8")

    return call


def _with_key(monkeypatch, pages: list[dict]) -> None:
    monkeypatch.setattr(
        main, "_youtube_client", lambda: youtube.Client("k", open_url=_api(pages))
    )


CHANNEL_PAGE = {
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


def _uploads(ids: list[str], next_page: str = "") -> dict:
    page = {
        "items": [
            {
                "snippet": {
                    "title": f"Prédica {v}",
                    "description": "x" * 900,
                    "resourceId": {"videoId": v},
                },
                "contentDetails": {
                    "videoId": v,
                    "videoPublishedAt": "2026-01-01T00:00:00Z",
                },
            }
            for v in ids
        ]
    }
    if next_page:
        page["nextPageToken"] = next_page
    return page


def _quota_exceeded() -> dict:
    """A page that is not a page: the scripted `open_url` raises it."""
    return {"__raise__": "quotaExceeded"}


def _durations(ids: list[str]) -> dict:
    return {
        "items": [
            {
                "id": v,
                "contentDetails": {"duration": "PT45M"},
                "snippet": {"liveBroadcastContent": "none", "description": "y" * 900},
            }
            for v in ids
        ]
    }


# --- refusals before anything is spent ---------------------------------------


def test_a_link_that_is_not_a_channel_is_refused_with_a_kind(client):
    r = client.post("/channels/sync", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 422
    assert r.json()["detail"]["kind"] == "not_a_channel_url"


def test_no_key_is_a_503_and_not_an_empty_catalogue(client, monkeypatch):
    # "This channel has no videos" and "nobody here can ask about this channel"
    # are different facts, and only one of them is about the channel.
    monkeypatch.setattr(main, "settings", lambda: _settings(key=""))
    r = client.post("/channels/sync", json={"url": "@Casarocachannel"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "youtube_key_missing"


def _settings(key: str = "k"):
    import pathlib

    from brainworker import config

    return config.Settings(
        workspace=pathlib.Path("/tmp"),
        temporal_target="",
        temporal_namespace="default",
        task_queue="q",
        qdrant_url="",
        qdrant_collection="brain",
        memgraph_url="",
        database_url="",
        log_level="INFO",
        secrets_file=pathlib.Path("/nonexistent"),
        youtube_api_key=key,
    )


def test_an_out_of_range_limit_is_fastapis_own_422(client):
    # A field constraint rather than a hand-raised error, so both planes answer
    # a bad body the same way — the decision `Question.effort` records. There
    # is no upper bound any more: the catalogue is free and saved page by page.
    r = client.post("/channels/sync", json={"url": "@x", "limit": 0})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


@pytest.mark.parametrize(
    "reason,status,kind",
    [
        ("quotaExceeded", 429, "youtube_quota_exceeded"),
        ("forbidden", 502, "youtube_refused"),
    ],
)
def test_the_two_403s_get_different_statuses(client, monkeypatch, reason, status, kind):
    def boom():
        raise main._youtube_http(
            youtube.YouTubeError("nope", kind=kind)
        )

    monkeypatch.setattr(main, "_youtube_client", boom)
    r = client.post("/channels/sync", json={"url": "@Casarocachannel"})
    assert r.status_code == status
    assert r.json()["detail"]["kind"] == kind


# --- the happy path ----------------------------------------------------------


def test_sync_catalogues_the_channel_and_creates_its_library(
    client, catalog, store, monkeypatch
):
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(ids), _durations(ids)])

    r = client.post("/channels/sync", json={"url": "@Casarocachannel", "limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["channel"]["channel_id"] == CHANNEL
    assert body["library_id"] == LIBRARY
    assert body["video_count"] == 2
    assert body["fetched"] == 2
    assert body["units_spent"] == 3

    # The shelf exists before anything is on it, named after the channel.
    assert catalog.ensured == [(LIBRARY, "Casa Sobre la Roca", main.LEGACY_TENANT_ID)]
    assert [v.duration_s for v in store.videos(CHANNEL)] == [2700, 2700]


def test_no_limit_walks_the_whole_playlist_and_says_the_catalogue_is_complete(
    client, catalog, store, monkeypatch
):
    # Three pages, no cap. `complete` is what tells "all 2,000" from "the most
    # recent 500", and the screen needs to say which one it is showing.
    a, b, c = ["a" * 11], ["b" * 11], ["c" * 11]
    _with_key(
        monkeypatch,
        [CHANNEL_PAGE, _uploads(a, "p2"), _durations(a), _uploads(b, "p3"), _durations(b),
         _uploads(c), _durations(c)],
    )
    r = client.post("/channels/sync", json={"url": "@Casarocachannel"})
    assert r.status_code == 200
    body = r.json()
    assert body["video_count"] == 3
    assert body["fetched"] == 3
    assert body["added"] == 3
    assert body["complete"] is True
    assert body["stopped_early"] is False
    assert store.read(CHANNEL).complete is True


def test_a_quota_that_runs_out_mid_walk_keeps_every_page_before_it(
    client, catalog, store, monkeypatch
):
    # The whole reason the sync is page by page. The 429 is about the pages
    # that did not arrive; the ones that did are on disk, and the catalogue
    # says it is incomplete so the next sync walks on rather than stopping at
    # what it knows.
    a = ["a" * 11]
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(a, "p2"), _durations(a), _quota_exceeded()])
    r = client.post("/channels/sync", json={"url": "@Casarocachannel"})
    assert r.status_code == 429
    assert r.json()["detail"]["kind"] == "youtube_quota_exceeded"
    assert [v.video_id for v in store.videos(CHANNEL)] == a
    assert store.videos(CHANNEL)[0].duration_s == 2700
    assert store.read(CHANNEL).complete is False


def test_a_second_sync_of_a_complete_catalogue_stops_at_the_first_known_page(
    client, catalog, store, monkeypatch
):
    # Newest-first: once a page holds nothing new, nothing after it is new
    # either. One `playlistItems` call and no `videos.list` at all — the known
    # durations are already paid for. `_api` raises on an unscripted request,
    # so the assertion is that the second script is never reached.
    a = ["a" * 11]
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(a), _durations(a)])
    client.post("/channels/sync", json={"url": "@Casarocachannel"})
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(a, "p2")])
    r = client.post("/channels/sync", json={"url": "@Casarocachannel"})
    assert r.status_code == 200
    assert r.json()["stopped_early"] is True
    assert r.json()["units_spent"] == 2
    assert r.json()["complete"] is True


def test_an_incomplete_catalogue_walks_past_what_it_knows(
    client, catalog, store, monkeypatch
):
    # Every catalogue written under the old 500-video cap is this case: its
    # first pages are known perfectly and nothing after them is. Stopping at
    # the first known page would leave it capped for ever.
    a, b = ["a" * 11], ["b" * 11]
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(a, "p2"), _durations(a)])
    client.post("/channels/sync", json={"url": "@Casarocachannel", "limit": 1})
    assert store.read(CHANNEL).complete is False
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(a, "p2"), _uploads(b), _durations(b)])
    r = client.post("/channels/sync", json={"url": "@Casarocachannel"})
    assert r.status_code == 200
    assert r.json()["stopped_early"] is False
    assert r.json()["added"] == 1
    assert r.json()["complete"] is True
    # Same publication date in the fixture, so the order is the id's; what
    # matters is that both are there.
    assert sorted(v.video_id for v in store.videos(CHANNEL)) == a + b


def test_a_full_sync_marks_what_the_playlist_no_longer_holds_and_drops_nothing(
    client, catalog, store, monkeypatch
):
    a, b = ["a" * 11], ["b" * 11]
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(a + b), _durations(a + b)])
    client.post("/channels/sync", json={"url": "@Casarocachannel"})
    # `b` was deleted on YouTube. An incremental sync says nothing about it…
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(a)])
    client.post("/channels/sync", json={"url": "@Casarocachannel"})
    assert all(v.available for v in store.videos(CHANNEL))
    # …a full one marks it, keeps it, and the listing says so.
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(a)])
    r = client.post("/channels/sync", json={"url": "@Casarocachannel", "full": True})
    assert r.status_code == 200
    assert r.json()["unavailable"] == 1
    assert r.json()["video_count"] == 2
    by_id = {v.video_id: v.available for v in store.videos(CHANNEL)}
    assert by_id == {"a" * 11: True, "b" * 11: False}
    detail = client.get(f"/channels/{CHANNEL}").json()
    assert {v["video_id"]: v["available"] for v in detail["videos"]} == by_id


def test_two_syncs_do_not_duplicate(client, catalog, store, monkeypatch):
    ids = ["aaaaaaaaaaa"]
    for _ in range(2):
        _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(ids), _durations(ids)])
        r = client.post("/channels/sync", json={"url": "@Casarocachannel", "limit": 1})
        assert r.status_code == 200
    assert r.json()["video_count"] == 1


def test_a_library_another_organisation_owns_is_a_409(
    client, catalog, store, monkeypatch
):
    catalog.refuse_library = True
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads([]), {"items": []}])
    r = client.post("/channels/sync", json={"url": "@Casarocachannel"})
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "library_owned_by_another"


# --- listing and detail ------------------------------------------------------


def test_listing_reports_the_catalogs_counts_not_the_files(
    client, catalog, store, monkeypatch
):
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(["aaaaaaaaaaa"]), _durations(["aaaaaaaaaaa"])])
    client.post("/channels/sync", json={"url": "@Casarocachannel", "limit": 1})
    catalog.libraries_rows = [
        {"id": LIBRARY, "name": "Casa", "language": "es",
         "documents": 7, "indexed_versions": 5}
    ]
    body = client.get("/channels").json()
    assert body["channels"][0]["documents"] == 7
    assert body["channels"][0]["indexed_versions"] == 5


def test_a_channel_that_was_never_synced_is_a_404(client, catalog, store):
    r = client.get(f"/channels/{CHANNEL}")
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "channel_not_synced"


def test_an_id_that_could_escape_the_workspace_is_refused(client, catalog, store):
    assert client.get("/channels/..").status_code in (404, 422)
    assert client.get("/channels/not-a-channel-id").status_code == 422


def test_indexed_is_postgres_answer_and_never_the_files(
    client, catalog, store, monkeypatch
):
    # The catalogue on disk says what the *channel* holds. Whether a video is
    # indexed is `document.source_key` plus an active version, asked every time
    # rather than mirrored — a second record of one fact can disagree in silence.
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(ids), _durations(ids)])
    client.post("/channels/sync", json={"url": "@Casarocachannel", "limit": 3})

    catalog.documents_rows = [
        _document("youtube/aaaaaaaaaaa", "doc_a"),
        _document("youtube/bbbbbbbbbbb", "doc_b"),
    ]
    catalog.active = {"doc_a": "ver_a", "doc_b": None}

    videos = {v["video_id"]: v for v in client.get(f"/channels/{CHANNEL}").json()["videos"]}
    assert videos["aaaaaaaaaaa"]["active_version_id"] == "ver_a"
    # A document with no active version is a real state — a cancelled run, or an
    # activation withheld over a structural mismatch — not a gap.
    assert videos["bbbbbbbbbbb"]["document_id"] == "doc_b"
    assert videos["bbbbbbbbbbb"]["active_version_id"] is None
    assert videos["ccccccccccc"]["document_id"] is None


def test_the_listing_truncates_descriptions_and_says_so(
    client, catalog, store, monkeypatch
):
    ids = ["aaaaaaaaaaa"]
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(ids), _durations(ids)])
    client.post("/channels/sync", json={"url": "@Casarocachannel", "limit": 1})

    short = client.get(f"/channels/{CHANNEL}").json()["videos"][0]
    assert len(short["description"]) == main.DESCRIPTION_PREVIEW
    assert short["description_truncated"] is True

    full = client.get(f"/channels/{CHANNEL}?full=true").json()["videos"][0]
    assert len(full["description"]) == 900
    assert full["description_truncated"] is False


def test_the_preview_is_cut_at_a_word_and_never_inside_one():
    # The screen builds keyword chips from the preview. On the first real
    # channel the cut fell inside "familia" often enough to put `famil` in the
    # top ten chips of 2,983 videos.
    words = ("familia " * 80).strip()            # 639 characters, spaces every 8
    out = main._preview(words)
    assert len(out) <= main.DESCRIPTION_PREVIEW
    assert out.endswith("familia")
    assert not out.endswith(" ")
    # No space at all: nothing to cut at, so the hard limit stands.
    assert len(main._preview("x" * 900)) == main.DESCRIPTION_PREVIEW


# --- quoting and starting ----------------------------------------------------


@pytest.fixture
def synced(client, catalog, store, monkeypatch):
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    _with_key(monkeypatch, [CHANNEL_PAGE, _uploads(ids), _durations(ids)])
    client.post("/channels/sync", json={"url": "@Casarocachannel", "limit": 3})
    return ids


def test_the_quote_is_free_and_reaches_no_provider(client, synced, monkeypatch):
    # No `_youtube_client` is scripted and no Temporal client is available: if
    # this route touched either, it could not answer.
    r = client.post(
        f"/channels/{CHANNEL}/discovery-estimate",
        json={"topic": "justicia social y pobreza", "limit": 3, "deep_limit": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["evaluated"] == 3
    assert body["read"] == 2
    stages = {s["stage"]: s for s in body["estimate"]["stages"]}
    assert set(stages) == {"channel-preselect", "channel-topics"}
    assert body["estimate"]["total_usd"] > 0
    assert body["estimate"]["price_source"]


def test_the_quote_judges_only_the_filtered_videos(client, synced):
    # The screen's title-keyword filter. `evaluated` is what the screen prints
    # as "se juzgarán N vídeos", so it has to be the filtered count and not the
    # catalogue's — otherwise the quote prices a run that judges videos the
    # person filtered out.
    r = client.post(
        f"/channels/{CHANNEL}/discovery-estimate",
        json={
            "topic": "un tema",
            "limit": 3,
            "deep_limit": 3,
            "video_ids": [synced[0], synced[2], "zzzzzzzzzzz"],
        },
    )
    assert r.status_code == 200
    assert r.json()["evaluated"] == 2
    assert r.json()["read"] == 2


def test_a_filter_longer_than_a_catalogue_can_be_is_fastapis_own_422(client, synced):
    r = client.post(
        f"/channels/{CHANNEL}/discovery-estimate",
        json={"topic": "un tema", "video_ids": ["a" * 11] * 501},
    )
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


def test_the_quote_says_how_many_videos_it_could_not_measure(client, synced):
    # A video with no duration cannot be quoted. Saying how many beats quoting
    # them at nothing, because a zero in a bill is a claim.
    r = client.post(
        f"/channels/{CHANNEL}/discovery-estimate",
        json={"topic": "un tema", "limit": 3, "deep_limit": 3},
    )
    assert r.json()["unmeasured"] == 0


def test_only_the_transcript_half_carries_a_range(client, synced):
    r = client.post(
        f"/channels/{CHANNEL}/discovery-estimate",
        json={"topic": "un tema", "limit": 3, "deep_limit": 3},
    )
    stages = {s["stage"]: s for s in r.json()["estimate"]["stages"]}
    assert stages["channel-preselect"]["usd_high"] == stages["channel-preselect"]["usd"]
    assert stages["channel-topics"]["usd_high"] > stages["channel-topics"]["usd"]


@pytest.mark.parametrize(
    "body",
    [
        {"topic": "ab"},                       # under min_length
        {"topic": "válido", "limit": 0},
        {"topic": "válido", "deep_limit": 26},  # over the caption-throttle cap
    ],
)
def test_a_bad_query_is_fastapis_own_422(client, synced, body):
    r = client.post(f"/channels/{CHANNEL}/discovery-estimate", json=body)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


def test_quoting_a_channel_nobody_synced_is_a_404(client, catalog, store):
    r = client.post(
        f"/channels/{CHANNEL}/discovery-estimate", json={"topic": "un tema"}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "channel_not_synced"


def test_discovering_without_a_provider_is_a_503(client, synced, monkeypatch):
    monkeypatch.setattr(main, "settings", lambda: _settings(key="k"))
    r = client.post(f"/channels/{CHANNEL}/discover", json={"topic": "un tema"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "provider_unconfigured"


def test_reading_nothing_is_refused_rather_than_started(client, synced, monkeypatch):
    # Checked before the provider, the same order `POST /videos` uses: a body
    # naming no videos is a client defect that would still be there after
    # somebody configured a provider.
    monkeypatch.setattr(main, "settings", lambda: _settings(key="k"))
    r = client.post(
        f"/channels/{CHANNEL}/topics", json={"topic": "un tema", "video_runs": []}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["kind"] == "no_videos_to_read"


def test_reading_without_a_provider_is_a_503(client, synced, monkeypatch):
    monkeypatch.setattr(main, "settings", lambda: _settings(key="k"))
    r = client.post(
        f"/channels/{CHANNEL}/topics",
        json={"topic": "un tema", "video_runs": ["video-1"]},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "provider_unconfigured"
