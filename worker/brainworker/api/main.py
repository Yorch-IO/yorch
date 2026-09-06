"""The control plane the desktop app talks to.

Bound to loopback by the compose port mapping and reached only through Rust,
which proxies each call as an explicit Tauri command. There is no
authentication because there is no path to this port from off the machine —
that assumption is enforced by the `127.0.0.1:` prefix on every published port
in docker-compose.yaml, and breaking it would need to be a deliberate act.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import Field
from uuid import uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

from .. import auditlog, config
from ..artifacts import ArtifactRef, ArtifactStore
from ..catalog import Catalog, MigrationError, current_version, require_schema
from ..graph import DEFAULT_CONFIDENCE_FLOOR, Graph, GraphError
from ..graph.queries import TemplateError, bind, get
from ..answering.effort import DEFAULT_EFFORT, MAX_STYLE_CHARS
from ..chat.title import fallback as fallback_title
from ..chat.types import (
    MAX_MESSAGE_CHARS,
    WINDOW_ANSWER_CHARS,
    WINDOW_TURNS,
    ChatStart,
    ChatTurn,
    TurnRecord,
)
from ..graph.schema import LEGACY_TENANT_ID, SEMANTIC_EDGES
from ..activities.rebuild import REQUIRED_ARTIFACT

#: The artifact a measured run leaves behind. Named here rather than written as a
#: literal at the two call sites, for the reason `REQUIRED_ARTIFACT` exists: the
#: probe and the reader must not be able to drift apart.
SCORES_ARTIFACT = "scores"
from ..answering import Question, ask
from .. import videosource
from ..pipeline import (
    SUPPORTED_FORMATS,
    IngestRequest,
    StageOptions,
    VideoRequest,
)
from ..workflows.ask import AskWorkflow
from ..workflows.chat import ChatWorkflow
from ..workflows.ingest import Approval, IngestWorkflow
from ..workflows.rebuild import RebuildWorkflow
from ..workflows.video import VideoIngestWorkflow
from ..workflows.ping import PingWorkflow

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 5.0

#: How long to wait for a workflow to answer a `stage` query before giving up.
#:
#: Short on purpose. A workflow inside a long activity does not answer at all —
#: measured against a real ingest mid-semantics, where `describe()` came back in
#: 0.00s and the query had not answered after 15 — so this is not "how long the
#: query takes" but "how long to block a route that has better sources for the
#: same fact". `state` comes from `describe()` and the catalog keeps a `stage`;
#: the query is the nicety, not the answer.
STAGE_QUERY_TIMEOUT = 2.0

#: Why the catalog schema is not usable, or None when it is. Startup does not
#: fail on a database that is still coming up — /health has to stay answerable
#: for exactly that case — so the failure is recorded and retried instead.
_schema_error: str | None = "not attempted"


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Check the catalog schema before serving; do not change it.

    This used to apply migrations, which is what kept the schema and the code
    reading it from ever disagreeing. Prisma owns the schema now
    (`yorch-tauri-backend/prisma/migrations`), applied by the `migrate` service
    before either plane starts, so what is left here is the check that closes
    the same window from the other side: the API asks whether the migration this
    code was written against is present, and reports the answer on /health.

    Startup still does not fail. A control plane whose /health is how an
    operator finds out what is wrong must be able to answer while Postgres is
    still coming up.
    """
    ensure_schema()
    yield


app = FastAPI(title="Company Brain control API", version="0.1.0", lifespan=lifespan)


#: Stamped on every workflow this plane starts. This plane *is* the legacy
#: organisation and always has been, so the value is a constant rather than a
#: parameter — but it has to be written, because the paid plane reads it to
#: decide whether a caller may see a run at all. A run with no memo forces that
#: check back onto the catalog, and `AskWorkflow` never writes a catalog row.
_OWNED_BY_LEGACY = {"tenant_id": LEGACY_TENANT_ID}


@dataclass
class AnswerStyleUpdate:
    """The new wording for one effort level. Empty clears the override.

    The cap is a field constraint rather than a hand-raised error so that
    FastAPI answers it in its own 422-with-a-list shape — which is the shape the
    paid plane's exception filter reproduces for `class-validator` failures. The
    same reasoning as `Question.effort`: a hand-rolled kind here would make the
    two planes answer one oversized body two different ways.
    """

    body: Annotated[str, Field(max_length=MAX_STYLE_CHARS)] = ""


async def _describe(handle: Any) -> Any | None:
    """One `describe()`, swallowed, so a caller wanting two facts pays once.

    `None` means Temporal has forgotten the run, which is ordinary once
    retention expires and is not a failure: the catalog still holds what the run
    produced. Swallowed for the same reason the stage query is.
    """
    try:
        return await handle.describe()
    except Exception:
        return None


def _state_of(description: Any | None) -> str | None:
    """Whether the run is still going, or what it ended as.

    `stage` cannot answer this. A query against a *failed* workflow hands back
    the last stage it recorded, which is indistinguishable from one still
    working — observed 2026-08-28, when an ingest whose activity retries were
    exhausted reported `"stage": "learning"` indefinitely while
    `describe().status` already read FAILED.
    """
    status = getattr(description, "status", None)
    return status.name.lower() if status is not None else None


async def _run_state(handle: Any) -> str | None:
    return _state_of(await _describe(handle))


async def _run_progress(client: Any, description: Any | None) -> dict[str, Any] | None:
    """How far the running activity has got, when it says.

    Read off `pending_activities`, where `activity.heartbeat` leaves it. Only
    `extract_semantics` reports today, and it is the one worth reporting: it is
    one generation call per chunk, so a chunk count is a call count and a spend
    count, and it is the stage long enough that a person wonders whether to wait.

    `None` covers every honest absence, and they are deliberately not told apart:
    nothing pending, an activity that does not heartbeat, a run older than this
    code, or details that will not decode. All four mean the same thing to a
    reader — this run is not reporting progress — and inventing four ways to say
    it would be four things for a screen to handle.
    """
    raw = getattr(description, "raw_description", None)
    if raw is None:
        return None
    for pending in raw.pending_activities:
        if not pending.HasField("heartbeat_details"):
            continue
        try:
            details = await client.data_converter.decode(
                list(pending.heartbeat_details.payloads)
            )
        except Exception:
            continue
        if len(details) >= 2:
            done, total = details[0], details[1]
            if isinstance(done, int) and isinstance(total, int) and total > 0:
                return {
                    "activity": pending.activity_type.name,
                    "done": done,
                    "total": total,
                }
    return None

_settings: config.Settings | None = None
_client: Client | None = None


def ensure_schema() -> str | None:
    """Check the catalog schema. Returns the error, or None when it is usable."""
    global _schema_error
    try:
        applied = require_schema(settings().database_url)
        log.info("catalog schema ok: %d migration(s), newest %s", len(applied), applied[-1])
        _schema_error = None
    except MigrationError as e:
        # A schema behind the code is a version disagreement, not a transient
        # outage, and it has a one-line fix the message names. Retrying it every
        # request would bury the cause.
        log.error("catalog schema is unusable: %s", e)
        _schema_error = str(e)
    except Exception as e:
        log.warning("catalog not migrated yet: %s: %s", type(e).__name__, e)
        _schema_error = f"{type(e).__name__}: {e}"
    return _schema_error


def settings() -> config.Settings:
    global _settings
    if _settings is None:
        _settings = config.load()
    return _settings


async def temporal() -> Client:
    """Connect lazily, and re-connect if a previous attempt failed.

    The API must answer /health while Temporal is still starting, so a failed
    connection cannot be fatal at import time.
    """
    global _client
    if _client is None:
        s = settings()
        _client = await Client.connect(s.temporal_target, namespace=s.temporal_namespace)
    return _client


@app.get("/health/live")
def live() -> dict[str, str]:
    """Whether this process is up. Deliberately dependency-free: the container
    healthcheck uses it, so it must not fail because something else is down."""
    return {"status": "ok"}


@app.get("/health")
async def health() -> dict[str, Any]:
    """Readiness of every backing service, each reported independently.

    One aggregate boolean would tell the user "something is broken"; the app's
    Stack screen needs to say which one.
    """
    s = settings()
    services: dict[str, dict[str, Any]] = {
        "api": {"ok": True, "detail": f"workspace {s.workspace}"},
        "qdrant": _check(lambda: _qdrant_detail(s)),
        "memgraph": _check(lambda: _memgraph_detail(s)),
        "postgres": _check(lambda: _postgres_detail(s)),
        "catalog": _catalog_health(s),
        "temporal": await _check_temporal(s),
        "provider": _provider_health(s),
    }
    return {"ok": all(v["ok"] for v in services.values()), "services": services}


def _provider_health(s: config.Settings) -> dict[str, Any]:
    """Whether *this process* has a billing project, and which models it would use.

    Reported here because the app cannot otherwise tell a setting from a setting
    that took effect. `stack.rs` writes `BRAIN_GEMINI_PROJECT_ID` into `.env` and
    compose interpolates it at `up` time, so the Services screen knows what
    *will* apply on the next start; this row is what *is* applied, read from the
    running container's own environment. Contradiction between the two is the
    normal state right after the user names a project, and saying so is better
    than an unchanged error message.

    Deliberately free. `Gemini.configured` is `bool(project_id)` and nothing
    here calls Vertex — proving the credentials actually work costs money and is
    `POST /provider/probe`, because a health check that spends is one people
    turn off.
    """
    g = s.gemini
    return {
        "ok": g.configured,
        "detail": (
            f"{g.project_id or 'sin proyecto'} · {g.model} / {g.embedding_model} "
            f"@ {g.location}"
        ),
    }


def _check(fn) -> dict[str, Any]:
    try:
        return {"ok": True, "detail": fn()}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def _qdrant_detail(s: config.Settings) -> str:
    r = httpx.get(f"{s.qdrant_url}/readyz", timeout=PROBE_TIMEOUT)
    r.raise_for_status()
    body = httpx.get(f"{s.qdrant_url}/collections", timeout=PROBE_TIMEOUT).json()
    names = [c["name"] for c in body.get("result", {}).get("collections", [])]
    return f"ready, {len(names)} collection(s)"


