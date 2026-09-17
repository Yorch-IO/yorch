"""The Temporal worker process."""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from . import config
from .activities import (
    activating, asking, chatting, exporting, ingest, paid, rebuild, removing,
)
from .activities import bucket as bucketacts
from .activities import channel as channelacts
from .activities import video as videoacts
from .activities.health import probe_provider, probe_services
from .workflows.activation import ActivationWorkflow
from .workflows.ask import AskWorkflow
from .workflows.bucket import (
    AudioIngestWorkflow,
    BucketSyncWorkflow,
    MediaLinkWorkflow,
    fetch_queue_for,
)
from .workflows.channel import (
    ChannelAskWorkflow,
    ChannelDiscoverWorkflow,
    ChannelTopicsWorkflow,
)
from .workflows.chat import ChatWorkflow
from .workflows.epub import EpubWorkflow
from .workflows.ingest import IngestWorkflow
from .workflows.ping import PingWorkflow
from .workflows.probe import ProviderProbeWorkflow
from .workflows.rebuild import RebuildWorkflow
from .workflows.removal import RemovalWorkflow
from .workflows.video import VideoIngestWorkflow

log = logging.getLogger(__name__)

WORKFLOWS = [
    PingWorkflow,
    IngestWorkflow,
    RebuildWorkflow,
    AskWorkflow,
    ChatWorkflow,
    RemovalWorkflow,
    ActivationWorkflow,
    EpubWorkflow,
    ProviderProbeWorkflow,
    VideoIngestWorkflow,
    ChannelDiscoverWorkflow,
    ChannelTopicsWorkflow,
    ChannelAskWorkflow,
    BucketSyncWorkflow,
    AudioIngestWorkflow,
    MediaLinkWorkflow,
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
    chatting.open_chat_run,
    chatting.run_chat_turn,
    chatting.fail_chat_turn,
    chatting.name_conversation,
    chatting.record_turn_cost,
    removing.remove_document,
    removing.remove_version,
    activating.promote_version,
    exporting.resolve_book_metadata,
    exporting.build_epub,
    exporting.export_version_epub,
    ingest.open_run,
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
    ingest.record_run_events,
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
    videoacts.resolve_video,
    videoacts.probe_video,
    videoacts.record_video_artifacts,
    videoacts.group_transcript,
    videoacts.preview_transcript,
    videoacts.estimate_video,
    videoacts.fetch_audio,
    videoacts.stage_audio,
    videoacts.discard_audio,
    videoacts.start_transcription,
    videoacts.poll_transcription,
    videoacts.collect_transcript,
    videoacts.abandon_transcription,
    videoacts.chunk_transcript,
    channelacts.quote_channel_discovery,
    channelacts.preselect_channel_videos,
    channelacts.read_channel_topics,
    channelacts.synthesise_channel,
    rebuild.load_rebuild_inputs,
    rebuild.replay_semantics,
    bucketacts.sync_bucket,
    bucketacts.estimate_audio,
    bucketacts.probe_object,
    bucketacts.stage_transcript,
    bucketacts.check_archive,
    bucketacts.archive_transcript,
    bucketacts.archive_correction,
    bucketacts.set_document_dates,
    bucketacts.presign_object,
]

#: Served on `<task_queue>-audio-fetch` by a second worker in this process,
#: with a small concurrency bound — see `FETCH_CONCURRENCY`.
FETCH_ACTIVITIES = [
    bucketacts.fetch_object,
]

#: How many bucket objects stream through this container at once.
#:
#: The main worker's default is a hundred concurrent activities, and a batch
#: of a hundred and forty-seven approvals in one sweep would open a hundred
#: streams into a 2 GiB container that is offered to the OOM killer first. A
#: second `Worker` on its own queue is the same shape `fetch_queue` already
#: has for YouTube, and a queue is exactly the right thing for the rest to
#: wait in: Temporal holds them, the workflows stay parked in `fetching`, and
#: nothing times out — the fetch is scheduled with no schedule-to-start
#: timeout for precisely that reason. Four is a guess until the pilot
#: measures it.
FETCH_CONCURRENCY = 4


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
    fetcher = Worker(
        client,
        task_queue=fetch_queue_for(settings.task_queue),
        activities=FETCH_ACTIVITIES,
        max_concurrent_activities=FETCH_CONCURRENCY,
    )
    await asyncio.gather(worker.run(), fetcher.run())


def run() -> None:
    asyncio.run(main())
