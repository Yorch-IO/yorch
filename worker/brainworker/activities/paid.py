"""The stages that spend money. None of them runs before the gate is answered.

Each one is separately switchable there because they cost wildly different
amounts — from a real measured run, correction $0.0334 against embedding
$0.0033 — so "approve" is a set of switches rather than one yes.

Two properties are shared by everything here and are not optional. The activity
records what it actually spent, from the counts the API reported rather than
from the estimate; and it is idempotent, because Temporal retries and a retry
that re-embedded a book would double a real bill.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import pathlib
import re
from collections import Counter
from dataclasses import asdict

from temporalio import activity

from .. import config, quoting
from ..artifacts import ArtifactStore
from ..catalog import Catalog
from ..graph import Graph
from ..graph import projection as proj
from ..graph.schema import chunk_id as make_chunk_id
from ..graph.schema import canonical_concept
from ..graph.schema import concept_id as make_concept_id
from ..pipeline import (
    ChunkKindCount,
    Chunked,
    Correction,
    EvalSet,
    Extraction,
    Indexed,
    ProfileDecision,
    ProfileRules,
    Registered,
    Scores,
    Semantics,
    Spend,
    StageOptions,
    Staged,
    TuneOutcome,
)
from ..indexing import (
    apply_overrides,
    INTEGER_INDEXES, PAYLOAD_INDEXES, QdrantWriter, StoredChunk, version_scope,
)
from ..indexing import chunk_row as indexing_chunk_row
from .. import videosource
from ..providers import CachedEmbedder, Provider, VertexAdapter
from ..providers.gemini import RETRIEVAL_DOCUMENT, Usage
from .ingest import (
    CHARS_PER_TOKEN,
    EVAL_SAMPLE,
    EVAL_SAMPLE_TUNING,
    EVAL_SEED,
    profile_dir,
    CONDENSE_SOURCE_CAP,
    CONDENSE_TOKENS,
    CONDENSE_WORDS,
    PROFILE_MAX_ATTEMPTS,
    RECORD_TIMEOUT,
    _Evidence,
    _record,
    _settings,
    kind_classifier,
    chunk_rules,
    price_for,
    rules_from_profile,
    section_tree,
)

log = logging.getLogger(__name__)

#: One Qdrant collection for every library, filtered by payload rather than one
#: collection per library. A collection's vector size is fixed at creation, so
#: many collections means many places a dimension change has to be migrated —
#: and cross-library search becomes a fan-out instead of a filter.
#:
#: The name comes from settings, not from here, so a test suite can write
#: somewhere disposable. This constant is only the default.
DEFAULT_COLLECTION = "brain"

#: Concurrent embedding requests. There is no batch size to choose: the model
#: returns one embedding for a request carrying four texts, with no error
#: (measured 2026-08-19), so `Provider.embed` issues one request per chunk and
#: this only bounds how many are in flight.
EMBED_WORKERS = 6

#: How often `embed_and_index` reports progress while the embedding thread runs.
#:
#: The stage's `heartbeat_timeout` is unset, so nothing fails for want of one —
#: what this buys is the progress `/runs/{workflow_id}` reads back off
#: `describe().pending_activities`, which is what turns "this is running" into
#: "500 chunks, 96 done". Five seconds is the interval `PAID_HEARTBEAT_TIMEOUT`
#: was calibrated against for the one stage that does set it.
HEARTBEAT_INTERVAL = 5.0


def _provider() -> Provider:
    return Provider(_settings().gemini)


def _embed_cache_dir(settings) -> "pathlib.Path":
    """Where embeddings already paid for live. See `Paths.embed_cache`.

    Kept as a name here because eight call sites read it, and moved to `Paths`
    because the answering path needs the same directory — two definitions would
    be a cache that never hits.
    """
    return settings.paths.embed_cache


def _charge(run_id: str, spend: Spend, provider: str = "vertex") -> Spend:
    """Record real spend in the catalog. Append-only.

    A retried activity spent its tokens whether or not the attempt succeeded, so
    this appends rather than overwriting — a total that could be revised
    downwards by a retry would under-report the bill that was actually incurred.

    **That premise is false in exactly one place**, and the caller there says so:
    an Amazon Transcribe job is started under a name derived from the version, so
    a retry finds the job already running and Amazon bills once. Charging again
    on the retry would double-report a bill that was incurred once.

    `provider` defaults to the one that charges for everything else here.
    `cost_entry.provider` has no CHECK constraint, so a new one needs no
    migration.
    """
    try:
        settings = _settings()
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            catalog.record_cost(
                run_id,
                stage=spend.stage,
                provider=provider,
                model=spend.model,
                input_tokens=spend.input_tokens,
                output_tokens=spend.output_tokens,
                usd=spend.usd,
            )
    except Exception as e:
        # Losing the record is bad; failing the stage that already spent the
        # money is worse, because the retry spends it again.
        log.warning("could not record spend for %s: %s", run_id, e)
    return spend


# ---------------------------------------------------------------------------
# Rule learning — the first stage that spends, and the cheapest
# ---------------------------------------------------------------------------


@activity.defn(name="learn_profile")
async def learn_profile(
    run_id: str, extraction: Extraction, decision: ProfileDecision
) -> ProfileDecision:
    """Learn this document family's structural rules, once, and keep them.

    Runs the engine's own propose/validate loop directly rather than through its
    LangGraph. That graph also embeds into Qdrant, builds an eval set and runs
    the tuning loop — work this pipeline already owns differently, behind its own
    gate and with its own point ids — so invoking it would run a second pipeline
    inside this one. `propose`, `validate` and `adopt` are the three functions
    the graph's nodes call, and calling them is the same work without the rest.

    **Validation is adversarial and that is the point.** The model proposes; a
    deterministic checker applies the proposal to the *whole* document and
    demands an independent signal, then hands back feedback for a refine round.
    Exhausting `PROFILE_MAX_ATTEMPTS` is **not a failure**: the built-in rules
    were themselves measured on a real book, so falling back to them is a
    known-good configuration and the document indexes exactly as it would have
    before this stage existed.

    Adoption is partial — a rule that failed is dropped and the rest are kept.
    Requiring all of them once threw away a header pattern that had validated
    cleanly three times, leaving 175 running-header lines in the text.
    """
    from docagent import profiles as engine_profiles
    from docagent import rules as engine_rules

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    evidence = _Evidence(store.read_json(extraction.evidence))
    text = store.read_bytes(extraction.text)

    adapter = VertexAdapter(_provider())
    feedback: list[str] = []
    validation = None
    proposal = None

    for attempt in range(1, PROFILE_MAX_ATTEMPTS + 1):
        # In a thread, like every other paid stage here and for the reason
        # `extract_semantics` states: `propose` is a synchronous network call,
        # and an `async def` activity that makes one inline holds the worker's
        # *only* event loop for its whole duration — no heartbeat flushed, no
        # workflow task processed, no query answered, for any workflow on this
        # worker. Observed live on 2026-09-03: an import's `embed_and_index`
        # held the loop, a question asked 48 seconds later logged a
        # `WORKFLOW_TASK_TIMED_OUT` and could not be queried at all, and the app
        # rendered "the control API did not answer" over an API that was
        # replying in under a millisecond.
        proposal = await asyncio.to_thread(
            engine_rules.propose, adapter, evidence, feedback or None
        )
        validation = engine_rules.validate(proposal, text, evidence)
        _record(
            run_id,
            "proposal",
            store.write_json("proposal", {"attempt": attempt, **asdict(proposal)}),
        )
        _record(
            run_id,
            "validation",
            store.write_json(
                "validation",
                {
                    "attempt": attempt,
                    "passed": validation.passed,
                    "notes": validation.notes(),
                    "failed_rules": sorted(validation.failed_rules()),
                },
            ),
        )
        if validation.passed:
            break
        feedback = validation.feedback
        log.info(
            "profile attempt %d/%d failed: %s",
            attempt, PROFILE_MAX_ATTEMPTS, sorted(validation.failed_rules()),
        )

    spend = _charge(
        run_id,
        Spend(
            stage="profile",
            model=settings.gemini.model,
            input_tokens=adapter.usage.input_tokens,
            output_tokens=adapter.usage.output_tokens,
            usd=price_for(
                settings.gemini.model,
                adapter.usage.input_tokens,
                adapter.usage.output_tokens,
            ),
        ),
    )
    decision.spend = spend

    if validation is None or proposal is None:  # pragma: no cover - loop always runs
        _record(run_id, "profile", store.write_json("profile", asdict(decision)))
        return decision

    # Adopted even when an essential rule failed every attempt, because `adopt`
    # is per-rule: a failed rule is reset to its measured default and the ones
    # that validated are kept. Discarding the lot instead is the mistake this
    # project already paid for once — and paid for again on the first real run
    # here, where three attempts failed `heading_guards` on a non-contiguous
    # chapter sequence and took with them a header pattern that had matched on
    # 26 pages with no body hits, plus a footnote rule an independent signal
    # confirmed at 100%. Nothing about a bad length guard makes a good header
    # pattern less true.
    adopted = engine_rules.adopted_rules(validation)
    if not adopted:
        # Every rule failed, so the profile would be nothing but defaults —
        # and saving *that* is worse than saving none. The next document of this
        # family matches the same fingerprint, would "reuse" it, and would never
        # try to learn again: one bad run would freeze the family on generic
        # rules permanently. Checked before `save`, not after.
        log.info("profile learning adopted nothing; the measured defaults apply")
        _record(run_id, "profile", store.write_json("profile", asdict(decision)))
        return decision

    profile = engine_rules.adopt(
        proposal,
        validation,
        fingerprint=decision.fingerprint,
        slug=engine_profiles.slug_for(extraction.source_key or run_id, decision.fingerprint),
        extractor=extraction.extractor,
        learned_from=extraction.source_key or run_id,
    )
    profile.save(profile_dir(settings, extraction.tenant_id))

    if not validation.passed:
        log.info(
            "profile learning kept %s after %s failed every attempt",
            adopted, sorted(validation.failed_rules()),
        )

    decision.source = "learned"
    decision.slug = profile.slug
    decision.learned_from = profile.learned_from
    decision.revisions = profile.revisions
    decision.rules = rules_from_profile(profile)
    decision.adopted = adopted
    _record(run_id, "profile", store.write_json("profile", asdict(decision)))
    log.info("learned profile %s, adopted %s", profile.slug, decision.adopted)
    return decision


# ---------------------------------------------------------------------------
# Correction — the dominant cost
# ---------------------------------------------------------------------------


@activity.defn(name="correct_text")
async def correct_text(run_id: str, extraction: Extraction) -> Correction:
    """Correct the prose, keeping the engine's verification gate intact.

    Structured sources never reach here: an LLM must not rewrite a cell value,
    and the workflow skips this stage for them.

    Note what this does *not* return: the corrected text inline. It is a whole
    book, and Temporal payloads cap around 2 MB.
    """
    from docagent import correct as engine_correct
    from docagent.chunk import split_paragraphs

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    data = store.read_bytes(extraction.text)
    paragraphs = [p.text for p in split_paragraphs(data)]

    adapter = VertexAdapter(_provider())
    # In a thread — see `learn_profile`. This is the longest stage in the
    # pipeline: 19 sequential batches, tens of minutes, and every one of those
    # minutes was a minute the worker could answer nothing else.
    corrected, report = await asyncio.to_thread(
        engine_correct.correct_paragraphs, adapter, paragraphs, stage="correct"
    )

    if extraction.single_line_paragraphs:
        # A transcript's paragraph index is what carries its timestamp, and
        # `verify` bounds length, scripture references, digits and proper nouns
        # while looking for no newline at all. A model returning "…dijo.\n\nY
        # entonces…" splits one paragraph into two on the join below and moves
        # every later timestamp by one. Collapsing is safe here and only here —
        # see `Extraction.single_line_paragraphs`.
        corrected = videosource.repair_paragraphs(corrected)

    # Re-joined exactly the way `split_paragraphs` expects to find them, because
    # every char_span computed after this indexes *this* byte stream.
    body = "\n\n".join(corrected).encode("utf-8")
    text_ref = _record(run_id, "corrected_text", store.write_bytes("corrected_text", body))
    report_ref = _record(
        run_id,
        "correction_report",
        store.write_json(
            "correction_report",
            {
                "paragraphs": report.paragraphs,
                "changed": report.changed,
                "unchanged": report.unchanged,
                "missing": report.missing,
                "cache_hits": report.cache_hits,
                "calls": report.calls,
                # `proposed` is the only durable copy: a refused correction
                # is never cached, so without this the report names what went
                # missing and never what the model would have written — which
                # is what makes a rejection reviewable rather than merely
                # counted. See `docagent.correct.Rejection`.
                "rejected": [
                    {"index": r.index, "reason": r.reason, "detail": r.detail,
                     "proposed": r.proposed}
                    for r in report.rejected
                ],
                "summary": report.summary(),
            },
        ),
    )

    spend = _charge(
        run_id,
        Spend(
            stage="correction",
            model=settings.gemini.model,
            input_tokens=adapter.usage.input_tokens,
            output_tokens=adapter.usage.output_tokens,
            usd=price_for(
                settings.gemini.model,
                adapter.usage.input_tokens,
                adapter.usage.output_tokens,
            ),
        ),
    )
    return Correction(
        text=text_ref,
        report=report_ref,
        paragraphs=report.paragraphs,
        changed=report.changed,
        rejected=len(report.rejected),
        missing=report.missing,
        cache_hits=report.cache_hits,
        spend=spend,
    )


@activity.defn(name="chunk_final")
async def chunk_final(
    run_id: str, text_kind: str, decision: ProfileDecision | None = None
) -> Chunked:
    """Chunk the text that will actually be indexed. Free, and always re-run.

    Re-run rather than reusing the preview because correction changed the byte
    stream: every `char_from`/`char_to` in the preview indexes text that no
    longer exists at those offsets.

    This is the chunking that decides the document's table of contents, so it is
    the one that must see the profile's rules. Passing `None` gives the engine's
    built-in defaults, which were themselves measured on a real book.
    """
    from docagent.chunk import build_chunks, split_paragraphs

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)

    from ..artifacts import ArtifactRef, KINDS

    path = settings.workspace / "runs" / run_id / KINDS[text_kind]
    data = path.read_bytes()

    rules = decision.rules if decision and decision.source != "default" else None
    paragraphs = split_paragraphs(data)
    chunks = build_chunks(data, paragraphs, chunk_rules(rules), kind_classifier(rules))
    # One row schema, written through one function — see `indexing.chunk_row`,
    # which sits beside `StoredChunk.from_row`, the reader it must agree with.
    #
    # The positions are read by *filename* from this run's own directory, like
    # the text two lines above, rather than threaded in as a parameter: the
    # arity of an activity is load-bearing here — Temporal maps payloads onto
    # parameters by count and a changed one is the recorded `'dict' object has
    # no attribute 'source_path'` failure three frames from its cause.
    #
    # And the paragraph index is what makes this sound across correction: the
    # sidecar was written against the *extracted* stream and this is chunking
    # the *corrected* one, but correction is keyed by paragraph index and
    # structurally cannot merge two, so paragraph `n` is the same paragraph in
    # both. A byte offset would not have survived; this does.
    # The preview's reader, not a second copy: the gate and the index must
    # not disagree about what page a chunk is on.
    from .ingest import _pages_from_sidecar

    pages = _pages_from_sidecar(run_id)
    rows = [indexing_chunk_row(c, pages=pages) for c in chunks]
    ref = _record(run_id, "chunks", store.write_jsonl("chunks", rows))
    kinds = Counter(c.kind for c in chunks)
    return Chunked(
        chunks=ref,
        count=len(rows),
        kinds=[ChunkKindCount(k, n) for k, n in sorted(kinds.items())],
    )


# ---------------------------------------------------------------------------
# Embedding and hybrid indexing
# ---------------------------------------------------------------------------


def _filterable_facts(settings, registered: Registered) -> tuple[int | None, str]:
    """The document's recording day and source, for every point's payload.

    Read from the catalog rather than threaded through `Registered`, whose
    fields the paid plane's parity spec compares, and best-effort in the way
    every bookkeeping read here is: a catalog that is down costs the points
    their date filter, not the run its index. `None` for the day, never
    zero — an unknown date is absent from the payload, so no range reaches it.
    """
    try:
        with Catalog(settings.database_url, pooled=False, timeout=RECORD_TIMEOUT) as catalog:
            document = catalog.document(registered.document_id, tenant_id=registered.tenant_id)
    except Exception as e:  # noqa: BLE001 - a missing filter, not a failed stage
        log.warning("could not read the document's dates for %s: %s",
                    registered.document_id, e)
        return None, ""
    if document is None:
        return None, ""
    day = document.recorded_at.toordinal() - EPOCH_ORDINAL if document.recorded_at else None
    return day, document.source_name or ""


#: `date(1970, 1, 1).toordinal()`, so `recorded_day` is days since the epoch —
#: the same arithmetic `retrieve.search` applies to a filter's bounds.
EPOCH_ORDINAL = 719163


def _overrides_for(registered: Registered) -> dict[int, Any]:
    """This version's edits, by chunk index, or none at all.

    Unpooled and wrapped, like every other catalog read in a paid stage: a pool
    retries a refused connection in the background, so a catalog that is merely
    down turns one immediate error into a ten-second stall — and an indexing
    run that failed because nobody had edited anything would be the worse
    trade. A catalog that cannot be read costs the edits, not the index.
    """
    try:
        with Catalog(_settings().database_url, pooled=False) as catalog:
            rows = catalog.chunk_overrides(registered.version_id)
    except Exception as e:  # noqa: BLE001
        log.warning("could not read chunk overrides for %s: %s",
                    registered.version_id, e)
        return {}
    return {o.chunk_index: o for o in rows}


@activity.defn(name="embed_and_index")
async def embed_and_index(
    run_id: str,
    library_id: str,
    registered: Registered,
    staged: Staged,
    chunked: Chunked,
) -> Indexed:
    """Embed every chunk and upsert it into Qdrant with its BM25 sparse vector.

    The embedding and the sparse vector come from `docagent.runner.index_chunks`;
    the identity of each point comes from `QdrantWriter`. That split is the whole
    boundary: the engine has no concept of tenancy and must not gain one, and a
    point written without a `tenant_id` is unreachable by either plane with the
    embedding already paid for.

    **Point ids are derived from the version, not the path.** The engine's
    `doc_id_for()` hashes the filename, which is what lets byte-identical
    duplicates index twice and then compete in ranking; using the content-derived
    version id means a re-index of the same bytes overwrites the same points and
    a duplicate file adds none. That also makes this activity idempotent, which
    Temporal requires of it anyway.
    """
    from docagent.qdrant import Qdrant
    from docagent.runner import index_chunks

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    rows = list(store.iter_jsonl(chunked.chunks))
    if not rows:
        return Indexed(
            collection=settings.qdrant_collection,
            points=0,
            dimensions=settings.gemini.embedding_dimensions,
            spend=Spend(stage="embedding", model=settings.gemini.embedding_model),
        )

    chunks = [StoredChunk.from_row(r) for r in rows]
    # What a person changed, substituted **before** the engine embeds, so the
    # dense vector, the sparse vector, the scripture filters and the payload
    # are all of the same words. An override that no longer matches its chunk
    # is orphaned rather than applied — `chunk_index` is not stable across a
    # re-cut — and is logged rather than dropped, because an edit that vanished
    # without a word is worse than one that stopped being applied.
    overrides = _overrides_for(registered)
    chunks, orphaned = apply_overrides(chunks, overrides)
    if orphaned:
        log.warning(
            "run %s: %d chunk override(s) no longer fit this version and were "
            "not applied (indices %s)",
            run_id, len(orphaned), sorted(o.chunk_index for o in orphaned),
        )
    embedder = CachedEmbedder(
        _provider(),
        _embed_cache_dir(settings),
        model=settings.gemini.embedding_model,
        dimensions=settings.gemini.embedding_dimensions,
        workers=EMBED_WORKERS,
    )

    # A 600-chunk book is ~100 minutes of wall clock against the per-minute
    # embedding quota, so silence here is indistinguishable from a hung socket —
    # three runs were given up for dead when the only thing wrong was that the
    # observer left first.
    #
    # **The counting and the sending are split, and that is not tidiness.**
    # `index_chunks` runs in a thread now (see `learn_profile`), and for an
    # `async def` activity `activity.heartbeat` is bound to the event loop:
    # temporalio installs its thread-safe wrapper only for *sync* activities run
    # in an executor, because "heartbeat calls internally use a data converter
    # which is async so they need to be called on the event loop". So the
    # embedding thread only records, and the loop sends. Before this the
    # heartbeat was called inline and *recorded* faithfully — and never flushed,
    # because the same call that recorded it was holding the loop. Observed
    # live: `embed_and_index` in flight with `last_heartbeat` unset.
    progress = {"done": 0, "total": len(chunks)}

    def note(done: int, total: int) -> None:
        progress["done"], progress["total"] = done, total

    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            # Guarded for the reason `extract_semantics` gives: every test in
            # `tests/activities/` calls these as plain functions rather than
            # through a worker, and `heartbeat` raises outside an activity.
            if activity.in_activity():
                activity.heartbeat(progress["done"], progress["total"])

    recorded_day, source_name = _filterable_facts(settings, registered)
    collection = settings.qdrant_collection
    with Qdrant(settings.qdrant_url, collection) as q:
        q.wait_ready()
        q.create(settings.gemini.embedding_dimensions, PAYLOAD_INDEXES,
                 integer_indexes=INTEGER_INDEXES)
        writer = QdrantWriter(
            q,
            tenant_id=registered.tenant_id,
            library_id=library_id,
            document_id=registered.document_id,
            version_id=registered.version_id,
            source_title=staged.title,
            model=settings.gemini.embedding_model,
            dimensions=settings.gemini.embedding_dimensions,
            recorded_day=recorded_day,
            source_name=source_name,
            # The flags only: the edited *text* is already in `chunks` above,
            # so the writer marks a chunk as edited or hidden and never has to
            # know what it now says.
            overrides=overrides,
        )
        beating = asyncio.ensure_future(beat())
        try:
            outcome = await asyncio.to_thread(
                index_chunks,
                chunks, embedder=embedder, writer=writer, on_done=note,
            )
        finally:
            beating.cancel()
        info = q.info()

    spend = _charge(
        run_id,
        Spend(
            stage="embedding",
            model=settings.gemini.embedding_model,
            input_tokens=embedder.usage.input_tokens,
            output_tokens=0,
            usd=price_for(
                settings.gemini.embedding_model, embedder.usage.input_tokens, 0
            ),
        ),
    )
    log.info(
        "indexed %d points into %r (%d total in collection); "
        "%d served from cache, %d stale point(s) pruned",
        outcome.points, collection, info.points_count,
        embedder.cache_hits, outcome.pruned,
    )
    return Indexed(
        collection=collection,
        points=outcome.points,
        dimensions=outcome.dimensions,
        spend=spend,
    )


# ---------------------------------------------------------------------------
# Measuring the index that was just written
# ---------------------------------------------------------------------------

#: `EVAL_SAMPLE` and `EVAL_SEED` come from `activities.ingest`, where the
#: estimator reads them too. One definition, because the estimate is a promise
#: about a bill and the stage is what settles it: two constants would let the
#: gate quote forty calls and the run make eighty.
#:
#: The sample is also what decides what a *comparison* can resolve. With
#: σ ≈ 0.358 the bootstrap margin needs roughly 80 questions to see a +0.040
#: MRR effect, so at 40 a tuning round measures this index honestly and will
#: correctly refuse almost any candidate.
#:
#: The seed is fixed so two runs of one document compare on the same questions.
#: Resampling each time would measure the sample instead of the change — the
#: mistake that made three tuning rounds drift 0.729 → 0.762 → 0.700.


def _profile_for(settings, decision: ProfileDecision, tenant_id: str):
    """This document's profile as it is on disk, or None.

    Read back rather than carried in the payload: `ProfileDecision` deliberately
    flattens a profile to fifteen scalars, because the profile itself also holds
    an eval set of 88 questions and a tuning history, and those would then persist
    in workflow history for the namespace's whole retention period.
    """
    from docagent import profiles as engine_profiles

    if not decision.fingerprint:
        return None
    return engine_profiles.load(
        decision.fingerprint, profile_dir(settings, tenant_id)
    )


@activity.defn(name="build_evalset")
async def build_evalset(
    run_id: str,
    extraction: Extraction,
    chunked: Chunked,
    decision: ProfileDecision,
    sample: int = EVAL_SAMPLE,
) -> EvalSet:
    """Generate the questions this document's index will be measured against.

    One generation call per sampled chunk, stratified by `kind` so a table-heavy
    or footnote-heavy document is not judged purely on its prose.

    **A reused profile's questions are dropped unless they were written from this
    document.** The fingerprint groups by *structure*, and structure is not
    subject matter: a hermeneutics chapter and a church-history book landed on
    the same fingerprint on the real corpus. Scoring book B against book A's
    questions produced a recall of 0 that meant nothing about either — which is
    the failure `doc/CLAUDE.md` records and `n_load_profile` fixes on the engine
    side. The same rule has to hold here or the fix does not travel.
    """
    from docagent import evaluate as ev

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    rows = list(store.iter_jsonl(chunked.chunks))
    if not rows:
        return EvalSet(
            items=store.write_json("evalset", []),
            questions=0,
            sample=sample,
            spend=Spend(stage="evalset", model=settings.gemini.model),
        )

    profile = _profile_for(settings, decision, extraction.tenant_id)
    source_key = extraction.source_key or run_id
    if profile is not None and profile.evalset and profile.learned_from == source_key:
        items = profile.evalset
        log.info("reusing %d questions this document already paid for", len(items))
        return EvalSet(
            items=_record(
                run_id, "evalset", store.write_json("evalset", [asdict(i) for i in items])
            ),
            questions=len(items),
            sample=sample,
            spend=Spend(stage="evalset", model=settings.gemini.model),
            reused=True,
        )
    if profile is not None and profile.evalset:
        log.info(
            "dropping %d inherited questions: the profile was learned from %r, "
            "not from %r",
            len(profile.evalset), profile.learned_from, source_key,
        )

    adapter = VertexAdapter(_provider())
    chunks = [StoredChunk.from_row(r) for r in rows]
    # In a thread — see `learn_profile`. One generation call per sampled chunk,
    # and this is the stage that was holding the loop when a question went
    # unanswerable on 2026-09-03.
    items = await asyncio.to_thread(
        ev.build_evalset, adapter, chunks, sample=sample, seed=EVAL_SEED
    )

    spend = _charge(
        run_id,
        Spend(
            stage="evalset",
            model=settings.gemini.model,
            input_tokens=adapter.usage.input_tokens,
            output_tokens=adapter.usage.output_tokens,
            usd=price_for(
                settings.gemini.model,
                adapter.usage.input_tokens,
                adapter.usage.output_tokens,
            ),
        ),
    )
    ref = _record(
        run_id, "evalset", store.write_json("evalset", [asdict(i) for i in items])
    )
    log.info("generated %d questions from %d chunks", len(items), len(rows))
    return EvalSet(items=ref, questions=len(items), sample=sample, spend=spend)


@activity.defn(name="evaluate_index")
async def evaluate_index(
    run_id: str,
    registered: Registered,
    chunked: Chunked,
    evalset: EvalSet,
    decision: ProfileDecision,
    artifact_kind: str = "scores",
) -> Scores:
    """Ask the index the questions and report what came back.

    Hybrid and dense-only, always both: the questions were written *from* the
    chunks they must find, so they leak vocabulary to the lexical leg and a
    hybrid figure quoted alone flatters the index. The gap between the two is the
    leakage measurement.

    The noise floor travels with them for the same reason. Recall says how often
    the right chunk came back; the floor says what a *wrong* one scores, which is
    what tells a reader whether this index can distinguish a miss from a hit at
    all.
    """
    from docagent.profiles import EvalItem, RetrievalParams
    from docagent.qdrant import Qdrant
    from docagent.runner import evaluate as run_evaluate

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    items = [EvalItem(**d) for d in store.read_json(evalset.items)]
    if not items:
        return Scores(chunks=chunked.count)

    profile = _profile_for(settings, decision, registered.tenant_id)
    params = profile.retrieval if profile is not None else RetrievalParams()

    embedder = CachedEmbedder(
        _provider(),
        _embed_cache_dir(settings),
        model=settings.gemini.embedding_model,
        dimensions=settings.gemini.embedding_dimensions,
        workers=EMBED_WORKERS,
    )
    scope = version_scope(registered.tenant_id, registered.version_id)

    with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
        # In a thread — see `learn_profile`. It embeds every question twice
        # over, plus the noise floor.
        outcome = await asyncio.to_thread(
            run_evaluate,
            embedder, q, items, scope=scope, params=params, chunks=chunked.count,
        )

    engine_scores = outcome.scores
    report = {
        "scores": asdict(engine_scores),
        "leakage": outcome.leakage,
        "margin": round(outcome.margin, 4),
        "retrieval": asdict(params),
        "scope": scope,
        # **The reciprocal rank of every question, misses included.** Not a
        # detail: the bootstrap margin a tuning candidate has to beat is resampled
        # from exactly this vector, and it cannot be reconstructed from `Scores`.
        # Rebuilding it from the mean — every hit at MRR × n / hits — was tried,
        # and on a realistic 40-question run it produced ±0.040 where the true
        # margin is ±0.062, because a flat vector has none of the spread that
        # ranks of 1, ½, ⅓, ¼ carry. **35% too small, in the direction that
        # accepts noise as a real gain**, which is the one thing the margin
        # exists to prevent. Forty floats in an artifact is the cheap way out.
        "reciprocal_ranks": [
            round(r, 6) for r in (outcome.baseline.rr_vector() if outcome.baseline else [])
        ],
        # The questions that did not find their own chunk, with where it ranked.
        # A score with no misses attached is a number nobody can act on.
        "misses": [
            {"question": q_, "want": want, "rank": rank}
            for q_, want, rank in (outcome.baseline.misses if outcome.baseline else [])
        ],
    }
    # A tuning candidate writes its own artifact. Both measurements happen in
    # one run, and writing both to `scores` left a reverted candidate describing
    # an index that had already been thrown away.
    ref = _record(run_id, artifact_kind, store.write_json(artifact_kind, report))

    spend = _charge(
        run_id,
        Spend(
            stage="evaluation",
            model=settings.gemini.embedding_model,
            input_tokens=embedder.usage.input_tokens,
            output_tokens=0,
            usd=price_for(
                settings.gemini.embedding_model, embedder.usage.input_tokens, 0
            ),
        ),
    )
    log.info("evaluated: %s", engine_scores.summary())
    return Scores(
        recall_at_1=engine_scores.recall_at_1,
        recall_at_5=engine_scores.recall_at_5,
        mrr_at_10=engine_scores.mrr_at_10,
        recall_at_5_dense_only=engine_scores.recall_at_5_dense_only,
        noise_floor=engine_scores.noise_floor,
        chunks=engine_scores.chunks,
        eval_questions=engine_scores.eval_questions,
        margin=round(outcome.margin, 4),
        leakage=outcome.leakage,
        report=ref,
        spend=spend,
    )


@activity.defn(name="propose_tuning")
async def propose_tuning(
    run_id: str,
    registered: Registered,
    chunked: Chunked,
    evalset: EvalSet,
    decision: ProfileDecision,
    scores: Scores,
) -> TuneOutcome:
    """One tuning round: the free knobs exhaustively, then at most one paid idea.

    Retrieval knobs — `min_score`, `per_section`, dense-only — change nothing in
    the index, so every one is tried and the best is adopted here and now. Only
    when none of them beats the noise margin is a chunking candidate worth its
    cost, and that one is *returned*, not applied: re-cutting the document means
    embedding every chunk again, which the gate priced as its own stage.

    Nothing is adopted for having been tried. The objective is MRR@10 rather than
    recall@5, which was measured to be blind here — saturated and binary at k=5,
    so an overlap of 300 scored identically to an overlap of 0.
    """
    from dataclasses import replace as dc_replace

    from docagent.profiles import EvalItem
    from docagent.qdrant import Qdrant
    from docagent.runner import tune_once

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    items = [EvalItem(**d) for d in store.read_json(evalset.items)]
    profile = _profile_for(settings, decision, registered.tenant_id)
    if not items or profile is None:
        # Without a profile there is nowhere to keep an adopted knob, and the
        # next run of this document would start from the same place having paid
        # for the round. Refusing is cheaper than measuring and forgetting.
        return TuneOutcome(
            notes=["no eval set or no profile to tune against"],
        )

    embedder = CachedEmbedder(
        _provider(),
        _embed_cache_dir(settings),
        model=settings.gemini.embedding_model,
        dimensions=settings.gemini.embedding_dimensions,
        workers=EMBED_WORKERS,
    )
    scope = version_scope(registered.tenant_id, registered.version_id)
    # The baseline the candidate is judged against is the run `evaluate_index`
    # just measured, re-derived from its own report rather than re-measured —
    # measuring it again would spend the query embeddings twice and compare two
    # runs that both moved.
    baseline = _baseline_from(store, scores)
    if baseline is None:
        # Without the per-question ranks there is no margin, and without a margin
        # a candidate would be adopted for having been tried — which is the one
        # thing this whole loop is built not to do.
        return TuneOutcome(
            notes=[
                "the measurement did not record its per-question ranks, so there "
                "is no noise margin to judge a candidate against"
            ]
        )

    with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
        # In a thread — see `learn_profile`. Every retrieval candidate is a
        # full measurement pass over the eval set.
        outcome = await asyncio.to_thread(
            tune_once,
            embedder, q, items, scope=scope, profile=profile, baseline=baseline,
        )

    for note in outcome.notes:
        log.info("tune: %s", note)

    ref = _record(
        run_id,
        "tuning",
        store.write_json(
            "tuning",
            {
                "kind": outcome.kind,
                "label": outcome.label,
                "baseline_objective": round(outcome.baseline_objective, 4),
                "margin": round(outcome.margin, 4),
                "history": outcome.history,
                "notes": outcome.notes,
            },
        ),
    )
    _charge(
        run_id,
        Spend(
            stage="tuning",
            model=settings.gemini.embedding_model,
            input_tokens=embedder.usage.input_tokens,
            output_tokens=0,
            usd=price_for(
                settings.gemini.embedding_model, embedder.usage.input_tokens, 0
            ),
        ),
    )

    if outcome.kind == "retrieval" and outcome.retrieval is not None:
        # Free and already decided, so it is written straight into the profile:
        # the knob is a property of the family, not of this run.
        with _profile_lock(
            profile_dir(settings, registered.tenant_id),
            decision.slug or decision.fingerprint,
        ):
            fresh = _profile_for(settings, decision, registered.tenant_id)
            if fresh is not None:
                dc_replace(fresh, retrieval=outcome.retrieval).save(
                    profile_dir(settings, registered.tenant_id)
                )
        return TuneOutcome(
            kind="retrieval", label=outcome.label,
            baseline_objective=outcome.baseline_objective, margin=outcome.margin,
            notes=outcome.notes, report=ref,
        )

    if outcome.kind == "chunking" and outcome.chunk_rules is not None:
        candidate = dc_replace(
            decision,
            # Never `default`: `chunk_final` applies a decision's rules only when
            # the source is not that, so a candidate labelled `default` would be
            # silently ignored and the round would re-measure the chunking it was
            # trying to change.
            source="tuned",
            rules=rules_from_chunk_rules(decision.rules, outcome.chunk_rules),
        )
        return TuneOutcome(
            kind="chunking", label=outcome.label, candidate=candidate,
            baseline_objective=outcome.baseline_objective, margin=outcome.margin,
            notes=outcome.notes, report=ref,
        )

    return TuneOutcome(
        baseline_objective=outcome.baseline_objective, margin=outcome.margin,
        notes=outcome.notes, report=ref,
    )


def rules_from_chunk_rules(rules: ProfileRules, chunk_rules) -> ProfileRules:
    """`rules` with the candidate's chunking values, and nothing else changed.

    A `ProfileRules` is the fifteen scalars that cross a Temporal payload; a
    candidate only ever moves two of them. Copying the rest rather than rebuilding
    is what keeps a learned header pattern from being lost to a tuning round.
    """
    from dataclasses import replace as dc_replace

    return dc_replace(
        rules,
        target_chars=chunk_rules.target_chars,
        hard_cap_chars=chunk_rules.hard_cap_chars,
        overlap_chars=chunk_rules.overlap_chars,
        min_chunk_chars=chunk_rules.min_chunk_chars,
        max_embed_chars=chunk_rules.max_embed_chars,
    )


def _baseline_from(store: ArtifactStore, scores: Scores) -> object | None:
    """The measured run, rebuilt from the ranks its own report kept.

    `EvalRun` carries a reciprocal rank per question and is far too big for a
    Temporal payload, so `evaluate_index` reduced it to `Scores` and wrote the
    ranks into the scores artifact. Tuning needs both halves: the objective, which
    `Scores` has, and a bootstrap margin over those ranks, which it does not.

    **None rather than an approximation.** Deriving the vector from the mean
    understates the margin by about a third on a realistic run — measured at
    ±0.040 against a true ±0.062 — because a flat vector has none of the spread
    that ranks of 1, ½, ⅓, ¼ carry, and a margin that is too small accepts noise
    as a real gain. A round that cannot compute its own threshold has nothing to
    judge against, and declining is the only honest answer.
    """
    from docagent.evaluate import EvalRun

    if scores.report is None:
        return None
    try:
        report = store.read_json(scores.report)
    except Exception:
        return None
    ranks = report.get("reciprocal_ranks")
    if not ranks:
        # An older report, written before the ranks were kept.
        return None
    n = len(ranks)
    return EvalRun(
        hits_at_1=int(round(scores.recall_at_1 * n)),
        hits_at_5=int(round(scores.recall_at_5 * n)),
        reciprocal_ranks=list(ranks),
        questions=n,
    )


@activity.defn(name="promote_candidate_scores")
async def promote_candidate_scores(run_id: str, scores: Scores) -> Scores:
    """Make a kept candidate's measurement the run's measurement. Free.

    Only reached when the candidate beat the noise margin, which means the index
    it describes is the one that stands. On a revert this never runs and `scores`
    is still the baseline's, untouched — which is the whole reason the two are
    separate artifacts.
    """
    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    if scores.report is None:
        return scores
    ref = _record(
        run_id, "scores", store.write_json("scores", store.read_json(scores.report))
    )
    from dataclasses import replace as dc_replace

    return dc_replace(scores, report=ref)


@activity.defn(name="persist_profile_scores")
async def persist_profile_scores(
    run_id: str,
    extraction: Extraction,
    decision: ProfileDecision,
    evalset: EvalSet,
    scores: Scores,
) -> bool:
    """Write the measurement back into the family's profile. Free.

    The artifact is the authority — it is what `/runs/{id}` reads and what a
    rebuild can find again. The profile's copy is what lets a *family* accumulate
    measurement across documents, which is the whole reason the engine keeps one.

    Returns whether it wrote, because "no profile to write to" is an ordinary
    outcome — a document indexed with the measured defaults has no file of its
    own — and reporting it as success would be a lie about where the numbers went.
    """
    import time
    from dataclasses import replace

    from docagent import profiles as engine_profiles
    from docagent.profiles import EvalItem, Scores as EngineScores

    settings = _settings()
    root = profile_dir(settings, extraction.tenant_id)
    store = ArtifactStore(settings.workspace, run_id)

    with _profile_lock(root, decision.slug or decision.fingerprint):
        # Re-read *inside* the lock. `revisions` is a read-modify-write, and two
        # documents of one family can be in flight at once in this worker.
        profile = _profile_for(settings, decision, extraction.tenant_id)
        if profile is None:
            log.info("no profile for %s; scores stay in the artifact", decision.fingerprint)
            return False

        items = [EvalItem(**d) for d in store.read_json(evalset.items)]
        updated = replace(
            profile,
            scores=EngineScores(
                recall_at_1=scores.recall_at_1,
                recall_at_5=scores.recall_at_5,
                mrr_at_10=scores.mrr_at_10,
                recall_at_5_dense_only=scores.recall_at_5_dense_only,
                noise_floor=scores.noise_floor,
                chunks=scores.chunks,
                eval_questions=scores.eval_questions,
            ),
            evalset=items,
            # The eval set belongs to *this* document, so the record of which one
            # has to move with it — otherwise the next book of the family reuses
            # questions written from a book it has nothing to do with, and the
            # drop rule in `build_evalset` has nothing to compare against.
            learned_from=extraction.source_key or profile.learned_from,
            revisions=profile.revisions + 1,
            learned_at=time.time(),
        )
        updated.save(root)

    log.info("wrote scores into profile %s", updated.slug)
    return True


@contextlib.contextmanager
def _profile_lock(root: pathlib.Path, name: str):
    """Serialise the read-modify-write on one profile.

    `Profile.save` has no locking and `revisions` is incremented from a value read
    earlier, so two runs of one family would each write the count they saw and one
    measurement would vanish. An advisory `flock` is enough: the contenders are
    threads and processes on one machine sharing one volume, not two hosts.

    A filesystem that cannot lock — some network mounts — must not stop a run
    recording what it measured, so failure here degrades to no lock rather than
    to no scores.
    """
    import fcntl

    handle = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle = open(root / f".{name}.lock", "w")
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError as e:
        log.warning("could not lock profile %s (%s); writing unlocked", name, e)
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()


# ---------------------------------------------------------------------------
# Semantic extraction
# ---------------------------------------------------------------------------

SEMANTIC_SYSTEM = """Eres un extractor de conocimiento sobre textos académicos en español.
Devuelves solo lo que el fragmento afirma explícitamente. No infieres, no completas
con conocimiento general y no inventas relaciones entre conceptos que el texto no
enuncia. Si el fragmento no contiene ninguna afirmación sustantiva, devuelves listas
vacías: eso es una respuesta correcta y frecuente.