def _catalog_health(s: config.Settings) -> dict[str, Any]:
    """Schema readiness, reported apart from Postgres reachability.

    They fail for different reasons and have different fixes: an unreachable
    Postgres is a stack problem, an unapplied migration is a version problem.
    One combined row would send the user to the wrong one.
    """
    if _schema_error is not None and ensure_schema() is not None:
        return {"ok": False, "detail": _schema_error}
    try:
        version = current_version(s.database_url)
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    return {"ok": version is not None, "detail": f"schema {version or 'absent'}"}


def _memgraph_detail(s: config.Settings) -> str:
    with Graph(s.memgraph_url, timeout=PROBE_TIMEOUT) as graph:
        return graph.probe()


def _postgres_detail(s: config.Settings) -> str:
    with psycopg.connect(s.database_url, connect_timeout=int(PROBE_TIMEOUT)) as conn:
        with conn.cursor() as cur:
            cur.execute("select current_database()")
            row = cur.fetchone()
    return f"database {row[0]}" if row else "connected"


async def _check_temporal(s: config.Settings) -> dict[str, Any]:
    try:
        client = await temporal()
        await client.service_client.check_health()
        return {"ok": True, "detail": f"namespace {s.temporal_namespace}"}
    except Exception as e:
        global _client
        _client = None  # force a fresh connection on the next attempt
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


@app.post("/provider/probe")
async def provider_probe() -> dict[str, Any]:
    """Prove the credentials, the project and the models all work — and pay for it.

    Separate from `/health` on purpose. `_provider_health` answers "is a project
    named", which is free and true of a process that has never authenticated;
    this answers "would a paid stage succeed", which cannot be known without
    making a real call. Gemini Enterprise refuses API keys outright, so the
    failure this catches is almost always ADC: expired, absent, or resolved to an
    account without `roles/aiplatform.user`.

    `Provider.probe` issues one short embedding rather than a generation because
    it is the cheapest request Vertex bills for.
    """
    import asyncio

    from ..providers import Provider, ProviderError

    s = settings()
    try:
        detail = await asyncio.to_thread(lambda: Provider(s.gemini).probe())
    except ProviderError as e:
        # The provider already classified this into something the UI's GUIDANCE
        # map keys on; flattening it to a message would throw that away.
        raise HTTPException(
            status_code=503, detail={"kind": e.kind, "message": str(e)}
        ) from e
    return {"ok": True, "detail": detail}


@app.post("/ping")
async def ping() -> dict[str, Any]:
    """Run the walking-skeleton workflow and return what the worker saw.

    Proves the whole path in one call: this process → Temporal → worker →
    Qdrant and Postgres → back. The Stack screen's "Run check" button.
    """
    s = settings()
    client = await temporal()
    handle = await client.start_workflow(
        PingWorkflow.run,
        id=f"ping-{_ulid()}",
        task_queue=s.task_queue,
        memo=_OWNED_BY_LEGACY,
    )
    report = await handle.result()
    return {"workflow_id": handle.id, **asdict(report)}


def _ulid() -> str:
    """A sortable, collision-resistant id for workflow ids.

    Deliberately not uuid4: Temporal ids read better in the UI when they sort
    chronologically, and this is the only place a run's identity is minted.
    """
    import time
    import uuid

    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@app.post("/ingest")
async def start_ingest(request: IngestRequest, options: StageOptions | None = None) -> dict[str, Any]:
    """Start a run. Free until someone answers the gate.

    Returns immediately with the workflow id rather than waiting: the run
    includes a human decision, so there is no response time that would be
    honest to block on.
    """
    s = settings()
    path = pathlib.Path(request.source_path)
    if path.suffix.lower() not in SUPPORTED_FORMATS:
        # Refused here rather than inside the workflow so the user gets the
        # answer in the request that asked, not as a failed run to go and read.
        raise HTTPException(
            status_code=422,
            detail={
                "kind": "unsupported_format",
                "message": f"V1 no admite {path.suffix!r}",
                "supported": sorted(SUPPORTED_FORMATS),
            },
        )

    client = await temporal()
    handle = await client.start_workflow(
        IngestWorkflow.run,
        args=[request, options or StageOptions()],
        id=f"ingest-{_ulid()}",
        task_queue=s.task_queue,
        memo=_OWNED_BY_LEGACY,
    )
    return {"workflow_id": handle.id, "state": "running"}


#: `document.format` for a source located by a clock rather than a byte range.
#: The same string `graph.projection.TIMED_FORMATS` branches on, and the reason
#: it is a named constant in both places rather than a literal in either.
TIMED_FORMAT = "youtube"


@app.post("/videos")
async def start_video(
    request: VideoRequest, options: StageOptions | None = None
) -> dict[str, Any]:
    """Start a video run. Free until someone answers the gate.

    The URL is validated **here as well as** inside `probe_video`, and that is
    belt and braces on purpose — the same doubling `retrieve.search` keeps for
    `tenant_id`. `videosource.video_id` is not only a parser: it is the host
    allowlist, and yt-dlp is an SSRF-shaped dependency with ~1800 extractors and
    a `generic` one that will fetch an arbitrary host. Unlike `stage_source`
    there is no `Paths.contains` to inherit, so this is the check that stands in
    its place, and refusing in the request that asked is what gets the user an
    answer rather than a failed run to go and read.
    """
    s = settings()
    try:
        videosource.video_id(request.url)
    except videosource.NotAVideoUrl as e:
        raise HTTPException(
            status_code=422,
            detail={
                "kind": "not_a_video_url",
                "message": f"{request.url!r} no es el enlace de un vídeo de YouTube",
                "detail": str(e),
            },
        ) from e

    client = await temporal()
    handle = await client.start_workflow(
        VideoIngestWorkflow.run,
        # `fetch_queue` is decided here rather than inside the workflow, which
        # may only decide on what its own history holds — reading an environment
        # variable there would make replay depend on the machine replaying it.
        # Empty unless this deployment's own egress is refused by YouTube, in
        # which case it names the queue a worker on an acceptable address is
        # serving. See `VideoRequest.fetch_queue`.
        args=[replace(request, fetch_queue=s.fetch_task_queue or request.fetch_queue),
              options or StageOptions()],
        id=f"video-{_ulid()}",
        task_queue=s.task_queue,
        memo=_OWNED_BY_LEGACY,
    )
    return {"workflow_id": handle.id, "state": "running"}


@app.get("/runs/{workflow_id}/video-gate")
async def video_gate(workflow_id: str) -> dict[str, Any]:
    """A video run's gate, which is a different shape from a document's.

    A separate route from `/runs/{id}/gate` even though both workflows name the
    query `gate_report` — the same decision, for the same reason, as
    `/runs/{id}/rebuild-gate`: querying through the ingest workflow's *typed*
    handle would decode a `VideoGateReport` into a `GateReport` and drop every
    field the two do not share, without failing.

    They genuinely differ. `GateReport.preview` is a required `Preview` holding
    two required artifact references, and a video with no captions has no text
    to preview until the money has been spent — so here it is optional, and
    `null` says so rather than quoting a count nobody measured.
    """
    handle = (await temporal()).get_workflow_handle(workflow_id)
    try:
        report = await handle.query(VideoIngestWorkflow.gate_report)
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail={"kind": "run_not_found", "message": f"{type(e).__name__}: {e}"},
        ) from e
    if report is None:
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "gate_not_ready",
                "message": "todavía se está examinando el vídeo",
                "stage": await handle.query(VideoIngestWorkflow.stage),
            },
        )
    return asdict(report)


@app.get("/runs/{workflow_id}/gate")
async def gate(workflow_id: str) -> dict[str, Any]:
    """What the approval screen renders, or a 409 while it is still being built."""
    handle = (await temporal()).get_workflow_handle(workflow_id)
    try:
        report = await handle.query(IngestWorkflow.gate_report)
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail={"kind": "run_not_found", "message": f"{type(e).__name__}: {e}"},
        ) from e
    if report is None:
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "gate_not_ready",
                "message": "las etapas gratuitas aún no han terminado",
                "stage": await handle.query(IngestWorkflow.stage),
                # Beside the stage, because the Import screen polls this on an
                # interval and reads a 409 as "keep waiting". A run that died
                # before publishing its gate answers `None` forever, so without
                # this the screen spins for a run that is never coming.
                "run_state": await _run_state(handle),
            },
        )
    return asdict(report)


@app.post("/runs/{workflow_id}/approve")
async def approve(workflow_id: str, approval: Approval) -> dict[str, str]:
    """Answer the gate. This is the call that can start spending money."""
    handle = (await temporal()).get_workflow_handle(workflow_id)
    try:
        await handle.signal(IngestWorkflow.approve, approval)
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail={"kind": "run_not_found", "message": f"{type(e).__name__}: {e}"},
        ) from e
    return {"workflow_id": workflow_id, "approved": str(approval.approved).lower()}


@app.post("/runs/{workflow_id}/cancel")
async def cancel_run(workflow_id: str) -> dict[str, Any]:
    """Stop a run that is already spending.

    **Cancel, not terminate.** Cancellation is delivered to the workflow, which
    unwinds — `record_run_outcome` still writes `cancelled`, the artifacts it has
    already produced stay, and the version is left importable. Terminating kills
    it where it stands and the catalog keeps whatever state it happened to be in,
    which is how a run ends up reading `running` forever.

    The reason this route exists at all is that the alternative was the Temporal
    CLI. A real ingest ran 598 chunks of semantic extraction — one generation
    call each, projected at ~$3.87 from this corpus's own measured rate — and the
    only way to stop it was `temporal workflow cancel` from a shell. A spend gate
    that can only be opened, never closed, is half a gate.

    Idempotent by nature: cancelling a workflow that has already finished is not
    an error here, because the caller's intent — "do not let this spend more" —
    is already true. A workflow Temporal has forgotten is a 404, which is the
    same thing every other route on this path says.
    """
    handle = (await temporal()).get_workflow_handle(workflow_id)
    try:
        await handle.cancel()
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail={"kind": "run_not_found", "message": f"{type(e).__name__}: {e}"},
        ) from e
    return {"workflow_id": workflow_id, "cancelled": True}


