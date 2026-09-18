"""Recasting a document into another genre, as activities.

Six of them, and the split between them is the retry policy rather than
tidiness — the same reasoning `activities/exporting.py` states. `read_source`,
`estimate_transform` and `bind_document` read and write files and get three
attempts; `probe_library`, `plan_transformation` and `compose_chapter` spend and
get two, because a re-run of a free stage is free and a re-run of a paid one is
not.

Every one of them `await asyncio.to_thread(...)` around anything synchronous,
and the two that can run for minutes heartbeat from a **loop-side task** rather
than from inside the work. That is not a precaution. Six activities in this
worker were `async def` around a synchronous network call and held the only
event loop for the length of a document, which meant the heartbeat they recorded
could never be *sent* — the call that recorded it was holding the loop that had
to send it. The measured cost was a worker frozen for 97 minutes, a
`heartbeat_timeout` firing at minute five on an activity that was working
perfectly, and **$9.4539 of a $10.017265 run** charged twice.

**No LangGraph symbol crosses the Temporal boundary.** The graphs are built and
run entirely inside `plan_transformation` and `compose_chapter`;
`workflows/transform.py` imports none of it. That is the separation
`evaluate.py` records about its own move — *"so the Temporal worker can pick a
candidate without importing this module, which would pull in LangGraph and the
whole node set for one function."*
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import bookexport, config
from ..artifacts import ArtifactRef, ArtifactStore
from ..catalog import Catalog
from ..pipeline import Estimate
from ..transform import assemble, budget, composing, estimate, genres, planning, reading
from ..transform.types import (
    ChapterOutcome,
    ChapterRequest,
    Continuity,
    ProbeReading,
    SourceReading,
    TransformPlan,
    TransformRequest,
)
from .ingest import _record, _settings
from .paid import _charge

log = logging.getLogger(__name__)

RECORD_TIMEOUT = 3.0

#: How often the loop-side task tells Temporal the work is alive. Sixty times
#: under `PAID_HEARTBEAT_TIMEOUT`, which is the calibration `activities/paid.py`
#: already documents for the same pair.
HEARTBEAT_INTERVAL = 5.0


class TransformError(RuntimeError):
    """Something this run cannot recover from, with a kind a client can act on."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def _refuse(e: TransformError) -> ApplicationError:
    """Carry the `kind` across the boundary, non-retryable.

    `kind` is what `app/src/lib/api.ts` keys its guidance on, so flattening it
    into a message would cost the UI its ability to offer a fix. Non-retryable
    because every kind raised here is a statement about what exists — and, since
    every lookup is tenant-scoped, also about who is asking. Neither answer
    changes on a second attempt.

    **The kind goes in `type` and in `details`, and both are needed.**
    `timed.failure_of` — which this workflow uses to record what a failure was —
    reads `cause.type`, while the two planes unwrap `details[0]` when they turn
    a workflow failure into `{"kind", "message"}` for a client. `exporting._refuse`
    puts a class name in `type` and gets away with it because nothing reads its
    run trail; a trail that recorded every refusal here as `TransformError`
    would name the exception and not the fault, which is the difference between
    "it failed" and "it failed because no provider is configured".
    """
    return ApplicationError(str(e), e.kind, type=e.kind, non_retryable=True)


def _catalog(settings):
    """Unpooled and short, like every other bookkeeping read in this package.

    A pool retries a refused connection in the background, so a catalog that is
    merely down turns one immediate error into a ten-second stall — measured
    elsewhere in this worker at 50 s against 0.45 s for one activity suite.
    """
    return Catalog(settings.database_url, pooled=False, timeout=RECORD_TIMEOUT)


def _rows(settings, reading_: SourceReading) -> list[dict]:
    if reading_.chunks is None:
        raise TransformError(
            "this version has no chunks artifact, so there is nothing to recast",
            "chunks_missing",
        )
    return bookexport.read_rows(settings.workspace, reading_.source_run_id, reading_.chunks)