Cada afirmación lleva además `cita`: el tramo del fragmento del que sale, copiado
carácter por carácter. No lo parafrasees, no lo recortes a mitad de una palabra, no
corrijas su ortografía ni su puntuación, y no lo compongas juntando trozos separados
del texto. Tiene que ser un tramo contiguo que aparezca tal cual en el fragmento,
porque el código lo busca dentro de él: una cita que no se encuentra se descarta, y la
afirmación se queda sin nada con que comprobarse.

Cada concepto lleva `descripcion`: una frase sobre qué es ese concepto *según este
fragmento*, no según lo que tú sepas de él. Es lo único que hace que el grafo
devuelva algo más que un nombre. Si el fragmento no dice qué es, la dejas vacía.

Cada afirmación puede llevar `relaciona`: un SEGUNDO concepto que el propio
fragmento pone en relación con `concepto`. Solo lo rellenas si el texto enuncia esa
relación — no si los dos conceptos simplemente aparecen cerca. Que dos ideas salgan
en el mismo párrafo no es que el documento las relacione, y una relación que el
texto no afirma es exactamente lo que no debes inventar. Si no hay segunda idea,
lo dejas vacío, que es el caso más frecuente.

Cada afirmación lleva también `estado`, que dice qué hace el documento con ella:
`afirma` si el documento la sostiene, `niega` si la rechaza o la refuta, y `atribuido`
si la reporta como de otro — una posición que cita, la defienda o no. La distinción
importa: un texto que expone la doctrina que va a rebatir la enuncia con las mismas
palabras que quien la sostiene, y sin este campo las dos quedan idénticas."""

SEMANTIC_SCHEMA = {
    "type": "object",
    "properties": {
        "conceptos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "tipo": {"type": "string"},
                    "confianza": {"type": "number"},
                    # Free: the same call, a few more output tokens. Without it a
                    # graph traversal returns a bare name, which is most of why
                    # the concept layer is hard to read.
                    "descripcion": {"type": "string"},
                },
                "required": ["nombre", "confianza"],
            },
        },
        "afirmaciones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string"},
                    "concepto": {"type": "string"},
                    # The second concept, when the fragment states a relation
                    # between the two. Not required: most claims are about one
                    # idea, and demanding this would invite the model to invent
                    # the relation the prompt just told it not to.
                    "relaciona": {"type": "string"},
                    "confianza": {"type": "number"},
                    # The span of the chunk the claim came from, copied
                    # verbatim. Required, because the alternative is a model
                    # deciding per claim whether to supply the one field that
                    # makes the claim checkable.
                    "cita": {"type": "string"},
                    # Spanish on the wire, like the chunk `kind` values and for
                    # the same reason: these are stored and filtered on, and the
                    # UI is what localises them.
                    "estado": {
                        "type": "string",
                        "enum": ["afirma", "niega", "atribuido"],
                    },
                },
                "required": ["texto", "concepto", "confianza", "cita", "estado"],
            },
        },
    },
    "required": ["conceptos", "afirmaciones"],
}

#: Chunks per extraction call. Larger batches cost fewer calls but make it
#: harder to attribute a concept to the chunk that produced it, and an
#: unattributable relation cannot be used as evidence for a cited answer.
SEMANTIC_BATCH = 1


#: Condense a concept's accumulated descriptions into one. A Spanish port of
#: graphrag's `SUMMARIZE_PROMPT` (MIT), keeping its "resolve the contradictions"
#: instruction: two documents describing one concept differently is the normal
#: case in a library, not an error to paper over.
CONDENSE_SYSTEM = """Recibes un concepto y varias descripciones suyas, extraídas de
fragmentos distintos. Devuelves UNA descripción que recoja lo que dicen todas.

