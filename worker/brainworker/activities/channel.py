"""Reading a channel, as activities: quote, judge metadata, read transcripts.

Every one of them `await asyncio.to_thread(...)` around the provider call. Not a
precaution: six activities in this worker were `async def` around a synchronous
network call and held the only event loop for the length of a document, which
meant the heartbeat they recorded could never be *sent* — the call that recorded
it was holding the loop that had to send it. `read_channel_topics` makes one
call per video and comes back to the loop between them, which is what lets it
heartbeat its progress rather than only record it.

Two things about what these read:

* **The catalogue comes from the workspace and the indexed state from Postgres.**
  Those are different questions with different owners, and mirroring either into
  the other is how two records of one fact come to disagree in silence.
* **A transcript is read through its `ArtifactRef`**, so the digest is checked.
  `chunk_transcript` gives the reason in its own docstring: the text and the cue
  table have to be a matched pair, and a stale one of either produces confident
  output about the wrong thing. Here the stake is lower — nothing indexes what
  this reads — but the file is somebody else's run's, so trusting the path
  without the hash would be reading whatever happens to be there now.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import config
from ..artifacts import ArtifactError, ArtifactRef, ArtifactStore
from ..catalog import Catalog
from ..answering import planner as planner_mod
from ..answering.retrieve import OffCorpus, search
from ..answering.types import Question
from ..channel import estimate as est
from ..channel import preselect, synthesis, topics
from ..channel.types import (
    DiscoverRequest,
    DiscoveryOutcome,
    TopicsOutcome,
    TopicsRequest,
)
from ..channelstore import ChannelStore
from ..pipeline import Estimate, Spend
from ..providers import Provider
from ..providers.gemini import ProviderError

log = logging.getLogger(__name__)

#: Bookkeeping must never stall a stage behind a catalog that is merely down.
#: See `activities/ingest._record`.
RECORD_TIMEOUT = 3.0

#: The artifact a probed video leaves that this reads. Named once rather than
#: written at both call sites, for the reason `REQUIRED_ARTIFACT` exists: the
#: writer and the reader must not be able to drift apart.
TRANSCRIPT_ARTIFACT = "transcript_text"


def _settings() -> config.Settings:
    return config.load()


def _store(settings: config.Settings, tenant_id: str) -> ChannelStore:
    return ChannelStore(settings.paths.for_tenant(tenant_id).channels)


def _fail(kind: str, message: str) -> ApplicationError:
    """A permanent failure, so Temporal stops retrying it.

    A channel nobody has synced and a topic nobody typed are decisions that will
    not change on the second attempt, and three retries against them is three
    times the wait before anybody is told why.
    """
    return ApplicationError(message, type=kind, non_retryable=True)


def _provider(settings: config.Settings) -> Provider:
    try:
        return Provider(settings.gemini)
    except ProviderError as e:
        raise _fail(e.kind, str(e)) from e


def _record(run_id: str, kind: str, ref: ArtifactRef) -> None:
    """Best-effort, for the reason `activities/ingest._record` gives: a pass that
    succeeded and failed to record its own receipt is a bookkeeping gap, and one
    refused because the catalog blinked is work paid for twice."""
    try:
        with Catalog(
            _settings().database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            catalog.record_artifact(
                run_id,
                name=kind,
                rel_path=ref.path,
                sha256=ref.sha256,
                size_bytes=ref.bytes,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("could not record %s for %s: %s", kind, run_id, e)


def _charge(run_id: str, spend: Spend | None) -> Spend | None:
    """Append-only, because a retried activity spent its tokens whether or not
    the attempt succeeded."""
    if spend is None:
        return None
    try:
        with Catalog(
            _settings().database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            catalog.record_cost(
                run_id,
                stage=spend.stage,
                provider="vertex",
                model=spend.model,
                input_tokens=spend.input_tokens,
                output_tokens=spend.output_tokens,
                usd=spend.usd,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("could not record spend for %s: %s", run_id, e)
    return spend


# --- quoting -----------------------------------------------------------------


def plan_for(settings: config.Settings, request: DiscoverRequest) -> est.DiscoveryPlan:
    """What this run would judge and read, from the catalogue on disk."""
    store = _store(settings, request.tenant_id)
    if store.read(request.channel_id) is None:
        raise _fail(
            "channel_not_synced",
            "este canal no se ha sincronizado todavía",
        )
    return est.plan_for(
        request.topic,
        store.videos(request.channel_id),
        limit=request.limit,
        deep_limit=request.deep_limit,
        video_ids=request.video_ids,
    )


@activity.defn(name="quote_channel_discovery")
async def quote_channel_discovery(run_id: str, request: DiscoverRequest) -> Estimate:
    """Price the two passes and persist the quote inside the run.

    Shown before the button and recorded inside the run, which is the pairing
    that makes "did the quote over-report or under-report?" answerable later.
    Until the `estimate` artifact was written at all, that question was
    permanently unanswerable for every run once Temporal's retention expired —
    and it is the one question a gate exists to let somebody check.
    """
    settings = _settings()
    plan = plan_for(settings, request)
    estimate = est.discovery_estimate(settings, plan)
    try:
        store = ArtifactStore(settings.workspace, run_id)
        _record(run_id, "estimate", store.write_json("estimate", asdict(estimate)))
    except Exception as e:  # noqa: BLE001 — never fail a run over its own receipt
        log.warning("could not persist the estimate for %s: %s", run_id, e)
    return estimate


# --- the metadata pass -------------------------------------------------------


@activity.defn(name="preselect_channel_videos")
async def preselect_channel_videos(
    run_id: str, request: DiscoverRequest
) -> DiscoveryOutcome:
    """Judge every video's metadata against the topic. One call per batch.

    The whole record goes into the `preselection` artifact — the topic, the
    exact ids evaluated, every verdict, and the model and prompt version that
    produced them — because a preselection is a measurement, and a measurement
    whose instrument is unrecorded cannot be compared with the next one.
    """
    settings = _settings()
    plan = plan_for(settings, request)
    provider = _provider(settings)

    result, spend = await asyncio.to_thread(
        preselect.select, provider, request.topic, plan.evaluated
    )
    _charge(run_id, spend)

    try:
        store = ArtifactStore(settings.workspace, run_id)
        _record(
            run_id, "preselection", store.write_json("preselection", asdict(result))
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not persist the preselection for %s: %s", run_id, e)

    counts = {k: 0 for k in preselect.RELEVANCE}
    for candidate in result.candidates:
        if candidate.relevancia in counts:
            counts[candidate.relevancia] += 1
    return DiscoveryOutcome(
        run_id=run_id,
        evaluated=len(result.evaluated),
        relevant=counts["relevante"],
        doubtful=counts["dudoso"],
        discarded=counts["descartado"],
        unevaluated=result.unevaluated,
        invented=result.invented,
        malformed=result.malformed,
        shortlist=[c.video_id for c in result.shortlist][: request.deep_limit],
        total_usd=spend.usd if spend else None,
    )


# --- the transcript pass -----------------------------------------------------


@activity.defn(name="read_channel_topics")
async def read_channel_topics(run_id: str, request: TopicsRequest) -> TopicsOutcome:
    """Read each probed video's uncorrected transcript for what it is about.

    One call per video, and the loop comes back between them — which is what
    lets the heartbeat it records actually be sent, and what makes a video that
    fails cost its own reading rather than the batch's.
    """
    settings = _settings()
    provider = _provider(settings)
    sources = await asyncio.to_thread(_transcripts_for, settings, request)

    store = ArtifactStore(settings.workspace, run_id)
    readings: list[topics.VideoTopics] = []
    charged: float = 0.0
    priced = False

    for index, (video_id, text) in enumerate(sources):
        reading, spend = await asyncio.to_thread(
            topics.read_topics, provider, request.topic, text, video_id=video_id
        )
        readings.append(reading)
        _charge(run_id, spend)
        if spend is not None and spend.usd is not None:
            charged += spend.usd
            priced = True
        # Guarded for the reason `extract_semantics` gives: every test in
        # `tests/activities/` calls these as plain functions rather than
        # through a worker, and `heartbeat` raises outside an activity context.
        if activity.in_activity():
            activity.heartbeat(index + 1, len(sources))

    try:
        _record(
            run_id,
            "topics",
            store.write_json(
                "topics",
                {
                    "topic": request.topic,
                    "channel_id": request.channel_id,
                    "prompt_version": topics.PROMPT_VERSION,
                    "videos": [asdict(r) for r in readings],
                },
            ),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not persist the topics for %s: %s", run_id, e)

    return TopicsOutcome(
        run_id=run_id,
        videos=len(readings),
        answering=sum(1 for r in readings if r.responde),
        verified=sum(r.verified for r in readings),
        unverified=sum(len(r.temas) - r.verified for r in readings),
        failed=sum(1 for r in readings if r.failed),
        total_usd=charged if priced else None,
    )


def _transcripts_for(
    settings: config.Settings, request: TopicsRequest
) -> list[tuple[str, str]]:
    """`(video_id, transcript)` for each run that has one, in the order asked.

    **The video id comes from the run's own document, never from the caller.**
    A client-supplied pairing would let one video's words be filed under
    another's identity, which is the damage `videosource.check_resolved` refuses
    and which fails nowhere downstream.

    A run that is not a video run, or belongs to another library, is refused
    outright: an id is not authorization, and this one arrives over HTTP.
    A run that simply has no transcript yet — still probing, or a video with no
    captions whose audio has not been transcribed — is skipped, because that is
    a state and not an error.
    """
    out: list[tuple[str, str]] = []
    with Catalog(settings.database_url) as catalog:
        documents = {
            d.id: d for d in catalog.documents(request.library_id, include_absent=True)
        }
        for workflow_id in request.video_runs:
            run = catalog.run(workflow_id, tenant_id=request.tenant_id)
            if run is None:
                raise _fail("run_not_found", f"no existe el run {workflow_id!r}")
            if run.kind != "video" or run.library_id != request.library_id:
                raise _fail(
                    "run_not_in_this_channel",
                    f"el run {workflow_id!r} no pertenece a este canal",
                )
            document = documents.get(run.document_id or "")
            if document is None:
                continue
            video_id = document.source_key.rsplit("/", 1)[-1]
            row = next(
                (
                    r
                    for r in catalog.artifacts(run.id)
                    if r["name"] == TRANSCRIPT_ARTIFACT
                ),
                None,
            )
            if row is None:
                continue
            ref = ArtifactRef(
                kind=TRANSCRIPT_ARTIFACT,
                path=row["rel_path"],
                sha256=row["sha256"],
                bytes=int(row["size_bytes"]),
            )
            try:
                text = ArtifactStore(settings.workspace, run.id).read_text(ref)
            except ArtifactError as e:
                # The row outlives the file in two recorded ways — a pruned run
                # directory, and a catalog holding rows written under a
                # different workspace. Neither is a reason to fail the batch.
                log.warning("could not read the transcript of %s: %s", video_id, e)
                continue
            out.append((video_id, text))
    return out


# --- the question --------------------------------------------------------------


@activity.defn(name="synthesise_channel")
async def synthesise_channel(question: Question) -> synthesis.Synthesis:
    """Read a channel's indexed sermons into five sections.

    The retrieval is `answering.retrieve.search`, unchanged and *not*
    reimplemented, which is what makes every figure measured against the
    one-shot path keep holding here — the tenant and library scoping, the dense
    floor, `diversify`, the concept expansion and the locators all come with it.

    Not retried by the workflow that calls it, for the reason `answer_question`
    gives: every attempt is a paid generation and the provider adapter already
    retries the transport failures worth retrying. A second attempt here would
    bill the user twice for one question.
    """
    settings = _settings()
    provider = _provider(settings)

    def work() -> synthesis.Synthesis:
        plan = planner_mod.plan(provider, question)
        spend = [plan.spend] if plan.spend else []
        try:
            supported: list[int] = []
            evidence = search(settings, provider, question, plan, spend, supported)
        except OffCorpus as e:
            # Told apart from "not enough evidence" because the remedy differs:
            # this topic belongs to a different corpus, not to a gap in this
            # one. The reason travels, because it is the only thing that says
            # which refusal this was.
            return synthesis.Synthesis(
                state="off_corpus",
                topic=question.text,
                model=provider.settings.model,
                reason=(
                    "ningún fragmento de este canal supera el umbral de "
                    "similitud: el tema parece ser de otra materia"
                ),
                evidence=e.nearby,
                spend=spend,
            )
        result = synthesis.compose(provider, question, evidence)
        result.spend = spend + result.spend
        return result

    return await asyncio.to_thread(work)