def _passages(settings, reading_: SourceReading) -> list[reading.Passage]:
    return reading.passages_of(_rows(settings, reading_))


# --------------------------------------------------------------------------- #
# Free stages
# --------------------------------------------------------------------------- #


@activity.defn(name="read_source")
async def read_source(request: TransformRequest, run_id: str) -> SourceReading:
    """Everything the free stage can know, from `chunks.jsonl` and the catalog.

    `chunks.jsonl` and not the corrected text stream, for the reason the EPUB
    export records: a chunk row carries `chapter` and `section`, which is the
    outline the chunker detected and **the only place it survives** —
    `build_chunks` *consumes* a heading paragraph rather than emitting it. A
    DOCX, PPTX or XLSX has no text stream at all, and a video has cues. One
    reader covers every source this product can index.
    """
    settings = _settings()

    def work() -> SourceReading:
        with _catalog(settings) as catalog:
            source_run = bookexport.source_run_for(catalog, request.version_id)
            if not source_run:
                raise TransformError(
                    "no run of this version left the chunks a recasting is made "
                    "from; re-index it first",
                    "chunks_missing",
                )
            ref = bookexport.chunks_ref(catalog, source_run)
            if ref is None:
                raise TransformError(
                    "the run that indexed this version recorded no chunks",
                    "chunks_missing",
                )
            document = catalog.document(
                request.document_id, tenant_id=request.tenant_id
            )
            language = catalog.library_language(
                request.library_id, tenant_id=request.tenant_id
            )

        rows = bookexport.read_rows(settings.workspace, source_run, ref)
        passages = reading.passages_of(rows)
        chapters = reading.chapters_of(passages)
        return SourceReading(
            source_run_id=source_run,
            chunks=ref,
            chapters=chapters,
            chunk_count=len(passages),
            characters=sum(len(p.text) for p in passages),
            reference_count=len(reading.references_of(passages)),
            title=(document.title if document else "") or "",
            author=(document.author if document else "") or "",
            language=language or "es",
        )

    try:
        result = await asyncio.to_thread(work)
    except TransformError as e:
        raise _refuse(e) from e
    log.info(
        "recasting %s: %d chunks, %d chapters, %d characters, %d references",
        request.version_id,
        result.chunk_count,
        len(result.chapters),
        result.characters,
        result.reference_count,
    )
    return result


@activity.defn(name="probe_library")
async def probe_library(
    request: TransformRequest, source: SourceReading, run_id: str
) -> ProbeReading:
    """Measure what the library has to say, and turn it into a query budget.

    The **existing** relevance mechanism and no other: the same dense-only probe
    `retrieve.search` runs, at the same `MIN_SCORE`, counting the chunks that
    clear it. Nothing here ranks anything.

    It spends — eight query embeddings, about $0.000032 — and the charge is
    recorded. A stage that spends without a row is exactly how the ledger came
    to be missing every question ever asked, and `ask-embedding` was invisible
    for months by being too small to notice.
    """
    settings = _settings()
    if not request.purposes or not settings.gemini.configured:
        return ProbeReading(budget=0)

    from ..providers import Provider
    from ..transform import research as research_mod

    provider = Provider(settings.gemini)

    def work() -> ProbeReading:
        passages = _passages(settings, source)
        texts = budget.probe_passages(passages)
        out = ProbeReading(sampled=len(texts))
        for text in texts:
            found = research_mod.gather(
                settings,
                provider,
                stage=research_mod.STAGE_PROBE,
                library_id=request.library_id,
                tenant_id=request.tenant_id,
                exclude_version_id=request.version_id,
                purposes=["context"],
                title="",
                intent="",
                source_text=text,
                allowance=1,
            )
            # The count that matters is the one *after* the source's own chunks
            # are filtered out: `supported` includes them, so reporting it alone
            # would over-state what other documents supply. Both are kept —
            # `supported` on the reading, this on the budget — because they
            # measure different things.
            out.per_chapter.append(len(found.evidence))
            out.supported += len(found.evidence)
            out.spend.extend(found.spend)
        return out

    try:
        result = await asyncio.to_thread(work)
    except Exception as e:  # noqa: BLE001 — a run without research beats no run
        log.warning("could not probe the library for %s: %s", request.version_id, e)
        return ProbeReading(budget=0)

    # **The target chapter count, not the source's.** `QUERIES_PER_CHAPTER` caps
    # the budget against "the work's own size", and the work is the one being
    # written — a 400,000-character book whose headings were never detected has
    # *one* source chapter and seventeen target ones, and passing the source
    # count gave it 3 queries where it had earned 51. Measured on this corpus
    # the day the feature first ran: 27 of 52 documents have exactly one
    # detected chapter, so this was not an edge case but the common one.
    genre = genres.GENRES.get(request.genre)
    chapters = (
        estimate.projected_chapters(len(source.chapters), source.characters, genre)
        if genre is not None
        else max(1, len(source.chapters))
    )
    result.budget = budget.budget_for(result.supported, request.purposes, chapters)
    for spend in result.spend:
        _charge(run_id, spend)
    log.info(
        "probe: %d supporting fragment(s) over %d sample(s) -> %d quer(ies)",
        result.supported,
        result.sampled,
        result.budget,
    )
    return result