Reglas:
- No añades nada que no esté en las descripciones recibidas.
- Si se contradicen, lo dices en la descripción, en vez de elegir una y callar la
  otra.
- Escribes en tercera persona y nombras el concepto, para que la frase se entienda
  sola.
- Como máximo {words} palabras."""

#: The threshold in characters, which is the unit the text is actually in. This
#: worker has no tokenizer, so the conversion goes through the estimator's own
#: `CHARS_PER_TOKEN` rather than a second constant that could drift from it.
CONDENSE_CHARS = int(CONDENSE_TOKENS * CHARS_PER_TOKEN)


def _condense_descriptions(
    graph, adapter, concept_ids: list[str]
) -> tuple[int, int]:
    """Give each concept one description, paying only where it is needed.

    Returns `(written, paid_calls)`. Three outcomes per concept, in order of how
    often they happen: short enough to concatenate (free), long enough to condense
    (one call), and already rich enough to leave alone (free). The last one is
    what stops a concept mentioned by fifty chunks being re-summarised on every
    import.
    """
    rows = proj.read_concept_descriptions(graph, concept_ids)
    updates: list[dict] = []
    paid = 0

    for row in rows:
        raw = [str(d) for d in (row.get("raw") or []) if str(d).strip()]
        if not raw:
            continue
        joined = " ".join(raw)
        if len(joined) <= CONDENSE_CHARS:
            updates.append({"id": row["id"], "description": joined})
            continue
        if len(raw) > CONDENSE_SOURCE_CAP and row.get("description"):
            continue
        try:
            text = adapter.generate(
                json.dumps({"concepto": row["name"], "descripciones": raw},
                           ensure_ascii=False),
                system=CONDENSE_SYSTEM.format(words=CONDENSE_WORDS),
                stage="semantics",
            )
        except Exception as e:
            # The concatenation is a worse description, not a missing one, and
            # this step runs after everything else has already been projected.
            log.warning("could not condense %r: %s", row["name"], e)
            updates.append({"id": row["id"], "description": joined})
            continue
        paid += 1
        updates.append({"id": row["id"], "description": text.strip()})

    return proj.set_concept_descriptions(graph, updates), paid


#: Ask the model for what it missed, in the same conversation. Ported from
#: nano-graphrag's `entiti_continue_extraction` and graphrag's `CONTINUE_PROMPT`
#: (both MIT), which pair it with a second call asking "are there more? Y/N".
#:
#: That second call is folded into the schema here instead. Those projects need
#: it because their extraction returns delimited text; this one returns
#: structured output, so the model can be asked in the same breath — one call
#: per round rather than two, and nothing extra for the estimator to count.
GLEAN_PROMPT = """Se te escaparon conceptos y afirmaciones en la extracción anterior
del mismo fragmento.