def _semantics_counts(
    workspace: pathlib.Path, run_id: str, artifacts: list[dict[str, Any]]
) -> dict[str, int] | None:
    """How many claims a run produced, and how many of them can be checked.

    Counted from the run's own `semantics.json` rather than from a catalog
    column, and that is the point: a run that predates verified quotes reports
    zero *honestly*, because none of its claims carries one. A column would have
    needed a migration to invent a number for every historical run, and the only
    honest number to invent is the one this computes for free.

    Best-effort, like the rest of this handler. A run whose workspace was pruned
    — `infra/backup.sh` found 79 such artifacts — still has to render a status.
    """
    ref = next((a for a in artifacts if a.get("name") == "semantics"), None)
    if ref is None:
        return None
    try:
        payload = ArtifactStore(workspace, run_id).read_json(
            ArtifactRef(
                kind="semantics",
                path=ref["rel_path"],
                sha256=ref["sha256"],
                bytes=ref["size_bytes"],
            )
        )
        claims = payload.get("claims") or []
        return {
            "concepts": len(payload.get("concepts") or []),
            "claims": len(claims),
            # The quote the extractor located in the chunk the claim came from.
            # Its absence is what "unverifiable" means here.
            "claims_verified": sum(1 for c in claims if c.get("quote")),
        }
    except Exception as e:
        log.warning("could not count %s's semantics: %s", run_id, e)
        return None


def _measured_scores(
    workspace: pathlib.Path, run_id: str, artifacts: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """What this run's index can actually be asked, or None if nobody asked.

    Read from the run's own `scores.json` for the reason `_semantics_counts`
    gives: a run that predates the measurement reports *nothing*, honestly,
    rather than a zero somebody would read as a broken index. Recall of 0.00 and
    "this was never measured" are different statements and only one of them is a
    fact about the corpus.

    The noise floor travels with the recall deliberately. Recall says how often
    the right chunk came back; the floor says what a *wrong* one scores, and
    without it a reader cannot tell an index that discriminates from one that
    returns everything at a similar distance.
    """
    ref = next((a for a in artifacts if a.get("name") == "scores"), None)
    if ref is None:
        return None
    try:
        payload = ArtifactStore(workspace, run_id).read_json(
            ArtifactRef(
                kind="scores",
                path=ref["rel_path"],
                sha256=ref["sha256"],
                bytes=ref["size_bytes"],
            )
        )
    except Exception as e:
        log.warning("could not read %s's scores: %s", run_id, e)
        return None

    scores = payload.get("scores") or {}
    if not scores.get("eval_questions"):
        # An eval set that produced no questions measured nothing. Rendering its
        # zeros would put "recall 0.00" on a perfectly good index.
        return None
    return {
        "recall_at_1": scores.get("recall_at_1"),
        "recall_at_5": scores.get("recall_at_5"),
        "mrr_at_10": scores.get("mrr_at_10"),
        "recall_at_5_dense_only": scores.get("recall_at_5_dense_only"),
        "noise_floor": scores.get("noise_floor"),
        "chunks": scores.get("chunks"),
        "eval_questions": scores.get("eval_questions"),
        "margin": payload.get("margin"),
        "leakage": payload.get("leakage"),
        "misses": len(payload.get("misses") or []),
    }


@app.get("/runs/{workflow_id}")
async def run_status(workflow_id: str) -> dict[str, Any]:
    """Live stage from Temporal, plus what the catalog recorded.

    Both, because they answer different questions: Temporal knows where the run
    is *now*, and the catalog knows what it produced and what it cost — which
    outlives the workflow's retention period.
    """
    s = settings()
    client = await temporal()
    handle = client.get_workflow_handle(workflow_id)
    # **Bounded, because an unbounded query hangs this whole route.** A query is
    # answered by the workflow, and a workflow sitting inside a long activity does
    # not answer: measured 2026-08-31 against a real ingest 260 generation calls
    # into semantic extraction, where `describe()` returned in 0.00s and
    # `query("stage")` had still not answered after 15 — so `GET /runs/{id}`
    # returned nothing at all, for the whole hour that stage lasts. The import
    # screen polls this route to tell a dead run from a slow one, so the hang
    # landed on exactly the screen that exists to say what is happening.
    #
    # Losing the query costs little: `state` comes from `describe()`, `progress`
    # from the activity's heartbeat, and the catalog holds a `stage` of its own.
    try:
        stage = await asyncio.wait_for(
            handle.query(IngestWorkflow.stage), timeout=STAGE_QUERY_TIMEOUT
        )
    except Exception:
        stage = None

    with Catalog(s.database_url) as catalog:
        artifacts = catalog.artifacts(workflow_id)
        costs = catalog.total_cost(workflow_id)

    # One `describe()` for both the state and the progress: they come off the
    # same response, and this endpoint is polled once per active run.
    description = await _describe(handle)

    body: dict[str, Any] = {
        "workflow_id": workflow_id,
        "stage": stage,
        "state": _state_of(description),
        "artifacts": artifacts,
        "cost": costs,
    }
    # Omitted, never zeroed, for the same reason `semantics` is below: "not
    # reporting progress" and "0 of 598 done" are different claims.
    if (progress := await _run_progress(client, description)) is not None:
        body["progress"] = progress
    # Omitted rather than zeroed when there is nothing to count: "this run
    # extracted no semantics" and "0 of 0 claims are verifiable" are different
    # statements, and only one of them is true of a structure-only run.
    if (semantics := _semantics_counts(s.workspace, workflow_id, artifacts)) is not None:
        body["semantics"] = semantics
    # Same rule again: absent means "this run was not asked to measure", which is
    # true of every run indexed before the stage existed and of every run whose
    # gate declined it.
    if (scores := _measured_scores(s.workspace, workflow_id, artifacts)) is not None:
        body["scores"] = scores
    return body


# ---------------------------------------------------------------------------
# The queue, and what a run actually did
# ---------------------------------------------------------------------------
#
# `GET /runs/{id}` answers "where is this now", and it answers it from Temporal.
# That is the right source for a live run and the wrong one for a finished one:
# Temporal forgets a run when retention expires, at which point that route
# reports `state: null` and `stage: null` for a run whose every column is still
# in Postgres. These three routes are the other half.
#
# `/runs` is the queue. `/runs/{id}/audit` is the durable ledger and reads only
# the catalog. `/runs/{id}/events` is the raw Temporal history — the one source
# that shows retries and heartbeat timeouts no application code recorded, and
# the one that goes away.


#: How many raw history events to translate before saying "truncated".
#:
#: A history is a few dozen events for an ordinary import and grows with retries.
#: The cap is about the *reader*, not the transport: past a few hundred lines
#: nobody is reading, and the ledger above already says what happened.
EVENT_CAP = 500

#: The event types worth showing a person.
#:
#: `WorkflowTaskScheduled/Started/Completed` are the bulk of any history and say
#: nothing anybody can act on — they are the workflow being woken up to decide
#: what to do next. `WorkflowTaskFailed` is kept, because that one is a bug.
_EVENTS_SHOWN = frozenset(
    {
        "WorkflowExecutionStarted",
        "WorkflowExecutionCompleted",
        "WorkflowExecutionFailed",
        "WorkflowExecutionCanceled",
        "WorkflowExecutionTerminated",
        "WorkflowExecutionTimedOut",
        "WorkflowExecutionSignaled",
        "WorkflowTaskFailed",
        "ActivityTaskScheduled",
        "ActivityTaskStarted",
        "ActivityTaskCompleted",
        "ActivityTaskFailed",
        "ActivityTaskTimedOut",
        "ActivityTaskCancelRequested",
        "ActivityTaskCanceled",
        "TimerStarted",
        "TimerFired",
        "TimerCanceled",
    }
)


def _cursor(value: str | None) -> tuple[datetime, str] | None:
    """Decode `before`, which is `{started_at ISO}|{run id}`.

    Two parts because the sort is `(started_at, id)`: `started_at` defaults to
    `now()` and several runs enqueued from one multi-file drop land in the same
    microsecond, so a timestamp alone would drop or repeat a row at the page
    boundary. A cursor that will not parse is ignored rather than refused — the
    caller gets the first page, which is a recoverable answer, instead of a 400
    on a string they did not construct.
    """
    if not value:
        return None
    stamp, _, run_id = value.partition("|")
    try:
        return datetime.fromisoformat(stamp), run_id
    except ValueError:
        return None


@app.get("/runs")
async def runs(
    limit: int = 25,
    before: str | None = None,
    kinds: str | None = None,
    states: str | None = None,
    library_id: str | None = None,
    document_id: str | None = None,
    version_id: str | None = None,
) -> dict[str, Any]:
    """The persistent queue: every run this organisation has, newest first.

    Catalog only, and therefore answerable when Temporal is down — the same
    reasoning `/project-summary` records for `recent_runs`. What a run is
    *doing* belongs to `/runs/{id}`; what it *was* belongs here, and that is what
    makes an import queue survive the window being closed.

    Not an extension of `/project-summary`: that route is the landing screen's,
    is capped at ten, and has no cursor because nobody scrolls it.
    """
    s = settings()
    capped = max(1, min(limit, 100))
    with Catalog(s.database_url) as catalog:
        rows = catalog.runs(
            # This plane *is* the legacy organisation, and saying so at the call
            # site is what makes it a decision rather than an accident.
            tenant_id=LEGACY_TENANT_ID,
            limit=capped + 1,
            before=_cursor(before),
            kinds=_csv(kinds),
            states=_csv(states),
            library_id=library_id,
            document_id=document_id,
            version_id=version_id,
        )
    # One more than asked for, so "is there another page" is answered without a
    # count over the whole table.
    more = len(rows) > capped
    rows = rows[:capped]
    return {
        "runs": [asdict(r) for r in rows],
        "next_before": (
            f"{rows[-1].started_at.isoformat()}|{rows[-1].id}" if more and rows else None
        ),
    }


@app.get("/runs/{workflow_id}/audit")
async def run_audit(workflow_id: str) -> dict[str, Any]:
    """Every stage the run passed through, what it cost, and what it produced.

    Reads only the catalog, deliberately, so it answers for a run Temporal has
    forgotten — which is every run older than the retention period, and which is
    exactly when somebody goes looking. A 404 here means no such run in this
    organisation, never "Temporal does not remember it".
    """
    s = settings()
    with Catalog(s.database_url) as catalog:
        run = catalog.run(workflow_id, tenant_id=LEGACY_TENANT_ID)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "kind": "run_not_found",
                    "message": f"no existe la ejecución {workflow_id!r}",
                },
            )
        events = catalog.run_events(workflow_id, tenant_id=LEGACY_TENANT_ID)
        costs = catalog.costs(workflow_id, tenant_id=LEGACY_TENANT_ID)
        artifacts = catalog.artifacts(workflow_id)
        # Keyed by version rather than by run, which is why this is fetched
        # separately and is empty for a run that never reached one. The row
        # carries no `kind`, so a caller cannot tell a plain collision from the
        # heading disagreement that withholds activation — that distinction
        # exists only on the Temporal payload today.
        warnings = (
            catalog.open_profile_warnings(run.version_id) if run.version_id else []
        )
    return auditlog.build(run, events, costs, artifacts, warnings)