@activity.defn(name="estimate_transform")
async def estimate_transform(
    request: TransformRequest,
    source: SourceReading,
    probe: ProbeReading,
    run_id: str,
) -> Estimate:
    """The quote, and the artifact that lets anybody check it afterwards.

    Persisted, because the one question a gate exists to let somebody check —
    did it over-report or under-report? — is permanently unanswerable once the
    workflow history ages out, and the only other copy lived there. Best-effort:
    a gate that failed over its own receipt would be the worse trade.
    """
    settings = _settings()
    genre = genres.GENRES.get(request.genre)
    if genre is None:
        raise _refuse(
            TransformError(f"unknown genre {request.genre!r}", "genre_unknown")
        )

    def work() -> Estimate:
        result = estimate.transform_estimate(
            characters=source.characters,
            source_chapters=len(source.chapters),
            genre=genre,
            purposes=request.purposes,
            research_budget=probe.budget,
        )
        try:
            store = ArtifactStore(settings.workspace, run_id)
            _record(run_id, "estimate", store.write_json("estimate", asdict(result)))
        except Exception as e:  # noqa: BLE001
            log.warning("could not persist the estimate for %s: %s", run_id, e)
        return result

    return await asyncio.to_thread(work)


# --------------------------------------------------------------------------- #
# Paid stages
# --------------------------------------------------------------------------- #


def _beating(progress: dict):
    """A loop-side heartbeat task.

    `activity.heartbeat` for an `async def` activity only **records**; the event
    loop is what sends it — temporalio installs its thread-safe wrapper only for
    *sync* activities run in an executor, because the heartbeat's data converter
    is async. So the work counts in a thread and this sends on the loop.
    """

    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if activity.in_activity():
                activity.heartbeat(progress.get("node", ""), progress.get("done", 0))

    return asyncio.ensure_future(beat())


