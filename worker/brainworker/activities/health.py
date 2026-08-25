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

from .. import config

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
