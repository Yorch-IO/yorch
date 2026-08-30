"""The control plane the desktop app talks to.

Bound to loopback by the compose port mapping and reached only through Rust,
which proxies each call as an explicit Tauri command. There is no
authentication because there is no path to this port from off the machine —
that assumption is enforced by the `127.0.0.1:` prefix on every published port
in docker-compose.yaml, and breaking it would need to be a deliberate act.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from temporalio.client import Client

from .. import config
from ..artifacts import ArtifactRef, ArtifactStore
from ..catalog import Catalog, MigrationError, current_version, require_schema
from ..graph import DEFAULT_CONFIDENCE_FLOOR, Graph, GraphError
from ..graph.queries import TemplateError, bind, get
from ..graph.schema import LEGACY_TENANT_ID, SEMANTIC_EDGES
from ..activities.rebuild import REQUIRED_ARTIFACT
from ..answering import Question, ask
from ..pipeline import SUPPORTED_FORMATS, IngestRequest, StageOptions
from ..workflows.ask import AskWorkflow
from ..workflows.ingest import Approval, IngestWorkflow
from ..workflows.rebuild import RebuildWorkflow
from ..workflows.ping import PingWorkflow

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 5.0

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


async def _run_state(handle: Any) -> str | None:
    """Whether the run is still going, or what it ended as.

    `stage` cannot answer this. A query against a *failed* workflow hands back
    the last stage it recorded, which is indistinguishable from one still
    working — observed 2026-08-28, when an ingest whose activity retries were
    exhausted reported `"stage": "learning"` indefinitely while
    `describe().status` already read FAILED.

    `None` means Temporal has forgotten the run, which is ordinary once
    retention expires and is not a failure: the catalog still holds what the run
    produced. Swallowed for the same reason the stage query is.
    """
    try:
        status = (await handle.describe()).status
    except Exception:
        return None
    return status.name.lower() if status is not None else None

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


@app.get("/runs/{workflow_id}")
async def run_status(workflow_id: str) -> dict[str, Any]:
    """Live stage from Temporal, plus what the catalog recorded.

    Both, because they answer different questions: Temporal knows where the run
    is *now*, and the catalog knows what it produced and what it cost — which
    outlives the workflow's retention period.
    """
    s = settings()
    handle = (await temporal()).get_workflow_handle(workflow_id)
    try:
        stage = await handle.query(IngestWorkflow.stage)
    except Exception:
        stage = None

    with Catalog(s.database_url) as catalog:
        artifacts = catalog.artifacts(workflow_id)
        costs = catalog.total_cost(workflow_id)

    body: dict[str, Any] = {
        "workflow_id": workflow_id,
        "stage": stage,
        "state": await _run_state(handle),
        "artifacts": artifacts,
        "cost": costs,
    }
    # Omitted rather than zeroed when there is nothing to count: "this run
    # extracted no semantics" and "0 of 0 claims are verifiable" are different
    # statements, and only one of them is true of a structure-only run.
    if (semantics := _semantics_counts(s.workspace, workflow_id, artifacts)) is not None:
        body["semantics"] = semantics
    return body


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