@activity.defn(name="plan_transformation")
async def plan_transformation(
    request: TransformRequest,
    source: SourceReading,
    probe: ProbeReading,
    run_id: str,
) -> TransformPlan:
    """Detect the source's genre and plan the new work's outline.

    Runs the planning `StateGraph` — detect → propose → validate → (adopt |
    refine | fallback) → budget — inside this one activity. Every node is async
    and every call goes through `asyncio.to_thread`, so the loop is free between
    them and the heartbeat below can actually be sent.
    """
    settings = _settings()
    genre = genres.GENRES.get(request.genre)
    if genre is None:
        raise _refuse(
            TransformError(f"unknown genre {request.genre!r}", "genre_unknown")
        )
    if not settings.gemini.configured:
        raise _refuse(
            TransformError(
                "no provider is configured, so nothing can be written",
                "provider_unconfigured",
            )
        )

    from ..providers import Provider

    passages = await asyncio.to_thread(_passages, settings, source)
    excerpt = reading.excerpt(passages)
    target = estimate.projected_chapters(len(source.chapters), source.characters, genre)

    deps = planning.PlanDeps(
        provider=Provider(settings.gemini),
        genre=genre,
        mode=request.mode,
        purposes=list(request.purposes),
        passages=passages,
        source_chapters=list(source.chapters),
        excerpt=excerpt,
        document_title=source.title,
        target_chapters=target,
        max_chapters=estimate.chapter_cap(target),
        supported=probe.supported,
    )

    progress: dict = {"node": "planning", "done": 0}
    beating = _beating(progress)
    try:
        plan = await planning.plan(deps)
    finally:
        beating.cancel()

    for spend in plan.spend:
        _charge(run_id, spend)

    def persist() -> None:
        try:
            store = ArtifactStore(settings.workspace, run_id)
            _record(
                run_id,
                "transform_plan",
                store.write_json("transform_plan", asdict(plan)),
            )
        except Exception as e:  # noqa: BLE001 — the plan is worth more than the row
            log.warning("could not persist the plan for %s: %s", run_id, e)

    await asyncio.to_thread(persist)
    log.info(
        "plan: %d chapters%s, %.0f%% uncovered, %d research quer(ies)",
        len(plan.chapters),
        " (fallback)" if plan.fallback else "",
        plan.uncovered_fraction * 100,
        plan.budget,
    )
    return plan


@activity.defn(name="compose_chapter")
async def compose_chapter(job: ChapterRequest) -> ChapterOutcome:
    """Research, write and check one chapter, then append it to the draft.

    One activity per chapter, which is the unit that retries and the unit that
    charges. The alternative — one activity for the whole work — is the recorded
    `extract_semantics` shape: hours long, all-or-nothing, and re-buying every
    chapter already written when the retry comes.

    The draft, the continuity record and the report are each **rewritten whole**
    and returned as references. A retried attempt re-reads the references
    describing the state *before* it ran, so it cannot double-append.
    """
    settings = _settings()
    genre = genres.GENRES.get(job.request.genre)
    if genre is None:
        raise _refuse(
            TransformError(f"unknown genre {job.request.genre!r}", "genre_unknown")
        )
    chapter = next(
        (c for c in job.plan.chapters if c.ordinal == job.ordinal), None
    )
    if chapter is None:
        raise _refuse(
            TransformError(
                f"the plan has no chapter {job.ordinal}", "chapter_not_planned"
            )
        )

    from ..providers import Provider

    store = ArtifactStore(settings.workspace, job.run_id)

    def load() -> tuple[str, list[str], Continuity, list[dict], list[dict], list[dict]]:
        """Every file this chapter needs, read once.

        Every read verifies its digest, which matters more here than it looks:
        each of these three artifacts is rewritten by every chapter, so a stale
        reference is not a missing file but a *previous* one, and every row in it
        would be perfectly well-formed.
        """
        passages = _passages(settings, job.reading)
        carried_rows = (
            store.read_jsonl(job.continuity) if job.continuity is not None else []
        )
        carried = _continuity_of(carried_rows[-1]) if carried_rows else Continuity()
        return (
            reading.text_for(passages, chapter.sources),
            reading.references_of(passages),
            carried,
            store.read_jsonl(job.draft) if job.draft is not None else [],
            carried_rows,
            store.read_jsonl(job.report) if job.report is not None else [],
        )

    (
        source_text,
        references,
        carried,
        draft_rows,
        carried_rows,
        report_rows,
    ) = await asyncio.to_thread(load)

    deps = composing.ChapterDeps(
        settings=settings,
        provider=Provider(settings.gemini),
        genre=genre,
        mode=job.request.mode,
        plan=job.plan,
        chapter=chapter,
        source_text=source_text,
        incoming=carried,
        purposes=list(job.request.purposes),
        references=references,
        allowance=job.allowance,
        library_id=job.request.library_id,
        tenant_id=job.request.tenant_id,
        version_id=job.request.version_id,
    )

    progress: dict = {"node": "composing", "done": job.ordinal}
    beating = _beating(progress)
    try:
        composed = await composing.compose(
            deps, on_node=lambda name: progress.__setitem__("node", name)
        )
    finally:
        beating.cancel()

    for spend in composed.spend:
        _charge(job.run_id, spend)

    def persist() -> tuple[ArtifactRef, ArtifactRef, ArtifactRef]:
        rows = [r for r in draft_rows if int(r.get("ordinal", 0)) != job.ordinal]
        rows.append(assemble.draft_row(composed.chapter))
        draft = _record(
            job.run_id, "transform_draft", store.write_jsonl("transform_draft", rows)
        )

        kept = [r for r in carried_rows if int(r.get("ordinal", 0)) != job.ordinal]
        kept.append({"ordinal": job.ordinal, **asdict(composed.continuity)})
        continuity = _record(
            job.run_id,
            "transform_continuity",
            store.write_jsonl("transform_continuity", kept),
        )

        reports = [r for r in report_rows if int(r.get("ordinal", 0)) != job.ordinal]
        reports.append(
            {
                "ordinal": job.ordinal,
                "title": composed.chapter.title,
                "characters": len(composed.chapter.body),
                "queries_made": composed.queries_made,
                "off_corpus": composed.off_corpus,
                "revisions": composed.revisions,
                "invented": composed.invented,
                "cited": len(composed.chapter.cited),
                # The text, not just the count. A dropped claim that leaves the
                # work quietly shorter is its own failure, and this is where a
                # reader can see exactly what the model wanted to say and could
                # not support.
                "removed": composed.removed,
            }
        )
        report = _record(
            job.run_id, "transform_report", store.write_jsonl("transform_report", reports)
        )
        return draft, continuity, report

    draft, continuity, report = await asyncio.to_thread(persist)
    return ChapterOutcome(
        draft=draft,
        continuity=continuity,
        report=report,
        queries_made=composed.queries_made,
        removed=len(composed.removed),
        invented=composed.invented,
        cited=len(composed.chapter.cited),
        revisions=composed.revisions,
        spend=list(composed.spend),
    )