@app.get("/runs/{workflow_id}/events")
async def run_events(workflow_id: str) -> dict[str, Any]:
    """The raw workflow history: retries, timeouts, signals, and what caused them.

    This is the only source that shows what no application code recorded. The
    run that charged semantic extraction twice looked, from every other angle,
    like one long stage — the heartbeat timeout, the closed attempt and the
    second identical pass are visible here and nowhere else.

    **`available: false` rather than a 404 or an empty list.** Temporal keeps
    history only for its retention period, and "the history has aged out" and
    "this run did nothing" must not render the same — the rule
    `/project-summary` already applies to a leg it could not read. A caller that
    sees `[]` with `available: true` is looking at a run that genuinely produced
    no events worth showing.
    """
    client = await temporal()
    handle = client.get_workflow_handle(workflow_id)
    events: list[dict[str, Any]] = []
    truncated = False
    # Which activity each scheduled event was for.
    #
    # **Only `ActivityTaskScheduled` carries the activity type.** Started,
    # Completed, Failed and TimedOut reference it by `scheduled_event_id`
    # instead, so reading the name off each event leaves two thirds of the log
    # anonymous — and the row that matters most is one of them, because the
    # *attempt* is on `ActivityTaskStarted`. Without this, the single line this
    # panel exists to show reads "ActivityTaskStarted · intento 2" with no
    # indication of which activity retried.
    named: dict[int, str] = {}
    try:
        async for event in handle.fetch_history_events():
            kind = _event_kind(event)
            if kind not in _EVENTS_SHOWN:
                continue
            if len(events) >= EVENT_CAP:
                truncated = True
                break
            events.append(_event_row(event, kind, named))
    except Exception as e:
        log.info("no history for %s: %s", workflow_id, e)
        return {"available": False, "truncated": False, "events": []}
    return {"available": True, "truncated": truncated, "events": events}


def _event_kind(event: Any) -> str:
    """`EVENT_TYPE_ACTIVITY_TASK_STARTED` -> `ActivityTaskStarted`.

    The CamelCase form is what Temporal's own UI shows and what anybody
    searching for one of these will type.

    **`event_type` is a plain `int`, not an enum object.** protobuf's Python
    runtime represents enum fields as integers, so `getattr(t, "name", str(t))`
    reads as careful and silently yields `"3"` — which matches nothing in
    `_EVENTS_SHOWN`, so the whole history filters down to an empty list and the
    route reports a run that did nothing. Found by translating a real history
    rather than a hand-built double, which would have agreed with the
    assumption.
    """
    name = EventType.Name(event.event_type).removeprefix("EVENT_TYPE_")
    return "".join(part.capitalize() for part in name.split("_"))


def _event_row(event: Any, kind: str, named: dict[int, str]) -> dict[str, Any]:
    """One line, with the activity and attempt when the event carries them.

    Attributes live on a per-type `*_event_attributes` field, so this reads
    whichever one is set rather than branching per type — a new event type then
    renders with whatever it happens to carry instead of rendering blank.

    `named` carries the activity forward from the scheduling event to the ones
    that only reference it. See `run_events`: without it the retry line, which
    is the reason this panel exists, has no name on it.
    """
    attrs = None
    for field in event.DESCRIPTOR.fields:
        if field.name.endswith("_event_attributes") and event.HasField(field.name):
            attrs = getattr(event, field.name)
            break

    activity = None
    if attrs is not None and hasattr(attrs, "activity_type"):
        activity = attrs.activity_type.name
        named[event.event_id] = activity
    elif attrs is not None:
        scheduled = getattr(attrs, "scheduled_event_id", 0)
        activity = named.get(scheduled)
    attempt = getattr(attrs, "attempt", None) if attrs is not None else None

    detail = None
    failure = getattr(attrs, "failure", None) if attrs is not None else None
    if failure is not None and getattr(failure, "message", ""):
        detail = failure.message[:500]

    stamp = event.event_time.ToDatetime().replace(tzinfo=timezone.utc)
    return {
        "id": event.event_id,
        "at": stamp.isoformat(),
        "type": kind,
        "activity": activity,
        "attempt": attempt or None,
        "detail": detail,
    }


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part for part in (p.strip() for p in value.split(",")) if part] or None


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------
#
# A question is answered in the background and collected by polling, rather than
# held open on the request that asked it.
#
# That is a measured decision. A real question against the church-history
# library ran past the desktop app's 180s client timeout, and reqwest reports an
# expired timeout with the same "error sending request for url" text it uses for
# a refused connection — so the app said the control API had not answered while
# the API log showed `POST /ask 200 OK`. The answer was computed, was paid for,
# and was thrown away, under a message blaming the wrong thing. Polling
# separates the two clocks: a client timeout now applies to a poll, and the
# answer survives the window being closed.
#
# **The store used to be this process's memory, and is now the workflow's.**
# That was safe only while exactly one process could hold it — uvicorn runs a
# single worker, and passing `workers=N` would have sent a poll to a process
# that never saw the question. A second control plane makes that assumption
# false by construction, so the state moved to `AskWorkflow`, which both planes
# reach by id. Durability came along as a side effect rather than as the motive;
# the reasoning in `answering/service.py` about a question not needing to be a
# workflow was about *durability*, and it was right.


@app.post("/ask")
async def ask_question(question: Question) -> dict[str, Any]:
    """Start answering, and return the id to collect the answer with."""
    s = settings()
    if not s.gemini.configured:
        # Refused here as well as in the activity, so a misconfigured project is
        # answered by the request that asked rather than by a failed run.
        raise HTTPException(
            status_code=503,
            detail={
                "kind": "provider_unconfigured",
                "message": "Falta BRAIN_GEMINI_PROJECT_ID: no se puede preguntar.",
            },
        )

    client = await temporal()
    handle = await client.start_workflow(
        AskWorkflow.run,
        question,
        id=f"ask-{_ulid()}",
        task_queue=s.task_queue,
        memo=_OWNED_BY_LEGACY,
    )
    return {"question_id": handle.id, "state": "running"}


@app.get("/ask/{question_id}")
async def ask_result(question_id: str) -> dict[str, Any]:
    """Collect a question started earlier, or say it is still running.

    Note the three states an answer can carry. `answered` always has at least one
    verified citation — the API cannot emit a grounded-looking answer without
    one. `insufficient_evidence` means the corpus was searched and did not
    support an answer. `off_corpus` means nothing cleared the similarity floor at
    all, which is a different problem with a different fix.
    """
    client = await temporal()
    handle = client.get_workflow_handle(question_id)
    try:
        outcome = await handle.query(AskWorkflow.result)
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail={
                "kind": "question_not_found",
                "message": (
                    "Esa pregunta ya no está en vuelo. Las preguntas viven en el "
                    "historial de Temporal y se pierden cuando expira su "
                    f"retención ({type(e).__name__})."
                ),
            },
        ) from e
    return {
        "question_id": question_id,
        "state": outcome.state,
        "answer": asdict(outcome.answer) if outcome.answer is not None else None,
        "error": outcome.error,
    }



# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


#: Node labels the summary reports, in the order a person would ask for them.
#: Anything else the graph holds is folded into neither total — a label added to
#: the schema without being added here is invisible rather than miscounted.
_SUMMARY_LABELS = ("Document", "DocumentVersion", "Section", "Chunk", "Concept", "Claim")


def _graph_totals(s: config.Settings) -> dict[str, Any]:
    """Node and edge counts for the whole graph, or an explicit unavailability.

    **Never zero for "could not ask".** Every Explore route turns a down
    Memgraph into a 503, which is right for a screen that exists to show the
    graph; it is wrong for the screen the app opens on, where the honest answer
    is "the graph could not be reached" and a hard failure would make an empty
    project and a stopped container look identical. The `/health` idiom — an
    `ok` flag beside a detail string — is what this follows, and the figures stay
    `None` so nothing downstream can add them up.

    `PROBE_TIMEOUT` and not the client's 30s default: a landing screen may not
    hang for half a minute on a container that is not running.
    """
    try:
        with Graph(s.memgraph_url, timeout=PROBE_TIMEOUT) as graph:
            scope = {"tenant_id": LEGACY_TENANT_ID}
            nodes = {
                row.data["label"]: row.data["total"]
                for row in graph.query("graph_node_counts", scope)
            }
            edges = {
                row.data["label"]: row.data["total"]
                for row in graph.query("graph_edge_counts", scope)
            }
    except Exception as e:
        return {
            "available": False,
            "detail": f"{type(e).__name__}: {e}",
            "nodes": None,
            "deterministic_edges": None,
            "semantic_edges": None,
        }

    # Split rather than summed: `HAS_CHUNK` is structure the document itself
    # supplies and `MENTIONS` is a model's proposal, and one total mixing them
    # would let the UI report model output as though the corpus had stated it.
    return {
        "available": True,
        "detail": None,
        "nodes": {label: int(nodes.get(label, 0)) for label in _SUMMARY_LABELS},
        "deterministic_edges": {
            name: int(count)
            for name, count in edges.items()
            if name not in SEMANTIC_EDGES
        },
        "semantic_edges": {
            name: int(count) for name, count in edges.items() if name in SEMANTIC_EDGES
        },
    }


