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
    asking.start_question_run,
    asking.record_question_cost,
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
    paid.build_evalset,
    paid.evaluate_index,
    paid.propose_tuning,
    paid.promote_candidate_scores,
    paid.persist_profile_scores,
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


def _warn_if_the_engine_disagrees_about_the_embedding_model(
    settings: config.Settings,
) -> None:
    """Say it out loud at startup, because nothing downstream can.

    `docagent.vertex.EMBED_MODEL` is what the CLI writes into `docagent_v2`;
    `BRAIN_EMBEDDING_MODEL` is what this worker writes into `brain`. They differ
    on this installation and that is legitimate — the model belongs to the
    collection, and the two collections were built at different times — but the
    two are both 3,072 wide, so Qdrant accepts either into either without an
    error and the cosine between them means nothing.

    `runner.index_chunks` refuses a mismatch it can see. It cannot see this one:
    both halves of a single run agree with each other. What it costs is that
    anybody reading the two constants has to already know which collection is
    which, so the log says it once per start rather than leaving it to be
    rediscovered.
    """
    try:
        from docagent.vertex import EMBED_MODEL as engine_model
    except Exception:  # pragma: no cover - the engine is a hard dependency
        return
    if engine_model != settings.gemini.embedding_model:
        log.warning(
            "the engine's CLI embeds with %s and this worker embeds with %s. "
            "Both are used against different collections, which is fine; mixing "
            "them in one collection is not, and nothing would report it.",
            engine_model, settings.gemini.embedding_model,
        )


async def main() -> None:
    settings = config.configure()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    _warn_if_the_engine_disagrees_about_the_embedding_model(settings)
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