def _continuity_of(row: dict) -> Continuity:
    return Continuity(
        glossary=dict(row.get("glossary") or {}),
        established=[str(x) for x in (row.get("established") or [])],
        threads=[str(x) for x in (row.get("threads") or [])],
        tail=str(row.get("tail") or ""),
        cited=[str(x) for x in (row.get("cited") or [])],
    ).trimmed()


@activity.defn(name="bind_document")
async def bind_document(
    request: TransformRequest,
    plan: TransformPlan,
    source: SourceReading,
    draft: ArtifactRef | None,
    run_id: str,
) -> ArtifactRef:
    """Assemble the chapters and the bibliography into the one file a person reads.

    Free. The bibliography is rendered by a pure function with no model in the
    path, which is what makes it unfabricatable rather than merely
    well-instructed: every library entry is a locator a citation carried and
    every original reference is a string found in the source's own bytes.
    """
    settings = _settings()
    genre = genres.GENRES.get(request.genre)
    if genre is None:
        raise _refuse(
            TransformError(f"unknown genre {request.genre!r}", "genre_unknown")
        )
    if draft is None:
        raise _refuse(
            TransformError("no chapter was composed", "nothing_composed")
        )

    store = ArtifactStore(settings.workspace, run_id)

    def work() -> ArtifactRef:
        rows = store.read_jsonl(draft)
        references = reading.references_of(_passages(settings, source))
        text = assemble.assemble(
            rows,
            genre=genre,
            language=source.language,
            work_title=plan.title or source.title,
            references=references,
        )
        return _record(
            run_id, "transform", store.write_bytes("transform", text.encode("utf-8"))
        )

    ref = await asyncio.to_thread(work)
    log.info("bound %s: %d bytes", run_id, ref.bytes)
    return ref
