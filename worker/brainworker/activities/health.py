"""Reachability probes for the backing services.

These are activities rather than plain functions because the walking skeleton
has to prove the whole path works: the API starts a workflow, Temporal schedules
it onto the worker, and the worker reaches Qdrant and Postgres. A probe that ran
inside the API would prove none of that.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import psycopg
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import config
from ..providers import Provider, ProviderError

PROBE_TIMEOUT = 5.0


@dataclass
class ServiceProbe:
    service: str
    ok: bool
    detail: str


@dataclass
class ProbeReport:
    probes: list[ServiceProbe]

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.probes)


@activity.defn(name="probe_services")
async def probe_services() -> ProbeReport:
    settings = config.load()
    return ProbeReport(
        probes=[
            _probe_qdrant(settings),
            _probe_memgraph(settings),
            _probe_postgres(settings),
        ]
    )


def _probe_qdrant(settings: config.Settings) -> ServiceProbe:
    try:
        r = httpx.get(f"{settings.qdrant_url}/readyz", timeout=PROBE_TIMEOUT)
        if r.status_code != 200:
            return ServiceProbe("qdrant", False, f"/readyz returned {r.status_code}")
        collections = httpx.get(
            f"{settings.qdrant_url}/collections", timeout=PROBE_TIMEOUT
        ).json()
        names = [c["name"] for c in collections.get("result", {}).get("collections", [])]
        return ServiceProbe("qdrant", True, f"ready, {len(names)} collection(s)")
    except Exception as e:  # a probe reports failure, it does not raise
        return ServiceProbe("qdrant", False, f"{type(e).__name__}: {e}")


def _probe_memgraph(settings: config.Settings) -> ServiceProbe:
    try:
        # Opening a driver is cheap but not free, and this runs on every probe
        # rather than being cached, on purpose: a cached driver that had lost
        # its connection would report the graph healthy right up until a real
        # query failed.
        from ..graph import Graph

        with Graph(settings.memgraph_url, timeout=PROBE_TIMEOUT) as graph:
            return ServiceProbe("memgraph", True, graph.probe())
    except Exception as e:
        return ServiceProbe("memgraph", False, f"{type(e).__name__}: {e}")


def _probe_postgres(settings: config.Settings) -> ServiceProbe:
    try:
        with psycopg.connect(settings.database_url, connect_timeout=int(PROBE_TIMEOUT)) as conn:
            with conn.cursor() as cur:
                cur.execute("select version()")
                row = cur.fetchone()
        version = (row[0] if row else "").split(" on ")[0]
        return ServiceProbe("postgres", True, version or "connected")
    except Exception as e:
        return ServiceProbe("postgres", False, f"{type(e).__name__}: {e}")


@activity.defn(name="probe_provider")
async def probe_provider() -> dict[str, str]:
    """Prove the credentials, the project and the models all work.

    Separate from `probe_services` and deliberately not part of `/health`,
    because this one **spends**: it is one short embedding, the cheapest request
    Vertex bills for, and a health check that costs real money is one people
    turn off. It runs when a person presses a button.

    An activity rather than a call from the control plane, for the reason the
    whole product is arranged this way: Vertex is reached with Application
    Default Credentials mounted into the worker, and Gemini Enterprise rejects
    API keys outright — so the credential lives here and travels nowhere.
    """
    import asyncio

    settings = config.load()
    try:
        detail = await asyncio.to_thread(Provider(settings.gemini).probe)
    except ProviderError as e:
        # The `kind` is what the desktop app keys its advice on — "the project
        # is unset" and "the quota is exhausted" have different fixes — so it
        # crosses the boundary in `details[0]` rather than being flattened.
        raise ApplicationError(
            str(e), e.kind, type="ProviderError", non_retryable=not e.retryable
        ) from e
    return {"ok": "true", "detail": detail}