def _catalog_totals(s: config.Settings, runs: int) -> dict[str, Any]:
    """Project-wide catalog figures and recent activity, degrading the same way.

    `pooled=False` for the same reason the bookkeeping writes use it: a pool
    retries a refused connection in the background, so a Postgres that is merely
    down turns one immediate error into a full-timeout stall — on the screen
    that has to render fastest.
    """
    try:
        with Catalog(s.database_url, pooled=False) as catalog:
            # This plane is the free, self-managed, single-tenant one, and its
            # organisation is the legacy one. Named at the call site rather
            # than defaulted in the repository, so two planes reading one
            # catalog cannot disagree about whose figures these are.
            totals = catalog.project_totals(tenant_id=LEGACY_TENANT_ID)
            recent = catalog.recent_runs(runs, tenant_id=LEGACY_TENANT_ID)
    except Exception as e:
        return {
            "catalog": {
                "available": False,
                "detail": f"{type(e).__name__}: {e}",
                "libraries": None,
                "documents": None,
                "absent_documents": None,
                "active_versions": None,
                "indexed_versions": None,
                "indexed_bytes": None,
            },
            # None, not []: "the catalog could not be read" and "nothing has run
            # yet" are different states with different fixes.
            "pages": {"available": False, "recorded": None, "of": None, "pages": None},
            "recent_runs": None,
        }

    return {
        "catalog": {
            "available": True,
            "detail": None,
            "libraries": totals.libraries,
            "documents": totals.documents,
            "absent_documents": totals.absent_documents,
            "active_versions": totals.active_versions,
            "indexed_versions": totals.indexed_versions,
            "indexed_bytes": totals.indexed_bytes,
        },
        # Nothing writes `page_count` today: `register_version` runs before the
        # document has been extracted, so the count is not knowable there. This
        # reports the measurement instead of a hardcoded absence, so the figure
        # appears by itself the day the column is filled in.
        "pages": {
            "available": totals.versions_with_pages > 0,
            "recorded": totals.versions_with_pages,
            "of": totals.indexed_versions,
            "pages": totals.pages if totals.versions_with_pages > 0 else None,
        },
        "recent_runs": [asdict(r) for r in recent],
    }


@app.get("/project-summary")
def project_summary(runs: int = 10) -> dict[str, Any]:
    """What the whole installation holds — the landing screen's only request.

    Two independent legs, each carrying its own availability, because this is
    the one endpoint that must answer while the stack is half up. A 503 here
    would be the first thing a user sees, and it would say nothing about the
    half that *was* readable.

    Not project-wide spend: money is shown where it is about to be committed —
    the approval gate — and in a run's own detail, where the price caveat sits
    beside it.
    """
    s = settings()
    summary = _catalog_totals(s, runs)
    summary["graph"] = _graph_totals(s)
    return summary


@app.get("/libraries")
def libraries() -> dict[str, Any]:
    """Which libraries exist, so the UI can offer them instead of guessing.

    Every screen used to open on a hardcoded `lib_1` that no installation has
    ever had, which made an empty shelf and an `off_corpus` answer the first
    thing a new user saw — both of them indistinguishable from a broken stack.
    A library id is not something a person can be expected to know or type.
    """
    s = settings()
    with Catalog(s.database_url) as catalog:
        # Single-tenant plane; see the note in the project summary above.
        rows = catalog.libraries(tenant_id=LEGACY_TENANT_ID)
    return {
        "libraries": [
            {
                "id": r["id"],
                "name": r["name"],
                "language": r["language"],
                "documents": r["documents"],
                # Only an indexed version can be retrieved from, so this — not
                # the document count — is what says a library can be asked.
                "indexed_versions": r["indexed_versions"],
            }
            for r in rows
        ]
    }


def _template_default(template_id: str, param: str) -> int:
    """The row cap a template declares, read back rather than repeated here.

    `truncated` is `len(rows) >= the cap`, and a cap written down twice is a cap
    that eventually disagrees with itself — at which point the response would
    say "complete" about a list the database had already cut short. Reading it
    from the registry makes the two impossible to separate.
    """
    for p in get(template_id).params:
        if p.name == param:
            assert isinstance(p.default, int), f"{template_id}.{param} is not an int"
            return p.default
    raise KeyError(f"{template_id} declares no parameter {param!r}")


_LIBRARY_DOCUMENT_LIMIT = _template_default("library_documents", "document_limit")
_LIBRARY_MENTION_LIMIT = _template_default("library_mentions", "mention_limit")
_LIBRARY_MIN_DOCUMENTS = _template_default("library_mentions", "min_documents")


@app.get("/libraries/{library_id}/graph")
def library_graph(
    library_id: str,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    min_documents: int = _LIBRARY_MIN_DOCUMENTS,
) -> dict[str, Any]:
    """Every projected book in a library and every concept it mentions.

    The whole dataset in one response, on purpose: the canvas that draws it
    needs to know the shape of the library before it can decide what to show,
    and paging would make "which books share this concept?" a question the
    client could only answer for the page it happens to hold.

    **The node list comes from the graph, not from the catalog.** A screen that
    draws the projection must list what the projection contains; taking the
    books from Postgres and the edges from Memgraph would let the two disagree
    without saying so, and the disagreement would render as a book with no
    concepts — indistinguishable from a book whose semantics were never
    extracted. A document indexed but never projected therefore does not appear
    here. The Library screen is the catalog's own view and remains so.

    Concepts need no query of their own: each is carried on the edge rows that
    reach it, so a concept with no mention above the floor is correctly absent
    rather than drawn as an unreachable dot.

    `min_documents` is the volume control and `confidence_floor` is not — see
    the measurement recorded on the template. `min_documents=1` returns every
    concept and is what a caller asks for when it wants the whole set; the
    default of 2 returns the subgraph that has edges between books at all.
    """
    documents = _explore("library_documents", {"library_id": library_id})
    mentions = _explore(
        "library_mentions",
        {
            "library_id": library_id,
            "confidence_floor": confidence_floor,
            # Clamped rather than rejected: 0 and -1 both mean "no filter", which
            # is what 1 already says, and a 400 for it would be pedantry.
            "min_documents": max(1, min_documents),
        },
    )

    # Folded here rather than in a third template: the rows already carry every
    # concept field, and a second traversal from `Concept` would have to be
    # scoped by library all over again — the leak `chunks_for_concepts` exists
    # to document.
    concepts: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for row in mentions:
        cid = row["concept_id"]
        seen = concepts.get(cid)
        if seen is None:
            concepts[cid] = {
                "id": cid,
                "name": row["name"],
                "concept_type": row["concept_type"],
                "mentions": row["mentions"],
                # The degree the *database* counted, not the number of rows that
                # survived the cap. A truncated response would otherwise report a
                # concept as narrower than it is, which is the one number a
                # reader would use to decide it does not connect two books.
                "documents": row["documents"],
            }
        else:
            seen["mentions"] += row["mentions"]
        edges.append(
            {
                "version_id": row["version_id"],
                "concept_id": cid,
                "mentions": row["mentions"],
                "confidence": row["confidence"],
            }
        )

    return {
        "library_id": library_id,
        # Every edge here rests on a `MENTIONS` a model proposed, so the whole
        # payload is declared semantic — the same contract the Explore routes
        # carry, and what the UI renders its "propuesto" badge from.
        "semantic": True,
        "confidence_floor": confidence_floor,
        "min_documents": max(1, min_documents),
        "documents": documents,
        # Ordered like the edges that produced them: most-mentioned first, so a
        # client that draws only part of the graph draws the part that matters.
        "concepts": list(concepts.values()),
        "edges": edges,
        # "At least this many", not a true total. Establishing the real count
        # costs a second traversal of the same pattern, and the caps are set well
        # above what a library of this size produces; a client that sees `true`
        # should say "showing N" and offer a higher floor, not a page number.
        "truncated": {
            "documents": len(documents) >= _LIBRARY_DOCUMENT_LIMIT,
            "edges": len(mentions) >= _LIBRARY_MENTION_LIMIT,
        },
    }


@app.get("/libraries/{library_id}/documents")
def documents(library_id: str, include_absent: bool = False) -> dict[str, Any]:
    """What the Library screen lists.

    `include_absent` is off by default: a file that disappeared keeps its history
    but should not clutter the shelf. Turning it on is how a user finds a
    document whose folder was unmounted, which is otherwise indistinguishable
    from one that was never imported.
    """
    s = settings()
    with Catalog(s.database_url) as catalog:
        rows = catalog.documents(library_id, include_absent=include_absent)
        active = {d.id: catalog.active_version(d.id) for d in rows}
    return {
        "library_id": library_id,
        "documents": [
            {
                "id": d.id,
                "title": d.title,
                "author": d.author,
                "format": d.format,
                "source_key": d.source_key,
                "present": d.present,
                "tags": list(d.tags),
                "active_version_id": active.get(d.id),
                "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            }
            for d in rows
        ],
    }


def _replayable(workspace: pathlib.Path, catalog: Catalog, run_id: str | None) -> bool:
    """Whether a rebuild of this run could actually read what it needs.

    The catalog row is not the question. `latest_run_with_artifact` proves a run
    *recorded* a `chunks` artifact; this proves the file is still where the row
    says, which is the difference between a disabled button and one that fails
    when pressed — and this endpoint exists to make that distinction.

    Two ways the row outlives the file, both seen: a pruned run directory
    (`infra/backup.sh` found 79 of them) and a catalog holding rows for runs
    written under a *different* workspace, which is what `rebuild` hit on
    2026-08-21 for every document indexed before the stack was pointed here.
    """
    if run_id is None:
        return False
    row = next(
        (a for a in catalog.artifacts(run_id) if a.get("name") == REQUIRED_ARTIFACT),
        None,
    )
    if row is None:
        return False
    return (workspace / row["rel_path"]).is_file()


@app.get("/libraries/{library_id}/documents/{document_id}")
def document_detail(library_id: str, document_id: str) -> dict[str, Any]:
    """One document, its versions, and which of the three verbs it can offer.

    The availability flags are computed here rather than left to the UI to
    infer, because both of them are catalog questions. A document imported
    before migration 003 has no `source_path` and cannot be re-indexed; a
    version whose run directory was pruned has no `chunks` artifact and cannot
    be rebuilt. Both are per-document states, and a button that fails when
    pressed is worse than one that says why it is disabled.
    """
    s = settings()
    with Catalog(s.database_url) as catalog:
        document = catalog.document(document_id, library_id=library_id)
        if document is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "kind": "document_not_found",
                    "message": f"no existe {document_id!r} en {library_id!r}",
                },
            )
        versions = catalog.versions_of(document_id)
        active = catalog.active_version(document_id)
        rebuild_from = {
            v.id: catalog.latest_run_with_artifact(v.id, REQUIRED_ARTIFACT)
            for v in versions
        }
        shared = {v.id: catalog.documents_holding(v.id) for v in versions}
        # The newest run of each version that measured anything, and what it
        # measured. A version indexed before the stage existed — which is every
        # version on this installation today — simply has none, and renders as
        # "not measured" rather than as a zero.
        scored_by = {
            v.id: catalog.latest_run_with_artifact(v.id, SCORES_ARTIFACT)
            for v in versions
        }
        scores = {
            vid: _measured_scores(s.workspace, run, catalog.artifacts(run))
            for vid, run in scored_by.items()
            if run
        }
        # Computed inside the `with`: the check reads the artifact row, and the
        # catalog is closed by the time the response below is assembled.
        can_rebuild = _replayable(s.workspace, catalog, rebuild_from.get(active))

    return {
        "id": document.id,
        "library_id": document.library_id,
        "title": document.title,
        "author": document.author,
        "format": document.format,
        "source_key": document.source_key,
        "source_path": document.source_path,
        "present": document.present,
        "tags": list(document.tags),
        "active_version_id": active,
        "can_reindex": bool(document.source_path),
        "can_rebuild": can_rebuild,
        "versions": [
            {
                "id": v.id,
                "content_sha256": v.content_sha256,
                "byte_size": v.byte_size,
                "page_count": v.page_count,
                "state": v.state,
                "active": v.id == active,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "rebuild_run_id": rebuild_from.get(v.id),
                # None means nobody measured this version, never "it scored 0".
                "scores": scores.get(v.id),
                # Which other documents hold these same bytes. Removing this
                # document leaves the version standing when this is non-empty,
                # and the confirm dialog has to be able to say so.
                "also_held_by": [d for d in shared.get(v.id, []) if d != document_id],
            }
            for v in versions
        ],
    }


