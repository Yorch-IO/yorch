"""The Temporal worker process."""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from . import config
from .activities import asking, ingest, paid, rebuild, removing
from .activities.health import probe_provider, probe_services
from .workflows.ask import AskWorkflow
from .workflows.ingest import IngestWorkflow
from .workflows.ping import PingWorkflow
from .workflows.probe import ProviderProbeWorkflow
from .workflows.rebuild import RebuildWorkflow
from .workflows.removal import RemovalWorkflow

log = logging.getLogger(__name__)

WORKFLOWS = [
    PingWorkflow,
    IngestWorkflow,
    RebuildWorkflow,
    AskWorkflow,
    RemovalWorkflow,
    ProviderProbeWorkflow,
]

# Every activity the workflows reference must be registered here or the worker
# accepts the task and then fails it with "activity not registered", which reads
# like a Temporal problem rather than a missing line in this list.
ACTIVITIES = [
    probe_services,
    probe_provider,
    asking.answer_question,
    removing.remove_document,
    removing.remove_version,
    ingest.stage_source,
    ingest.register_document,
    ingest.extract_text,
    ingest.preview_chunks,
    ingest.estimate_cost,
    ingest.resolve_profile,
    ingest.link_duplicate,
    ingest.project_structure,
    ingest.activate_version,
    ingest.record_run_outcome,
    ingest.set_run_stage,
    paid.learn_profile,
    paid.correct_text,
    paid.chunk_final,
    paid.embed_and_index,
    paid.extract_semantics,
    rebuild.load_rebuild_inputs,
    rebuild.replay_semantics,
]


async def connect(settings: config.Settings, attempts: int = 30) -> Client:
    """Connect to Temporal, tolerating a server that is still starting.

    `docker compose up` returns as soon as the containers exist, and the
    auto-setup image spends its first several seconds creating schemas. Failing
    immediately here would make a healthy stack look broken on every cold start.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await Client.connect(
                settings.temporal_target, namespace=settings.temporal_namespace
            )
        except Exception as e:
            last = e
            log.info(
                "temporal not ready (attempt %d/%d): %s", attempt, attempts, e
            )
            await asyncio.sleep(2)
    raise RuntimeError(f"could not reach Temporal at {settings.temporal_target}") from last


async def main() -> None:
    settings = config.configure()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    client = await connect(settings)
    log.info(
        "worker starting: queue=%s namespace=%s workspace=%s",
        settings.task_queue,
        settings.temporal_namespace,
        settings.workspace,
    )
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
    )
    await worker.run()


def run() -> None:
    asyncio.run(main())
