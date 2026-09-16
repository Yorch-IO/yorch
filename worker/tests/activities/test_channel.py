"""Reading a channel, as activities.

Nothing here spends: the provider is replaced. Three properties are what these
exist for — that a video id is derived and never taken from the caller, that a
catalog which is merely down costs a figure and not a run, and that neither pass
holds the worker's only event loop.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from dataclasses import asdict
from datetime import datetime, timezone

import pytest
from temporalio.exceptions import ApplicationError

from brainworker.activities import channel as acts
from brainworker.channel import preselect, topics
from brainworker.channel.types import DiscoverRequest, TopicsRequest
from brainworker.channelstore import ChannelStore, library_id_for
from brainworker.graph.schema import LEGACY_TENANT_ID
from brainworker.providers.gemini import Generation, Usage
from brainworker.youtube import ChannelRef, ChannelVideo

CHANNEL = "UCabcdefghijklmnopqrstuv"
LIBRARY = library_id_for(CHANNEL)

#: Long enough that a blocked loop cannot hide behind scheduling noise, short
#: enough to keep these fast. Same figures as the paid stages' own loop tests.
_CALL = 0.02
_TICK = 0.002


@pytest.fixture
def workspace(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv("BRAIN_WORKSPACE_DIR", str(tmp_path))
    # Pointed at a port nothing serves on purpose: every catalog write in this
    # module is best-effort, and the test is that they stay out of the way.
    monkeypatch.setenv("BRAIN_DATABASE_URL", "postgresql://brain:x@127.0.0.1:1/brain")
    monkeypatch.setenv("BRAIN_GEMINI_PROJECT_ID", "proj-test")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    return tmp_path


class FakeProvider:
    def __init__(self, payload=None, *, delay: float = 0.0):
        self.payload = payload if payload is not None else {}
        self.delay = delay
        self.usage = Usage(1000, 200, 0, 1)
        self.calls = 0

        class _Settings:
            model = "gemini-3.6-flash"

        self.settings = _Settings()

    def generate(self, prompt, **kw):
        import time

        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return Generation(
            text=json.dumps(self.payload), usage=self.usage, finish_reason=None
        )


def _videos(n: int, duration_s: int = 2700) -> list[ChannelVideo]:
    return [
        ChannelVideo(
            video_id=f"v{i:010d}"[:11],
            title=f"Prédica {i}",
            description="d" * 100,
            # Descending, so index 0 is the newest — which is the order the
            # store returns and the order `limit` means.
            published_at=f"2026-01-{n - i:02d}T00:00:00Z",
            duration_s=duration_s,
        )
        for i in range(n)
    ]


def _sync(workspace: pathlib.Path, videos: list[ChannelVideo]) -> None:
    ChannelStore(workspace / "channels").save(
        ChannelRef(
            channel_id=CHANNEL,
            title="Casa Sobre la Roca",
            handle="casarocachannel",
            description="",
            uploads_playlist_id="UUabc",
            url="https://www.youtube.com/@casarocachannel",
        ),
        videos,
        now=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def _discover(**kw) -> DiscoverRequest:
    return DiscoverRequest(
        channel_id=CHANNEL,
        topic="justicia social",
        library_id=LIBRARY,
        tenant_id=LEGACY_TENANT_ID,
        **kw,
    )


async def _ticks_during(coro) -> tuple[object, int]:
    """Run `coro`, counting the turns the event loop got while it ran.

    A blocked loop gives the ticker none. The ticker is cancelled with no await
    in between, so every tick counted happened *during* the call.
    """
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(_TICK)
            ticks += 1

    beating = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        beating.cancel()
    return result, ticks


# --- quoting ------------------------------------------------------------------


async def test_the_quote_is_persisted_inside_the_run(workspace):
    _sync(workspace, _videos(10))
    estimate = await acts.quote_channel_discovery(
        "channel-1", _discover(limit=10, deep_limit=2)
    )
    assert {s.stage for s in estimate.stages} == {preselect.STAGE, topics.STAGE}

    # Shown before the button, recorded inside the run: that pairing is what
    # makes "did it over-report?" answerable once Temporal has forgotten the run.
    written = json.loads((workspace / "runs" / "channel-1" / "estimate.json").read_text())
    assert written["total_usd"] == estimate.total_usd
    assert written["price_source"]


async def test_a_channel_nobody_synced_is_a_permanent_failure(workspace):
    with pytest.raises(ApplicationError) as e:
        await acts.quote_channel_discovery("channel-1", _discover())
    assert e.value.type == "channel_not_synced"
    assert e.value.non_retryable


async def test_a_quote_that_cannot_be_written_does_not_fail_the_run(
    workspace, monkeypatch
):
    # Never fail a run over its own receipt.
    _sync(workspace, _videos(3))

    def boom(*_a, **_k):
        raise OSError("read-only volume")

    monkeypatch.setattr(acts.ArtifactStore, "write_json", boom)
    estimate = await acts.quote_channel_discovery("channel-1", _discover())
    assert estimate.stages


# --- the metadata pass --------------------------------------------------------


def _verdicts(videos: list[ChannelVideo], relevancia="relevante") -> dict:
    return {
        "resultados": [
            {
                "video_id": v.video_id,
                "relevancia": relevancia,
                "puntaje": 90,
                "razon": "el título lo dice",
                "incertidumbre": "baja",
            }
            for v in videos
        ]
    }


async def test_the_preselection_records_the_whole_measurement(workspace, monkeypatch):
    videos = _videos(3)
    _sync(workspace, videos)
    monkeypatch.setattr(acts, "_provider", lambda _s: FakeProvider(_verdicts(videos)))

    outcome = await acts.preselect_channel_videos(
        "channel-1", _discover(limit=3, deep_limit=2)
    )
    assert (outcome.evaluated, outcome.relevant) == (3, 3)
    assert len(outcome.shortlist) == 2  # capped at `deep_limit`

    # A measurement whose instrument is unrecorded cannot be compared with the
    # next one, so the artifact carries the prompt version and the model.
    written = json.loads(
        (workspace / "runs" / "channel-1" / "preselection.json").read_text()
    )
    assert written["prompt_version"] == preselect.PROMPT_VERSION
    assert written["model"] == "gemini-3.6-flash"
    assert written["evaluated"] == [v.video_id for v in videos]


async def test_the_preselection_leaves_the_event_loop_free(workspace, monkeypatch):
    # The recorded defect: an `async def` around a synchronous provider call
    # holds the worker's only loop, so the heartbeat it records can never be
    # sent — and nothing else in this suite can see a blocked loop.
    videos = _videos(2)
    _sync(workspace, videos)
    monkeypatch.setattr(
        acts, "_provider", lambda _s: FakeProvider(_verdicts(videos), delay=_CALL)
    )
    _, ticks = await _ticks_during(
        acts.preselect_channel_videos("channel-1", _discover(limit=2))
    )
    assert ticks > 0


# --- the transcript pass ------------------------------------------------------


class _FakeCatalog:
    def __init__(self, runs=None, documents=None, artifacts=None):
        self._runs = runs or {}
        self._documents = documents or []
        self._artifacts = artifacts or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, workflow_id, *, tenant_id):
        return self._runs.get(workflow_id)

    def documents(self, library_id, *, include_absent=False):
        return list(self._documents)

    def artifacts(self, run_id):
        return list(self._artifacts.get(run_id, []))

    def record_artifact(self, *a, **k):
        pass

    def record_cost(self, *a, **k):
        pass


class _Run:
    def __init__(self, run_id, *, kind="video", library_id=LIBRARY, document_id="doc_a"):
        self.id = run_id
        self.workflow_id = run_id
        self.kind = kind
        self.library_id = library_id
        self.document_id = document_id


class _Doc:
    def __init__(self, doc_id="doc_a", source_key="youtube/aaaaaaaaaaa"):
        self.id = doc_id
        self.source_key = source_key


def _transcript(workspace: pathlib.Path, run_id: str, text: str) -> dict:
    from brainworker.artifacts import ArtifactStore

    ref = ArtifactStore(workspace, run_id).write_text("transcript_text", text)
    return {
        "name": "transcript_text",
        "rel_path": ref.path,
        "sha256": ref.sha256,
        "size_bytes": ref.bytes,
    }


def _topics_request(runs: list[str]) -> TopicsRequest:
    return TopicsRequest(
        channel_id=CHANNEL,
        topic="justicia social",
        library_id=LIBRARY,
        video_runs=runs,
        tenant_id=LEGACY_TENANT_ID,
    )


async def test_the_video_id_comes_from_the_run_and_never_from_the_caller(
    workspace, monkeypatch
):
    # A client-supplied pairing would let one video's words be filed under
    # another's identity — the damage `check_resolved` refuses, which fails
    # nowhere downstream.
    text = "hoy hablamos de la justicia social"
    catalog = _FakeCatalog(
        runs={"video-1": _Run("video-1")},
        documents=[_Doc(source_key="youtube/zzzzzzzzzzz")],
        artifacts={"video-1": [_transcript(workspace, "video-1", text)]},
    )
    monkeypatch.setattr(acts, "Catalog", lambda *a, **k: catalog)
    monkeypatch.setattr(
        acts,
        "_provider",
        lambda _s: FakeProvider(
            {
                "temas": [{"tema": "justicia", "evidencia": "la justicia social"}],
                "responde_a_la_consulta": True,
                "motivo": "sí",
            }
        ),
    )
    await acts.read_channel_topics("channel-1", _topics_request(["video-1"]))
    written = json.loads((workspace / "runs" / "channel-1" / "topics.json").read_text())
    assert written["videos"][0]["video_id"] == "zzzzzzzzzzz"
    assert written["videos"][0]["verified"] == 1


@pytest.mark.parametrize(
    "run,kind",
    [
        (None, "run_not_found"),
        (_Run("video-1", kind="index"), "run_not_in_this_channel"),
        (_Run("video-1", library_id="lib_otra"), "run_not_in_this_channel"),
    ],
)
async def test_a_run_from_somewhere_else_is_refused(workspace, monkeypatch, run, kind):
    # An id is not authorization, and this one arrives over HTTP.
    catalog = _FakeCatalog(runs={"video-1": run} if run else {})
    monkeypatch.setattr(acts, "Catalog", lambda *a, **k: catalog)
    monkeypatch.setattr(acts, "_provider", lambda _s: FakeProvider())
    with pytest.raises(ApplicationError) as e:
        await acts.read_channel_topics("channel-1", _topics_request(["video-1"]))
    assert e.value.type == kind


async def test_a_run_with_no_transcript_yet_is_skipped_rather_than_failed(
    workspace, monkeypatch
):
    # Still probing, or a video with no captions whose audio has not been
    # transcribed. That is a state, not an error.
    catalog = _FakeCatalog(
        runs={"video-1": _Run("video-1")}, documents=[_Doc()], artifacts={"video-1": []}
    )
    monkeypatch.setattr(acts, "Catalog", lambda *a, **k: catalog)
    monkeypatch.setattr(acts, "_provider", lambda _s: FakeProvider())
    outcome = await acts.read_channel_topics("channel-1", _topics_request(["video-1"]))
    assert outcome.videos == 0
    assert outcome.failed == 0


async def test_a_transcript_whose_file_is_gone_costs_its_own_reading(
    workspace, monkeypatch
):
    # The row outlives the file in two recorded ways — a pruned run directory,
    # and a catalog holding rows written under a different workspace.
    row = _transcript(workspace, "video-1", "algo")
    (workspace / row["rel_path"]).unlink()
    catalog = _FakeCatalog(
        runs={"video-1": _Run("video-1")},
        documents=[_Doc()],
        artifacts={"video-1": [row]},
    )
    monkeypatch.setattr(acts, "Catalog", lambda *a, **k: catalog)
    monkeypatch.setattr(acts, "_provider", lambda _s: FakeProvider())
    outcome = await acts.read_channel_topics("channel-1", _topics_request(["video-1"]))
    assert outcome.videos == 0


async def test_verified_and_unverified_topics_are_counted_apart(workspace, monkeypatch):
    text = "hoy hablamos de la justicia social"
    catalog = _FakeCatalog(
        runs={"video-1": _Run("video-1")},
        documents=[_Doc()],
        artifacts={"video-1": [_transcript(workspace, "video-1", text)]},
    )
    monkeypatch.setattr(acts, "Catalog", lambda *a, **k: catalog)
    monkeypatch.setattr(
        acts,
        "_provider",
        lambda _s: FakeProvider(
            {
                "temas": [
                    {"tema": "a", "evidencia": "la justicia social"},
                    {"tema": "b", "evidencia": "esto no está en el texto"},
                ],
                "responde_a_la_consulta": True,
                "motivo": "sí",
            }
        ),
    )
    outcome = await acts.read_channel_topics("channel-1", _topics_request(["video-1"]))
    assert (outcome.verified, outcome.unverified) == (1, 1)


async def test_the_transcript_pass_leaves_the_event_loop_free(workspace, monkeypatch):
    catalog = _FakeCatalog(
        runs={"video-1": _Run("video-1")},
        documents=[_Doc()],
        artifacts={"video-1": [_transcript(workspace, "video-1", "algo")]},
    )
    monkeypatch.setattr(acts, "Catalog", lambda *a, **k: catalog)
    monkeypatch.setattr(
        acts,
        "_provider",
        lambda _s: FakeProvider(
            {"temas": [], "responde_a_la_consulta": False, "motivo": "no"},
            delay=_CALL,
        ),
    )
    _, ticks = await _ticks_during(
        acts.read_channel_topics("channel-1", _topics_request(["video-1"]))
    )
    assert ticks > 0


# --- the catalog being down ---------------------------------------------------


async def test_a_catalog_that_is_down_costs_a_figure_and_not_the_pass(workspace, monkeypatch):
    """`pooled=False` everywhere, with the bound asserted rather than commented.

    A pool retries a refused connection in the background, so a catalog that is
    merely down turns one immediate error into a ten-second stall — per write,
    on a pass that has a perfectly good answer without it. Measured once already
    at 50 s -> 0.45 s on the export suite.
    """
    import time

    videos = _videos(2)
    _sync(workspace, videos)
    monkeypatch.setattr(acts, "_provider", lambda _s: FakeProvider(_verdicts(videos)))

    started = time.monotonic()
    outcome = await acts.preselect_channel_videos("channel-1", _discover(limit=2))
    elapsed = time.monotonic() - started

    assert outcome.evaluated == 2
    assert elapsed < 5.0, f"the pass waited {elapsed:.1f}s on a catalog that is down"