@app.delete("/libraries/{library_id}/documents/{document_id}")
async def remove_document(library_id: str, document_id: str) -> dict[str, Any]:
    """Permanent removal, across Qdrant, Memgraph and the catalog, in that order.

    A threadpool rather than a workflow, the same shape `/ask` uses and for the
    same reason it gives: this is interactive and short-lived, and durability
    buys nothing when the remedy for a failure is to press the button again —
    which `removal`'s ordering makes safe.

    Nothing here deletes the file on disk.
    """
    import asyncio

    from ..removal import RemovalError, remove_document as run_removal

    s = settings()
    try:
        result = await asyncio.to_thread(
            run_removal, s, library_id=library_id, document_id=document_id
        )
    except RemovalError as e:
        raise HTTPException(
            status_code=404, detail={"kind": e.kind, "message": str(e)}
        ) from e
    return result.as_dict()


@app.delete("/libraries/{library_id}/versions/{version_id}")
async def remove_version(library_id: str, version_id: str) -> dict[str, Any]:
    """Remove one version, leaving its document and its other versions standing.

    Removing the active version leaves the document with no active version
    rather than silently promoting an older one: which version a citation refers
    to must have exactly one answer, and picking a replacement the user did not
    choose is not that answer.
    """
    import asyncio

    from ..removal import RemovalError, remove_version as run_removal

    s = settings()
    try:
        result = await asyncio.to_thread(
            run_removal, s, library_id=library_id, version_id=version_id
        )
    except RemovalError as e:
        raise HTTPException(
            status_code=404, detail={"kind": e.kind, "message": str(e)}
        ) from e
    return result.as_dict()


@app.post("/libraries/{library_id}/versions/{version_id}/activate")
async def activate_version_route(library_id: str, version_id: str) -> dict[str, Any]:
    """Promote a version the pipeline deliberately did not.

    An ingest that finds a structural collision indexes everything and withholds
    only this step, because the fingerprint that selects a family profile is
    structural and structure is not subject matter — and the retrieval metrics
    provably cannot see the difference, since the eval questions come from the
    very chunks the wrong rules produced. So the judgement is a person's, and
    this is the half of it that says "the rules were right".

    The other half is re-importing with `ignore_profile`, which says they were
    wrong and costs another pipeline run. This costs nothing: the index it
    promotes is the one already paid for.
    """
    import asyncio

    from ..activation import ActivationError, activate_version

    try:
        result = await asyncio.to_thread(
            activate_version,
            library_id,
            version_id,
            # Named rather than defaulted, the same decision `reindex_document`
            # records: this plane *is* the legacy organisation, it has no
            # accounts, and saying so is what keeps the value from being an
            # accident. It was an accident until 2026-08-31 — the parameter
            # defaulted, so this route answered 404 for every version any other
            # organisation owned, which is every version on this installation
            # since the corpus moved to `preprod`. The paid plane reaches the
            # same function through `ActivationWorkflow`, carrying its own.
            tenant_id=LEGACY_TENANT_ID,
        )
    except ActivationError as e:
        raise HTTPException(
            status_code=404, detail={"kind": e.kind, "message": str(e)}
        ) from e
    return result.as_dict()


@app.post("/libraries/{library_id}/documents/{document_id}/reindex")
async def reindex_document(
    library_id: str, document_id: str, options: StageOptions | None = None
) -> dict[str, Any]:
    """Re-run the whole ingest pipeline on this document's source file.

    Deliberately the normal workflow, arriving at the normal free gate: a
    re-index re-quotes before it spends, and every stage stays individually
    switchable there. Putting it behind a bare "are you sure" instead would be
    the one place in the product where money is spent without an estimate.
    """
    s = settings()
    with Catalog(s.database_url) as catalog:
        document = catalog.document(document_id, library_id=library_id)
        if document is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "kind": "document_not_found",
                    "message": f"no existe {document_id!r} en {library_id!r}",
                },
            )
        if not document.source_path:
            raise HTTPException(
                status_code=409,
                detail={
                    "kind": "source_path_unknown",
                    "message": (
                        f"{document.title!r} se importó antes de que el catálogo "
                        "registrara la ruta de origen; vuelve a importarlo desde "
                        "el archivo para poder reindexarlo"
                    ),
                },
            )

    if document.format == TIMED_FORMAT:
        # A video's `source_path` is its URL, not a path. Handing it to
        # `IngestWorkflow` puts it through `stage_source`, which checks tenant
        # containment on a filesystem path and dies with
        # `'https://youtu.be/…' is outside this organisation's workspace` —
        # three frames from the cause, on a button whose whole purpose is to
        # re-run what already worked once.
        video = VideoRequest(
            library_id=library_id,
            url=document.source_path,
            title=document.title,
            author=document.author,
            reindex=True,
            tenant_id=LEGACY_TENANT_ID,
        )
        client = await temporal()
        handle = await client.start_workflow(
            VideoIngestWorkflow.run,
            args=[video, options or StageOptions()],
            id=f"video-{_ulid()}",
            task_queue=s.task_queue,
            memo=_OWNED_BY_LEGACY,
        )
        return {"workflow_id": handle.id, "state": "running", "kind": "video"}

    request = IngestRequest(
        library_id=library_id,
        source_path=document.source_path,
        source_key=document.source_key,
        title=document.title,
        author=document.author,
        folder_id=document.folder_id,
        # Without this the workflow short-circuits on `already_indexed`, which
        # is the whole point of the button.
        reindex=True,
        # Named rather than defaulted, for the reason the rebuild path learned
        # the hard way: `project_structure` reads the tenant off this request,
        # and a request that leaves it out projects into the legacy
        # organisation silently. This plane *is* that organisation, so here the
        # value is right — saying it is what makes that a decision.
        tenant_id=LEGACY_TENANT_ID,
    )
    client = await temporal()
    handle = await client.start_workflow(
        IngestWorkflow.run,
        args=[request, options or StageOptions()],
        id=f"reindex-{_ulid()}",
        task_queue=s.task_queue,
        memo=_OWNED_BY_LEGACY,
    )
    return {"workflow_id": handle.id, "state": "running", "kind": "reindex"}


@app.post("/libraries/{library_id}/documents/{document_id}/rebuild")
async def rebuild_document(library_id: str, document_id: str) -> dict[str, Any]:
    """Replay this version's artifacts into Qdrant and Memgraph.

    Not "re-index with correction off". Correction runs before chunking because
    it changes the text's length, so re-running without it produces different
    `char_span`s and different chunk boundaries — a silently different index.
    Reproducing what was paid for means replaying `chunks.jsonl`.
    """
    s = settings()
    client = await temporal()
    handle = await client.start_workflow(
        RebuildWorkflow.run,
        args=[library_id, document_id],
        id=f"rebuild-{_ulid()}",
        task_queue=s.task_queue,
        memo=_OWNED_BY_LEGACY,
    )
    return {"workflow_id": handle.id, "state": "running", "kind": "rebuild"}


@app.get("/runs/{workflow_id}/rebuild-gate")
async def rebuild_gate(workflow_id: str) -> dict[str, Any]:
    """The rebuild's one-question gate.

    A separate route from `/runs/{id}/gate` even though both workflows name the
    query `gate_report`. The shapes differ — a rebuild has one stage that can
    spend, not a table of them — and querying through the ingest workflow's
    typed handle would decode a `RebuildReport` into a `GateReport`, dropping
    every field the two do not share without failing.
    """
    handle = (await temporal()).get_workflow_handle(workflow_id)
    try:
        report = await handle.query(RebuildWorkflow.gate_report)
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail={"kind": "run_not_found", "message": f"{type(e).__name__}: {e}"},
        ) from e
    if report is None:
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "gate_not_ready",
                "message": "todavía se están leyendo los artefactos",
                "stage": await handle.query(RebuildWorkflow.stage),
            },
        )
    return asdict(report)


# ---------------------------------------------------------------------------
# Explore
# ---------------------------------------------------------------------------
#
# Six endpoints, one per thing a person can look at, each naming its template as
# a literal. **No route accepts a template id or Cypher from its caller.** The
# planner's contract is a template id plus typed parameters precisely because
# Memgraph does not enforce read-only — a `CREATE` inside a `default_access_mode
# ="READ"` session succeeds on 3.12.0 — and the guarantee is structural. Letting
# the webview choose the template would put a string from the UI one validator
# away from the database and buy nothing: these six are the whole surface.


