"""The two routes a video adds, and the one a video would have broken.

Temporal and the catalog are faked. What is under test is the routing decisions:
that a URL is refused in the request that asked rather than as a failed run, that
the video gate is served from its own route because its shape differs, and that
Reindexar starts the workflow a video can survive.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker.api import main  # noqa: E402
from brainworker.catalog.repo import Document  # noqa: E402
from brainworker.graph.projection import TIMED_FORMATS  # noqa: E402

VID = "dQw4w9WgXcQ"


class FakeHandle:
    def __init__(self, wid: str):
        self.id = wid


class FakeClient:
    def __init__(self, started: list):
        self.started = started

    async def start_workflow(self, run, *, args, id, task_queue, memo=None, **kw):
        self.started.append({"run": run, "args": args, "id": id})
        return FakeHandle(id)


@pytest.fixture
def started(monkeypatch):
    calls: list = []

    async def _temporal():
        return FakeClient(calls)

    monkeypatch.setattr(main, "temporal", _temporal)
    return calls


@pytest.fixture
def client():
    return TestClient(main.app)


# --- POST /videos ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/123456",
        "https://www.youtube.com/playlist?list=PLabc",
        "https://www.youtube.com/@canal",
        "not a url",
        "",
    ],
)
def test_a_url_that_is_not_one_video_is_refused_in_the_request_that_asked(
    client, started, url
):
    """The allowlist is the SSRF guard too.

    yt-dlp ships ~1800 extractors and a `generic` one that will fetch an
    arbitrary host, and unlike `stage_source` there is no `Paths.contains` to
    inherit. Refusing here *and* in the activity is the same doubling
    `retrieve.search` keeps for `tenant_id`.
    """
    got = client.post("/videos", json={"request": {"library_id": "lib_v", "url": url}})
    assert got.status_code == 422, got.text
    assert got.json()["detail"]["kind"] == "not_a_video_url"
    assert started == [], "nothing may reach Temporal for a refused URL"


def test_a_good_url_starts_a_video_workflow_with_a_video_prefixed_id(client, started):
    got = client.post(
        "/videos",
        json={"request": {"library_id": "lib_v", "url": f"https://youtu.be/{VID}"}},
    )
    assert got.status_code == 200, got.text
    assert got.json()["state"] == "running"
    assert got.json()["workflow_id"].startswith("video-")
    assert len(started) == 1
    # The workflow id prefix is also the run id and the artifact directory name,
    # so it has to say which kind of run this is.
    assert started[0]["id"].startswith("video-")
    assert started[0]["args"][0].url.endswith(VID)


def test_the_body_is_embedded_like_ingest_and_not_bare_like_reindex(client, started):
    """`/ingest` takes `{request, options}` and `/reindex` takes a bare
    `StageOptions`. The paid plane reproduces both quirks deliberately, so a new
    route has to pick one and mean it."""
    got = client.post(
        "/videos",
        json={
            "request": {"library_id": "lib_v", "url": f"https://youtu.be/{VID}"},
            "options": {"correct": False, "embed": True},
        },
    )
    assert got.status_code == 200, got.text
    assert started[0]["args"][1].correct is False


# --- the gate route ----------------------------------------------------------


def test_an_unknown_run_is_a_404_with_a_kind_the_app_can_key_on(client, monkeypatch):
    class Missing:
        # The real client hands back a handle for any id and fails on the
        # *query*, which is why `rebuild_gate` and this route both wrap the
        # query rather than the lookup.
        def get_workflow_handle(self, wid):
            return self

        async def query(self, q, *a, **k):
            raise RuntimeError("workflow execution not found")

    async def _temporal():
        return Missing()

    monkeypatch.setattr(main, "temporal", _temporal)
    got = client.get("/runs/video-1/video-gate")
    assert got.status_code == 404
    assert got.json()["detail"]["kind"] == "run_not_found"


def test_a_gate_still_being_built_is_a_409_naming_the_stage(client, monkeypatch):
    """409 rather than 404, because the app reads a 409 as *keep waiting* — and
    a run that answered 404 while it was merely busy would look failed."""

    class Busy:
        def get_workflow_handle(self, wid):
            return self

        async def query(self, q, *a, **k):
            return "probing" if getattr(q, "__name__", "") == "stage" else None

    async def _temporal():
        return Busy()

    monkeypatch.setattr(main, "temporal", _temporal)
    got = client.get("/runs/video-1/video-gate")
    assert got.status_code == 409
    assert got.json()["detail"]["kind"] == "gate_not_ready"
    assert got.json()["detail"]["stage"] == "probing"


# --- the route a video would have broken ------------------------------------


def _document(fmt: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id="doc_v", library_id="lib_v", folder_id=None,
        source_key=f"youtube/{VID}", title="Charla", author="Canal",
        format=fmt, present=True, absent_since=None, tags=[],
        created_at=now, updated_at=now,
        source_path=f"https://youtu.be/{VID}" if fmt == "youtube" else "/w/inbox/a.pdf",
    )


@pytest.fixture
def catalog_with(monkeypatch):
    def _install(document: Document):
        class FakeCatalog:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def document(self, document_id, library_id=None):
                return document

        monkeypatch.setattr(main, "Catalog", FakeCatalog)

    return _install


def test_reindexing_a_video_starts_the_workflow_it_can_survive(
    client, started, catalog_with
):
    """`IngestWorkflow` would take the URL through `stage_source`, which checks
    tenant containment on a filesystem path and dies three frames from the
    cause."""
    catalog_with(_document("youtube"))
    got = client.post("/libraries/lib_v/documents/doc_v/reindex", json={})
    assert got.status_code == 200, got.text
    assert got.json()["kind"] == "video"
    assert started[0]["id"].startswith("video-")
    request = started[0]["args"][0]
    assert request.url == f"https://youtu.be/{VID}"
    # Without this the run short-circuits on `already_indexed`, which is the
    # whole point of the button.
    assert request.reindex is True


def test_reindexing_a_document_is_untouched_by_the_video_branch(
    client, started, catalog_with
):
    catalog_with(_document("pdf"))
    got = client.post("/libraries/lib_v/documents/doc_v/reindex", json={})
    assert got.status_code == 200, got.text
    assert got.json()["kind"] == "reindex"
    assert started[0]["id"].startswith("reindex-")


def test_the_api_and_the_locator_agree_on_what_a_timed_format_is():
    """Two modules branch on this string — the route that picks a workflow and
    the locator that renders a timestamp instead of a byte range. A
    correspondence test, because nothing else makes them agree."""
    assert main.TIMED_FORMAT in TIMED_FORMATS