Añade SOLO los que falten, con las mismas reglas: cita literal, estado, y nada que el
fragmento no enuncie. No repitas nada de lo que ya devolviste. Si no falta nada,
devuelves listas vacías, que es una respuesta correcta y frecuente.

`quedan` dice si después de esta respuesta todavía queda algo por extraer."""

SEMANTIC_GLEAN_SCHEMA = {
    "type": "object",
    "properties": {
        **SEMANTIC_SCHEMA["properties"],
        "quedan": {"type": "boolean"},
    },
    "required": [*SEMANTIC_SCHEMA["required"], "quedan"],
}


def _extract_passes(adapter, text: str, *, rounds: int) -> list[dict]:
    """One chunk's extraction, plus up to `rounds` gleaning passes over it.

    Each pass runs *in the same conversation*, which is what makes a second pass
    cheaper than a second independent extraction: the model can be told to add
    what it missed instead of being told not to repeat itself. It stops early
    when the model says nothing is left.

    Off by default (`Gemini.max_gleaning`). The two projects this is ported from
    both default to one pass; this pipeline has no measurement of its own yet,
    and its last measurement of this stage went against the intuition — semantics
    came out better with reasoning off, not on.
    """
    import json as _json

    first = adapter.generate_json(
        text, system=SEMANTIC_SYSTEM, schema=SEMANTIC_SCHEMA, stage="semantics"
    )
    passes = [first]
    if rounds <= 0:
        return passes

    history = [(text, _json.dumps(first, ensure_ascii=False))]
    for _ in range(rounds):
        more = adapter.generate_json(
            GLEAN_PROMPT, system=SEMANTIC_SYSTEM, schema=SEMANTIC_GLEAN_SCHEMA,
            stage="semantics", history=history,
        )
        passes.append(more)
        if not more.get("quedan"):
            break
        history.append((GLEAN_PROMPT, _json.dumps(more, ensure_ascii=False)))
    return passes


#: What the document does with a claim. Ported from graphrag's
#: `Claim Status: **TRUE**, **FALSE**, or **SUSPECTED**` (MIT), renamed for what
#: this corpus actually needs: the interesting third case here is not "unverified"
#: but "reported as somebody else's", which is how a theology text handles a
#: position it is about to reject.
CLAIM_STATES = frozenset({"afirma", "niega", "atribuido"})


def _status(value: object) -> str | None:
    """Keep a state the schema allows, and None for anything else.

    None rather than a default of `afirma`, because there is no safe default: a
    claim the document merely reports would be promoted to one it asserts, which
    is the exact confusion this field exists to remove. The read side renders the
    absence as `sin_estado`.
    """
    text = str(value or "").strip().lower()
    return text if text in CLAIM_STATES else None


def _locate_quote(quote: str, text: str, offset: int) -> tuple[str, int, int] | None:
    """Find a model-supplied quote inside the chunk it claims to come from.

    Returns the quote *as the document spells it* together with its absolute
    span, or None when the quote is not in the text at all.

    The idea of asking for the quote is ported from the `Claim Source Text`
    field of microsoft/graphrag's claim-extraction prompt (MIT). The checking is
    not: graphrag asks for the quote and nothing ever verifies it, which leaves
    the same hole this function exists to close — a claim nobody can check
    against the source still looks exactly like evidence.

    Matching is `quoting.find`, which is shared with the topic pass rather than
    written twice: it tolerates differences in whitespace and nothing else, and
    a second copy that tolerated one more thing would let one of the two
    features accept quotes the other refuses without either of them failing.

    The span indexes the *corrected* byte stream, like every other offset this
    pipeline stores (see `char_span` in `index_chunks`), not the original file.
    Correction changes the text's length, so a byte-exact pointer into the PDF is
    not something this function can produce.

    **`offset` is a byte offset and `re.Match.start()` is a character index**, so
    the match has to be converted before the two are added. Adding them directly
    shifted the span by one byte for every non-ASCII character earlier in the
    chunk — which in Spanish prose is nearly all of them. Measured 2026-09-03
    over the nine runs in this workspace that carry quote spans: **295 of 13,966
    stored spans, 2.1%, resolved to the quote they were recorded for**, while
    the chunks' own `char_span`s verified 600/600, 631/631 and so on against the
    same stream. Nothing failed, and `claims_verified` counted every one of them
    as verified — the quote *was* found, and the pointer stored for it was not.
    `docagent.chunk._sentence_spans` makes the same conversion for the same
    reason and says so.
    """
    match = quoting.find(quote, text)
    if match is None:
        return None
    verbatim = match.group(0)
    start = offset + len(text[: match.start()].encode("utf-8"))
    return verbatim, start, start + len(verbatim.encode("utf-8"))


@activity.defn(name="extract_semantics")
async def extract_semantics(
    run_id: str,
    registered: Registered,
    chunked: Chunked,
    options: StageOptions | None = None,
) -> Semantics:
    """Ask the model for concepts and claims, one chunk at a time.

    One chunk per call is deliberate. Batching would be cheaper, but every
    relation this produces must carry the `source_chunk_id` that lets a person
    check it — and a model given ten chunks reliably attributes claims to the
    wrong one. A relation nobody can verify is worse than no relation, because
    it still looks like evidence.
    """
    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    rows = list(store.iter_jsonl(chunked.chunks))

    # Concept ids are salted with it: two customers who both talk about "Dios"
    # must not share a node, because `description_raw` accumulates the text of
    # every chunk that mentions it.
    tenant = registered.tenant_id
    adapter = VertexAdapter(_provider())
    concepts: dict[str, dict] = {}
    claims: list[dict] = []
    edges: list[proj.SemanticEdge] = []
    #: Claims whose quote was not in the chunk. Counted rather than only logged:
    #: a claim with no verifiable quote is a *degraded* claim, and the number of
    #: them is the difference between a degradation the user can see and one that
    #: hides behind a count of claims that all look equally good.
    unverified = 0

    for done, row in enumerate(rows):
        # One heartbeat per chunk, which is the only unit of progress this stage
        # has: extraction is deliberately one call per chunk, so a count of
        # chunks is a count of calls and of spend. `/runs/{workflow_id}` reads it
        # back off `describe().pending_activities`, which is what turns "this is
        # running" into "598 chunks, 96 done" for somebody deciding whether to
        # let it finish.
        #
        # The workflow sets `heartbeat_timeout` against this, at sixty times the
        # interval below. It was left unset at first, on the reasoning that a
        # late heartbeat would retry an expensive stage — and a worker restart
        # then orphaned this activity for what would have been three more hours
        # before the same retry happened anyway. See `PAID_HEARTBEAT_TIMEOUT`.
        #
        # **This call only records; the event loop is what sends.** That is why
        # the extraction below runs in a thread, and the two lines are one
        # decision: with the synchronous call inline, this loop held the worker's
        # only event loop for the whole document, no heartbeat was ever flushed,
        # and the timeout fired on an activity that was working perfectly.
        # Guarded because every test in `tests/activities/` calls these as plain
        # functions rather than through a worker — which is the pattern that
        # keeps them cheap to test — and `heartbeat` raises outside an activity
        # context. `in_activity()` is the SDK's own answer for code that has to
        # run both ways.
        if activity.in_activity():
            activity.heartbeat(done, len(rows))
        chunk = make_chunk_id(registered.version_id, row["index"])
        try:
            # In a thread, like `activities/removing.py` and `asking.py`, and for
            # the reason `asking.py` states: a synchronous network call left on
            # the activity's own event loop blocks everything that loop also
            # owes — the heartbeat flush above, this workflow's task, every
            # query the UI polls with. `_extract_passes` is one such call per
            # chunk, so the block lasts the whole document.
            #
            # **Measured 2026-08-31 on `ver_0b71d21eeb3228f54437d9cf`**, 600
            # chunks: the loop was held for 50 minutes, the 5-minute heartbeat
            # timeout fired at minute five, and the blocked coroutine could not
            # see the cancellation either — so it ran all 600 chunks, projected
            # them, wrote the artifact and billed $4.72 into a slot Temporal had
            # closed 45 minutes earlier. `_PAID_RETRY` then did it again:
            # $9.45 for a run that ended `failed`. Awaiting here yields once per
            # chunk, which is the ~5s interval the timeout was calibrated
            # against in the first place.
            passes = await asyncio.to_thread(
                _extract_passes,
                adapter, row["text"], rounds=settings.gemini.max_gleaning,
            )
        except Exception as e:
            # One unparseable chunk must not lose the whole document's
            # semantics; structure and citations are already projected and the
            # document stays browsable either way.
            log.warning("semantic extraction failed for chunk %s: %s", chunk, e)
            continue

        # A gleaning pass is asked for what the previous ones *missed*, so a
        # claim it returns on ground already covered is by construction not new.
        # Deduplicating on the claim id — the same key the graph MERGEs on — and
        # on the verified span keeps a restatement from becoming a second node.
        seen_ids: set[str] = set()
        seen_spans: set[tuple[int, int]] = set()

        for parsed in passes:
            # Spans found by *this* pass are held back until it ends. The rule
            # above is about a gleaning pass restating ground an earlier one
            # covered; inside a single pass two different claims quoting one
            # sentence are the ordinary case, and dropping the second one is
            # exactly the pair `status` exists to keep apart — a text
            # enunciating the doctrine it is about to rebut cites the same words
            # for `afirma` and for `niega`. With `max_gleaning` at 0 there is
            # only ever one pass, so before this the dedup could do nothing but
            # that. Measured over the 40 semantics artifacts in this workspace:
            # 128 pairs of distinct claims in one chunk share a quote *start*
            # and 736 overlap, out of 40,019 same-chunk pairs — the pairs that
            # shared an exact span are the ones that were never written, so
            # their number cannot be read back out of the artifacts.
            pass_spans: set[tuple[int, int]] = set()
            for item in parsed.get("conceptos", []) or []:
                name = (item.get("nombre") or "").strip()
                if not name:
                    continue
                confidence = float(item.get("confianza", 0.0))
                # Keyed on the canonical form, which is what `project_concepts`
                # derives the node id from. Keyed on the raw spelling, "Cuerpo"
                # and "cuerpo" became two rows of one `UNWIND` for one node —
                # so `SET k.type = row.type` let the last row win, and a row
                # reached only as a claim's subject carries `type: None`.
                # Measured over the 40 semantics artifacts here: **1,169 groups
                # of rows share a canonical key, 1,199 rows more than there are
                # nodes**, and 4 of those groups end on an untyped row that
                # wipes a type the model was paid for. The count goes out too:
                # `ingest-1788222510755-1ba85855` reports 2,612 concepts where
                # the graph holds 2,484.
                key = canonical_concept(name)
                entry = concepts.setdefault(
                    key, {"name": name, "type": item.get("tipo"), "descriptions": []}
                )
                if entry["type"] is None and item.get("tipo"):
                    # A type learned from any chunk beats the absence of one.
                    entry["type"] = item.get("tipo")
                # Deduplicated as it accumulates. Two chunks often describe a
                # concept in the same words, and the condensation step is charged
                # by length.
                described = (item.get("descripcion") or "").strip()
                if described and described not in entry["descriptions"]:
                    entry["descriptions"].append(described)
                edges.append(
                    proj.SemanticEdge(
                        type="MENTIONS",
                        source_id=chunk,
                        target_id=make_concept_id(name, tenant),
                        confidence=confidence,
                        extractor_model=settings.gemini.model,
                        source_chunk_id=chunk,
                    )
                )

            for item in parsed.get("afirmaciones", []) or []:
                text = (item.get("texto") or "").strip()
                about = (item.get("concepto") or "").strip()
                if not text or not about:
                    continue
                confidence = float(item.get("confianza", 0.0))
                concepts.setdefault(
                    canonical_concept(about),
                    {"name": about, "type": None, "descriptions": []},
                )
                claim = {
                    "text": text,
                    "confidence": confidence,
                    "source_chunk_id": chunk,
                    "status": _status(item.get("estado")),
                }
                # A quote that is not in the chunk costs the claim its quote, not
                # its existence. The claim is still a reading of a chunk a person
                # can open; what it loses is the span that would have taken them
                # to the exact sentence.
                located = _locate_quote(
                    str(item.get("cita") or ""), row["text"], int(row["char_from"])
                )
                if located is not None:
                    verbatim, start, end = located
                    claim["quote"] = verbatim
                    claim["quote_char_start"] = start
                    claim["quote_char_end"] = end

                # `claim_id` is the key the graph MERGEs on, so deduplicating on
                # it here means a restatement from a later pass converges instead
                # of becoming a second node. The span is checked too: a gleaning
                # pass citing ground an earlier one already used is restating it,
                # since gleaning is asked only for what was missed.
                cid = proj.claim_id(chunk, text)
                span = (
                    (claim["quote_char_start"], claim["quote_char_end"])
                    if located is not None else None
                )
                if cid in seen_ids or (span is not None and span in seen_spans):
                    continue
                seen_ids.add(cid)
                if span is not None:
                    pass_spans.add(span)

                # Counted *after* the dedup, so the number refers to the claims
                # actually stored. Counting it above let a duplicate with an
                # unlocatable quote be tallied and then dropped, which is how a
                # verified count starts exceeding what it counts.
                if located is None:
                    unverified += 1
                claims.append(claim)
                edges.append(
                    proj.SemanticEdge(
                        type="ABOUT",
                        source_id=proj.claim_id(chunk, text),
                        target_id=make_concept_id(about, tenant),
                        confidence=confidence,
                        extractor_model=settings.gemini.model,
                        source_chunk_id=chunk,
                    )
                )

                # The second concept, when the fragment related two. This is the
                # graph's only concept-to-concept path: everything else joins two
                # concepts through a chunk that mentioned both, which is
                # co-occurrence rather than anything the document said.
                #
                # Self-relations are dropped — a claim relating a concept to
                # itself is the model restating `concepto`, and it would add a
                # loop the traversal has to filter out on every read.
                related = (item.get("relaciona") or "").strip()
                if related and make_concept_id(related, tenant) != make_concept_id(about, tenant):
                    concepts.setdefault(
                        canonical_concept(related),
                        {"name": related, "type": None, "descriptions": []},
                    )
                    edges.append(
                        proj.SemanticEdge(
                            type="INVOLVES",
                            source_id=proj.claim_id(chunk, text),
                            target_id=make_concept_id(related, tenant),
                            confidence=confidence,
                            extractor_model=settings.gemini.model,
                            source_chunk_id=chunk,
                        )
                    )

            seen_spans |= pass_spans

    condense_spend: Spend | None = None
    with Graph(settings.memgraph_url) as graph:
        graph.ensure_schema()
        proj.project_concepts(graph, list(concepts.values()), tenant=tenant)
        proj.project_claims(graph, claims, tenant=tenant)
        written = proj.project_semantic_edges(graph, edges)

        # And delete what a *previous* extraction of this version left. All
        # three projections above are `MERGE`s, so a re-index adds rather than
        # replaces — and a stale claim is not inert debris: `claim_id` keys on
        # the chunk and the text, and `chunk_id` keys on the version and the
        # index, so re-chunking keeps every id alive while the text underneath
        # changes. Measured once at 4,988 claims left behind out of 8,043.
        # After the projection, never before: see `prune_semantics`.
        pruned = proj.prune_semantics(
            graph, registered.version_id, claims=claims, edges=edges
        )
        if pruned.claims or pruned.mentions:
            log.info(
                "pruned %d stale claim(s) and %d stale MENTIONS from %s, "
                "collecting %d orphan concept(s)",
                pruned.claims, pruned.mentions, registered.version_id,
                pruned.concepts_collected,
            )

        # After projection, so the descriptions being condensed include this
        # run's. Its own adapter, so its tokens land in their own ledger row
        # rather than inflating the per-chunk extraction they are not part of.
        if options is not None and options.condense_descriptions:
            condenser = VertexAdapter(_provider())
            names = [make_concept_id(c["name"], tenant) for c in concepts.values()]
            described, paid_calls = _condense_descriptions(graph, condenser, names)
            log.info(
                "condensed %d concept description(s), %d of them paid for",
                described, paid_calls,
            )
            condense_spend = _charge(
                run_id,
                Spend(
                    stage="semantics-condense",
                    model=settings.gemini.model,
                    input_tokens=condenser.usage.input_tokens,
                    output_tokens=condenser.usage.output_tokens,
                    usd=price_for(
                        settings.gemini.model,
                        condenser.usage.input_tokens,
                        condenser.usage.output_tokens,
                    ),
                ),
            )

    # Persisted *after* projecting, so the artifact only ever describes a graph
    # write that succeeded. This is the most expensive output of the pipeline to
    # reproduce — one generation call per chunk — and until now it existed
    # nowhere but in Memgraph, so dropping the graph meant paying for it again.
    _record(
        run_id,
        "semantics",
        store.write_json(
            "semantics",
            {
                "extractor_model": settings.gemini.model,
                "concepts": list(concepts.values()),
                "claims": claims,
                "edges": [asdict(e) for e in edges],
            },
        ),
    )

    if unverified:
        log.warning(
            "%d of %d claim(s) quoted text that is not in their chunk; they are "
            "stored without a span and cannot be opened at the sentence",
            unverified, len(claims),
        )

    spend = _charge(
        run_id,
        Spend(
            stage="semantics",
            model=settings.gemini.model,
            input_tokens=adapter.usage.input_tokens,
            output_tokens=adapter.usage.output_tokens,
            usd=price_for(
                settings.gemini.model,
                adapter.usage.input_tokens,
                adapter.usage.output_tokens,
            ),
        ),
    )
    return Semantics(
        concepts=len(concepts),
        claims=len(claims),
        claims_verified=len(claims) - unverified,
        edges=written,
        spend=spend,
        condense_spend=condense_spend,
    )