def _explore(template_id: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one registered template and hand back plain rows.

    The tenant is injected here rather than taken from the caller, and that is
    the whole tenancy story of this plane: it is the free, self-managed one, it
    has no accounts, and everything it can reach belongs to the tenant every
    pre-tenancy row already carries. A route that accepted an organisation would
    be offering a choice this product does not have.

    `Graph.query` validates the template and binds the parameters, which is where
    the limit clamp and the type checks live. A graph that is simply down is a
    503 naming the fix, not a 500: the answer is "start the stack", and a UI that
    can say so beats one echoing a Bolt error.
    """
    s = settings()
    args = {**args, "tenant_id": LEGACY_TENANT_ID}
    try:
        # Validated *before* connecting, and the order is the point. `Graph.query`
        # binds after opening a session, so a malformed id sent while Memgraph
        # happened to be down came back as "graph unreachable" — the wrong
        # diagnosis, pointing the user at the stack instead of at their stale
        # link. Binding here is idempotent; `query` does it again on the way in.
        bind(get(template_id), args)
    except TemplateError as e:
        # The path parameter did not have the shape of a graph id. That check is
        # the same one that stops a planner's output becoming query syntax, and
        # it applies here because these parameters arrive from the webview. A
        # 400 rather than a 500: the caller sent something wrong, and a UI that
        # links to a stale id should be told so rather than shown a server
        # error.
        raise HTTPException(
            status_code=400,
            detail={"kind": "bad_identifier", "message": str(e)},
        ) from e

    try:
        with Graph(s.memgraph_url) as graph:
            return [dict(row.data) for row in graph.query(template_id, args)]
    except GraphError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "kind": "graph_unreachable",
                "message": f"No se pudo consultar el grafo: {e}",
            },
        ) from e


# ---------------------------------------------------------------------------
# Conversations
#
# The same two-clock split `/ask` records above, plus the half a conversation
# adds: **the answer is written in the worker and read in the API**, in another
# process. `POST /chat/{id}/turn` claims a turn number and signals; the prose
# arrives through `conversation_delta`, which the activity writes and the stream
# route reads.
#
# A table rather than a notification because it replays. `?since=N` is what makes
# a reader who reloads, or whose connection drops, resume from where they were
# instead of from nothing — which is the same failure the `/ask` split exists to
# prevent, arriving from the streaming direction.
#
# `LEGACY_TENANT_ID` is named at every call site here. This plane *is* the free,
# self-managed, single-tenant one, and saying so is what makes it a decision
# rather than a default that travelled.


@dataclass
class NewConversation:
    library_id: str


@dataclass
class NewTurn:
    """One message. The cap is a *field constraint* on purpose.

    Both planes then answer an oversized body with FastAPI's own
    422-with-a-list, the same decision `Question.effort` and
    `AnswerStyleUpdate.body` already record — a hand-raised 400 carrying a `kind`
    would make the two planes answer one bad request two different ways.
    """

    text: Annotated[str, Field(max_length=MAX_MESSAGE_CHARS)]
    effort: Literal["brief", "standard", "thorough"] = DEFAULT_EFFORT


def _turn_json(turn: Any) -> dict[str, Any]:
    return {
        "seq": turn.seq,
        "question": turn.question,
        "searched": turn.searched,
        "answer": turn.answer,
        "state": turn.state,
        "effort": turn.effort,
        "style_effort": turn.style_effort,
        "citations": turn.citations,
        "cited_evidence": turn.cited_evidence,
        "error": turn.error,
        "asked_at": turn.asked_at.isoformat() if turn.asked_at else None,
        "answered_at": turn.answered_at.isoformat() if turn.answered_at else None,
    }


def _conversation_json(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "library_id": row.library_id,
        "title": row.title,
        "title_generated": row.title_generated,
        "turns": row.turns,
        "created_at": row.created_at.isoformat(),
        "last_message_at": row.last_message_at.isoformat(),
    }


def _unreachable(e: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "kind": "catalog_unreachable",
            "message": f"No se pudo leer el catálogo ({type(e).__name__}: {e}).",
        },
    )


_NO_CONVERSATION = HTTPException(
    status_code=404,
    detail={
        "kind": "conversation_not_found",
        "message": "Esa conversación no existe.",
    },
)


@app.post("/chat")
async def create_conversation(body: NewConversation) -> dict[str, Any]:
    """Open a conversation. Must precede any turn.

    Not an optimisation: `conversation_turn` and `conversation_delta` derive
    their tenant in the INSERT from this row, so a turn signalled for a
    conversation that does not exist would insert nothing at all — silently, the
    same way an event written before its run row is silently dropped.
    """
    s = settings()
    conversation_id = f"cnv_{uuid4().hex[:24]}"
    try:
        with Catalog(s.database_url, pooled=False) as catalog:
            catalog.start_conversation(
                conversation_id,
                tenant_id=LEGACY_TENANT_ID,
                library_id=body.library_id,
                title="",
            )
    except Exception as e:
        raise _unreachable(e) from e
    return {"conversation_id": conversation_id, "library_id": body.library_id}


@app.get("/chat")
async def list_conversations(limit: int = 50) -> dict[str, Any]:
    s = settings()
    try:
        with Catalog(s.database_url, pooled=False) as catalog:
            rows = catalog.conversations(
                tenant_id=LEGACY_TENANT_ID, limit=max(1, min(limit, 200))
            )
    except Exception as e:
        raise _unreachable(e) from e
    return {"conversations": [_conversation_json(r) for r in rows]}


@app.get("/chat/{conversation_id}")
async def read_conversation(conversation_id: str) -> dict[str, Any]:
    """The whole transcript, which is what survives Temporal's retention."""
    s = settings()
    try:
        with Catalog(s.database_url, pooled=False) as catalog:
            row = catalog.conversation(conversation_id, tenant_id=LEGACY_TENANT_ID)
            if row is None:
                raise _NO_CONVERSATION
            turns = catalog.turns(conversation_id, tenant_id=LEGACY_TENANT_ID)
    except HTTPException:
        raise
    except Exception as e:
        raise _unreachable(e) from e
    return {**_conversation_json(row), "turns_detail": [_turn_json(t) for t in turns]}


@app.delete("/chat/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str) -> None:
    s = settings()
    try:
        with Catalog(s.database_url, pooled=False) as catalog:
            if not catalog.delete_conversation(
                conversation_id, tenant_id=LEGACY_TENANT_ID
            ):
                raise _NO_CONVERSATION
    except HTTPException:
        raise
    except Exception as e:
        raise _unreachable(e) from e


@app.post("/chat/{conversation_id}/turn")
async def add_turn(conversation_id: str, body: NewTurn) -> dict[str, Any]:
    """Claim a turn number and hand the question to the conversation's session.

    **Signal-with-start**, so this is one call whether or not a session is live.
    A conversation whose workflow has gone dormant — or aged out of Temporal
    entirely — is resumed here from the catalog, which is what makes the record
    outlive the retention that bounds the session.

    The sequence number is claimed *before* the signal, in the same statement
    that checks who owns the conversation, so the signal carries an id the
    workflow can be idempotent on: a duplicated delivery names a turn already
    queued and is dropped rather than asked and billed twice.
    """
    s = settings()
    if not s.gemini.configured:
        raise HTTPException(
            status_code=503,
            detail={
                "kind": "provider_unconfigured",
                "message": "Falta BRAIN_GEMINI_PROJECT_ID: no se puede conversar.",
            },
        )

    try:
        with Catalog(s.database_url, pooled=False) as catalog:
            row = catalog.conversation(conversation_id, tenant_id=LEGACY_TENANT_ID)
            if row is None:
                raise _NO_CONVERSATION

            # The window the session starts from, if this is the one that starts
            # it. Only settled turns: a turn still running has no answer to
            # resolve a pronoun against, and a failed one has none either.
            history = [
                TurnRecord(question=t.question, answer=t.answer[:WINDOW_ANSWER_CHARS])
                for t in catalog.turns(
                    conversation_id, tenant_id=LEGACY_TENANT_ID, limit=WINDOW_TURNS * 2
                )
                if t.answer
            ][-WINDOW_TURNS:]

            seq = catalog.open_turn(
                conversation_id,
                tenant_id=LEGACY_TENANT_ID,
                question=body.text,
                effort=body.effort,
            )
            if seq is None:
                raise _NO_CONVERSATION

            # The first question names the conversation until `chat-title` does.
            # Written here rather than at creation because the title is the first
            # *question*, and at creation there is not one yet.
            if seq == 1:
                catalog.set_conversation_title(
                    conversation_id,
                    fallback_title(body.text),
                    tenant_id=LEGACY_TENANT_ID,
                    generated=False,
                )
    except HTTPException:
        raise
    except Exception as e:
        raise _unreachable(e) from e

    turn = ChatTurn(
        conversation_id=conversation_id,
        turn_seq=seq,
        text=body.text,
        library_id=row.library_id,
        tenant_id=LEGACY_TENANT_ID,
        effort=body.effort,
    )
    client = await temporal()
    await client.start_workflow(
        ChatWorkflow.run,
        ChatStart(
            conversation_id=conversation_id,
            library_id=row.library_id,
            tenant_id=LEGACY_TENANT_ID,
            window=history,
            answered=row.turns,
        ),
        id=f"chat-{conversation_id}",
        task_queue=s.task_queue,
        memo=_OWNED_BY_LEGACY,
        start_signal="ask",
        start_signal_args=[turn],
    )
    return {"conversation_id": conversation_id, "turn_seq": seq, "state": "running"}


#: How often the stream route looks for new prose. Fast enough that text reads as
#: arriving rather than appearing, slow enough that a turn taking a minute costs
#: a few hundred cheap indexed reads on one pooled connection.
STREAM_POLL_SECONDS = 0.2

#: A stream never outlives the turn it is following by more than this. The
#: activity's own ceiling is `TURN_TIMEOUT`; this is the backstop for a turn whose
#: row never settles at all — a worker killed mid-answer, say — so a generator
#: cannot be left running for the lifetime of the process.
STREAM_MAX_SECONDS = 20 * 60

#: How long the wire may stay silent before an empty `ping` event is sent.
#:
#: **The stream is legitimately silent for most of a turn, and that silence used
#: to be indistinguishable from a dead connection.** Nothing is emitted between
#: `generating` and the first prose, because reasoning tokens produce no text and
#: `FieldStreamer` withholds a tail on top of that — measured against Vertex on
#: 2026-09-06: 8.4s on a `brief` turn and 11s on two `standard` ones, with the
#: whole turn taking 10s and 16s. A client cannot tell that apart from a stalled
#: upstream, and one really did stall: a `standard` turn opened its
#: `streamGenerateContent` stream, produced not one chunk, and was still open 15
#: minutes later when Temporal's `start_to_close` closed the activity. The
#: desktop app gave up at its own 120s idle budget and told the person the turn
#: "could not be answered", of a turn that was still running.
#:
#: So the client's idle budget must measure the *connection*, which is what it is
#: for, and this is what lets it: a ping is a row-less event, costs no query and
#: no tokens, and needs no client change — both clients already treat an
#: unrecognised `type` as data rather than as a failure, which is the reason that
#: decision was taken. Well under any client budget: `CHAT_STREAM_IDLE` is 120s.
STREAM_PING_SECONDS = 10.0


def _sse(payload: dict[str, Any]) -> str:
    """One event. No `event:` name — the type is a field, so one handler reads
    every event and an unknown one is data rather than a dropped message."""
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@app.get("/chat/{conversation_id}/turn/{turn_seq}/stream")
async def stream_turn(
    conversation_id: str, turn_seq: int, since: int = 0
) -> StreamingResponse:
    """The answer, as it is written.

    `since` is the resume point: pass the highest chunk sequence already seen and
    only what follows it is sent. A reader who never disconnects passes 0 once.

    Ends on the turn's own row settling, not on the relay going quiet — a model
    that pauses mid-answer is not a model that has finished. The terminal `done`
    event carries the whole settled turn, and **it is authoritative**: the prose
    that streamed is a draft of one field, and a turn can stream fluently and
    still come back `insufficient_evidence` because the citations, which arrive
    last in the envelope, did not survive verification. A client must replace
    what it showed rather than keep it.

    A disconnected client stops the stream and **does not stop the turn**.
    Generation is one call and is billed for what it produced, so cancelling
    refunds nothing and would reproduce exactly the failure `/ask` split its two
    calls to avoid: an answer computed, billed, and thrown away.
    """
    s = settings()

    async def events():
        deadline = time.monotonic() + STREAM_MAX_SECONDS
        cursor = since
        # Tracked rather than derived from the loop count: the ping's whole
        # purpose is to say "the connection is alive" when nothing else has, so
        # it has to be reset by every *real* event too, not only by another ping.
        last_sent = time.monotonic()
        try:
            # Pooled, unlike every other read here: this one connection is used
            # a few hundred times over a turn, and an unpooled catalog opens a
            # fresh connection per statement.
            with Catalog(s.database_url, min_size=1, max_size=2) as catalog:
                while True:
                    for row in catalog.relay(
                        conversation_id, turn_seq,
                        tenant_id=LEGACY_TENANT_ID, since=cursor,
                    ):
                        cursor = int(row["chunk_seq"])
                        # One ordered stream, two kinds. A row whose kind is not
                        # `token` is a stage, and its name goes on the wire as
                        # the stage rather than as a fourth event type per
                        # stage — so a stage added in the worker reaches an
                        # older client as a `stage` event it can render or
                        # ignore, instead of as an unknown event type.
                        if row["kind"] == "token":
                            yield _sse({
                                "type": "token",
                                "seq": cursor,
                                "text": row["text"],
                            })
                        else:
                            yield _sse({
                                "type": "stage",
                                "seq": cursor,
                                "stage": row["kind"],
                                **(row["detail"] or {}),
                            })
                        last_sent = time.monotonic()

                    settled = next(
                        (
                            t
                            for t in catalog.turns(
                                conversation_id, tenant_id=LEGACY_TENANT_ID
                            )
                            if t.seq == turn_seq and t.state != "running"
                        ),
                        None,
                    )
                    if settled is not None:
                        yield _sse({"type": "done", "turn": _turn_json(settled)})
                        return
                    if time.monotonic() > deadline:
                        yield _sse({
                            "type": "error",
                            "kind": "turn_abandoned",
                            "message": (
                                "El turno no terminó dentro del tiempo previsto; "
                                "vuelve a abrir la conversación para ver su estado."
                            ),
                        })
                        return
                    now = time.monotonic()
                    if now - last_sent >= STREAM_PING_SECONDS:
                        # Carries no sequence: it is not a position in the relay
                        # and a client must never advance `since` past it.
                        yield _sse({"type": "ping"})
                        last_sent = now
                    await asyncio.sleep(STREAM_POLL_SECONDS)
        except asyncio.CancelledError:
            # The reader went away. The turn keeps going and lands in the
            # catalog; there is nothing to clean up and nothing to refund.
            raise
        except Exception as e:
            # Never a 500 mid-flight: the response has already begun, so the only
            # way to say what happened is to say it in the stream.
            yield _sse({
                "type": "error",
                "kind": "catalog_unreachable",
                "message": f"{type(e).__name__}: {e}",
            })

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/answer-styles")
def answer_styles() -> dict[str, Any]:
    """How this organisation words an answer at each effort level.

    Returns every level, always, with the text that would actually be used —
    the organisation's override where there is one and the built-in default
    where there is not — plus `custom`, which is what lets the screen show a
    "restore the default" affordance only where it would do something.

    `LEGACY_TENANT_ID` is named at the call site rather than defaulted, the way
    every other listing on this plane names it: this plane *is* that
    organisation, and saying so is what makes it a decision.
    """
    from ..answering.effort import BUDGETS, EFFORT_LEVELS

    try:
        with Catalog(settings().database_url, pooled=False) as catalog:
            saved = catalog.answer_styles(tenant_id=LEGACY_TENANT_ID)
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={"kind": "catalog_unreachable", "message": str(e)},
        ) from e

    return {
        "levels": [
            {
                "effort": name,
                "body": saved.get(name, BUDGETS[name].style),
                "default_body": BUDGETS[name].style,
                "custom": name in saved,
            }
            for name in EFFORT_LEVELS
        ],
        "max_chars": MAX_STYLE_CHARS,
    }


@app.put("/answer-styles/{effort}")
def set_answer_style(effort: str, payload: AnswerStyleUpdate) -> dict[str, Any]:
    """Override one level's wording, or clear the override.

    An empty body clears it, so "restore the default" is the same request as
    "save an empty box" and the two cannot drift into different states.

    The level is validated against the table rather than trusted: a row written
    for a level that does not exist would be invisible — reads are keyed by the
    level being asked for — so it would look like a save that silently did
    nothing.
    """
    from ..answering.effort import EFFORT_LEVELS

    if effort not in EFFORT_LEVELS:
        raise HTTPException(
            status_code=404,
            detail={
                "kind": "effort_not_found",
                "message": f"no existe el nivel {effort!r}",
            },
        )
    try:
        with Catalog(settings().database_url, pooled=False) as catalog:
            catalog.set_answer_style(
                effort, payload.body, tenant_id=LEGACY_TENANT_ID
            )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={"kind": "catalog_unreachable", "message": str(e)},
        ) from e
    return {"effort": effort, "custom": bool(payload.body.strip())}


@app.get("/versions/{version_id}/outline")
def outline(version_id: str, limit: int = 200) -> dict[str, Any]:
    """The document's table of contents.

    Deterministic — derived from the document's own headings, not proposed by a
    model — which is why nothing here carries a confidence and the UI marks it
    differently from the concept panes.
    """
    return {
        "version_id": version_id,
        "sections": _explore("document_outline", {"version_id": version_id, "limit": limit}),
    }


@app.get("/sections/{section_id}/chunks")
def section_chunks(section_id: str, limit: int = 50) -> dict[str, Any]:
    """The chunks under one section, in reading order.

    The default limit is well below the registry's clamp of 200: chunks carry
    their text, and 200 of them is a few hundred kilobytes of JSON for a pane
    showing a dozen.
    """
    return {
        "section_id": section_id,
        "chunks": _explore("section_chunks", {"section_id": section_id, "limit": limit}),
    }


@app.get("/chunks/{chunk_id}/context")
def chunk_context(chunk_id: str) -> dict[str, Any]:
    """One chunk with its neighbours and its verifiable locator.

    Both halves together because they answer one question — "what am I looking
    at, and where does it come from in the original?" — and a citation the user
    cannot open is not a citation.
    """
    rows = _explore("chunk_neighbours", {"chunk_id": chunk_id, "limit": 1})
    citations = _explore("citations_for_chunks", {"chunk_ids": [chunk_id], "limit": 1})
    return {
        "chunk_id": chunk_id,
        "context": rows[0] if rows else None,
        "citation": citations[0] if citations else None,
    }


@app.get("/versions/{version_id}/concepts")
def version_concepts(
    version_id: str, confidence_floor: float = 0.6, limit: int = 50
) -> dict[str, Any]:
    """Concepts this version mentions. **Model-proposed, and labelled as such.**

    `concepts_in_version` declares `uses_semantic_edges`, and that flag exists
    because an edge a model proposed and an edge derived from the document's own
    table of contents have different standing as evidence. The response repeats
    the declaration so the UI has something to render the difference from rather
    than having to know which endpoint it called.
    """
    return {
        "version_id": version_id,
        "semantic": True,
        "confidence_floor": confidence_floor,
        "concepts": _explore(
            "concepts_in_version",
            {"version_id": version_id, "confidence_floor": confidence_floor, "limit": limit},
        ),
    }


@app.get("/versions/{version_id}/related")
def related_documents(
    version_id: str, confidence_floor: float = 0.6, limit: int = 25
) -> dict[str, Any]:
    """Other documents sharing concepts with this one — the graph's reason to
    exist, and also model-proposed."""
    return {
        "version_id": version_id,
        "semantic": True,
        "confidence_floor": confidence_floor,
        "documents": _explore(
            "related_documents",
            {"version_id": version_id, "confidence_floor": confidence_floor, "limit": limit},
        ),
    }


@app.get("/concepts/{concept_id}/claims")
def concept_claims(
    concept_id: str, confidence_floor: float = 0.6, limit: int = 50
) -> dict[str, Any]:
    """Claims about one concept, each naming the chunk it came from.

    `source_chunk_id` is not decoration: a relation nobody can check is worse
    than no relation, because it still looks like evidence. Every claim here is
    traceable back to a chunk the reader can open.
    """
    return {
        "concept_id": concept_id,
        "semantic": True,
        "confidence_floor": confidence_floor,
        "claims": _explore(
            "claims_about_concept",
            {"concept_id": concept_id, "confidence_floor": confidence_floor, "limit": limit},
        ),
    }
