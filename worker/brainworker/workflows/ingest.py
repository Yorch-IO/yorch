"""The ingest pipeline, with a free approval gate before anything is paid for.

The shape of this workflow is dictated by one measurement. From a real run
(`docaget/costo.json`): correction $0.0334, eval-set generation $0.0131,
embedding $0.0033. **Correction dominates, not embedding** — which is the
opposite of the usual assumption about RAG pipelines, and it is why the gate
sits before correction rather than before embedding, and why every stage is
switchable there instead of the whole run being one yes/no.

A second constraint sets the stage order and cannot be worked around: correction
runs *before* chunking, because it changes the text's length and every
`char_span` is a byte offset into that text. So the chunks shown at the gate are
not the chunks that get indexed whenever correction is on, and
`Preview.chunks_are_final` says so rather than leaving the UI to imply otherwise.

Durability is the reason this is a workflow at all. A gate that a user may take
a day to answer cannot live in a process that a laptop lid closing would end.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.exceptions import CancelledError as TemporalCancelledError

with workflow.unsafe.imports_passed_through():
    from ..activities import exporting as export
    from ..activities import ingest as act
    from ..activities import paid
    from ..pipeline import (
        Chunked,
        Correction,
        Estimate,
        EvalSet,
        Extraction,
        Indexed,
        Scores,
        Semantics,
        TuneOutcome,
        GateReport,
        IngestRequest,
        IngestResult,
        Preview,
        ProfileDecision,
        Spend,
        Registered,
        RunOpen,
        StageOptions,
        Staged,
        run_kind_of,
    )

#: Free stages are fast and local; a long timeout here only delays the report of
#: a hung extractor. Extraction of a 900-page PDF is the outlier, hence minutes
#: rather than seconds.
FREE_TIMEOUT = timedelta(minutes=20)

#: Catalog and graph writes are small and local.
WRITE_TIMEOUT = timedelta(minutes=2)

#: Paid stages are network-bound, sequential and long. A measured correction
#: batch of 22,946 chars took 55.8 s, and a book is many batches — so this is
#: hours, not minutes. The provider's own retry ceiling is what stops a genuinely
#: stuck call, not this.
PAID_TIMEOUT = timedelta(hours=4)

#: How long an activity that heartbeats may go quiet before Temporal gives up on
#: the attempt and retries it.
#:
#: **This was left unset, and the first real event proved that wrong.** The
#: reasoning against it was that a heartbeat merely arriving late would fail and
#: retry the activity, and a retry of semantic extraction re-runs every
#: generation call from the start — so detecting a stall looked worth less than
#: never paying twice for a slow one.
#:
#: What that missed is that without it nothing is detected at all. On 2026-08-31
#: a worker restart — an ordinary event, and one the documented deploy command
#: causes — left `extract_semantics` orphaned in `Started`, 47 minutes and 413
#: generation calls in, with the worker that ran it gone and the new one idle.
#: Temporal would not have noticed until `PAID_TIMEOUT` expired: **three more
#: hours of nothing, and then the same retry from scratch anyway.** The cost the
#: original reasoning was avoiding turned out to be the cost it was paying, plus
#: the wait.
#:
#: Five minutes is sixty times the observed interval between heartbeats, which is
#: one per chunk at roughly one every five seconds. Tripping it spuriously needs
#: a single generation call to stall for five minutes, which nothing measured
#: here comes close to.
#:
#: **That last sentence was true and the timeout fired anyway, twice, on the
#: very next run.** It assumed a heartbeat that is recorded is a heartbeat that
#: is sent. `activity.heartbeat` only records; the activity's event loop flushes
#: it — and `extract_semantics` was an `async def` with no `await` in it,
#: running a synchronous per-chunk network call, so the loop was held for the
#: whole document and flushed nothing. Measured on
#: `ver_0b71d21eeb3228f54437d9cf`, 600 chunks: both attempts completed all 600
#: calls, projected them and billed ~$4.72 each into an attempt Temporal had
#: already failed — **$9.45 for a run that ended `failed`**, and a worker frozen
#: for 97 minutes (its workflow queries came back all at once as
#: `query task not found, or already expired`).
#:
#: So this constant is sound and was never the bug. The bug was that nothing
#: could send what it measures; the extraction runs in a thread now. A heartbeat
#: timeout on this stage again means a genuinely stalled call, which is what it
#: was always supposed to mean.
PAID_HEARTBEAT_TIMEOUT = timedelta(minutes=5)

#: A paid activity is retried far less eagerly than a free one. Every attempt
#: spends real money, and the provider already retries the transient failures
#: internally — so a Temporal retry here means the whole stage runs again.
_PAID_RETRY = RetryPolicy(maximum_attempts=2)

#: How long the gate waits for a person. A desktop app's user may close the lid
#: on Friday and approve on Monday, so this is days rather than hours — but it
#: is bounded, because a workflow that waits forever is one that never reports
#: an outcome and quietly accumulates in the namespace.
GATE_TIMEOUT = timedelta(days=7)

_RETRY = RetryPolicy(maximum_attempts=3)

#: The one patch id in this codebase, shared by both ingest workflows.
#:
#: `workflow.patched` is how a new **first** activity is added to a workflow that
#: already has executions in flight: inserting a command at the head of the
#: sequence is a non-determinism error on replay, and a gate parked for seven
#: days is exactly the history that would hit it. Both workflows use the same id
#: because it is one change; patch ids are scoped per workflow type.
_RUN_ROW_FIRST = "run-row-before-the-first-activity"


def _basename(source_path: str) -> str:
    """The last segment of a path, for the queue to show before there is a title.

    Written with `rsplit` rather than `pathlib`, because this runs inside the
    workflow sandbox and the answer must not depend on which OS the worker is on:
    `PurePath` would split on `\\` on Windows and not on Linux, and a workflow
    that replays on a different host must produce the same string.
    """
    for sep in ("/", "\\"):
        source_path = source_path.rsplit(sep, 1)[-1]
    return source_path


@dataclass
class Approval:
    """The gate's answer. Carries the per-stage switches, never a credential."""

    approved: bool
    options: StageOptions = field(default_factory=StageOptions)
    reason: str = ""


@workflow.defn(name="IngestWorkflow")
class IngestWorkflow:
    def __init__(self) -> None:
        self._approval: Approval | None = None
        self._report: GateReport | None = None
        self._correction: Correction | None = None
        self._stage: str = "starting"
        #: The audit trail's total order, and its idempotency key.
        #:
        #: A counter on the workflow object rather than a sequence in the
        #: database, because Temporal retries the activity that writes the row:
        #: a retried transition has to carry the number it carried the first
        #: time, or the trail grows a duplicate every time the catalog blinks.
        #: `workflow.now()` is fixed at the same moment for the same reason.
        self._seq: int = 0
        #: Whether `register_document` has run, which is whether a `run` row
        #: exists to hang an event off. `_insert_event` derives its tenant from
        #: that row, so an event written before it is silently dropped.
        self._registered: bool = False
        #: Transitions that happened before the row existed, flushed once it
        #: does. Two, in practice: `staging` and `registering`.
        self._pending: list[dict[str, object]] = []

    # -- signals and queries ----------------------------------------------

    @workflow.signal
    def approve(self, approval: Approval) -> None:
        """Answer the gate.

        Deliberately last-write-wins rather than first: a user who changes their
        mind about a stage before the run resumes should get the later answer,
        and the workflow only reads it once it has stopped waiting.
        """
        self._approval = approval

    @workflow.query
    def gate_report(self) -> GateReport | None:
        """What the approval screen renders. None until the free stages finish."""
        return self._report

    @workflow.query
    def stage(self) -> str:
        return self._stage

    @workflow.query
    def correction(self) -> Correction | None:
        """What correction did, for the second gate's diff view."""
        return self._correction

    # -- run ---------------------------------------------------------------

    @workflow.run
    async def run(self, request: IngestRequest, options: StageOptions) -> IngestResult:
        run_id = workflow.info().workflow_id

        try:
            return await self._run(request, options, run_id)
        except asyncio.CancelledError:
            # A cancellation is an outcome, not a crash, and it has to reach the
            # catalog or the run reads `running` for ever — the same lie
            # `_set_stage` used to tell, arrived at from the other direction.
            #
            # This is the quiet half: cancelled with nothing in flight, which in
            # practice means at a gate. The half that matters is in the
            # `ActivityError` branch below.
            #
            # Re-raised, so Temporal still records the execution as CANCELED:
            # swallowing it would report success for a run somebody stopped.
            #
            # The bookkeeping write is deliberately *not* wrapped in
            # `asyncio.shield`. That is the usual Python-SDK answer for cleanup
            # after cancellation, and it was tried here — but on temporalio
            # 1.31.0 the write completes without it in both paths, checked by
            # removing it and watching the two tests still pass. Defensive code no
            # test exercises is code that rots; if a later SDK does cancel this
            # write, `test_a_cancelled_run_records_the_outcome_rather_than_reading_running`
            # and its mid-activity sibling fail, and the shield goes back in with
            # a reason.
            await self._finish(run_id, "cancelled")
            raise
        except ActivityError as e:
            # **A cancellation arrives here, not above, whenever an activity was
            # running** — which is every interesting case, because a person stops
            # a run while it is spending, not while it waits at a gate. The
            # activity is cancelled first, so what propagates is an
            # `ActivityError` wrapping a cancellation rather than a bare
            # `CancelledError`, and without this branch the run was recorded as
            # `failed`. Measured by writing the test before the branch: it
            # asserted `cancelled` and got `failed`.
            #
            # They are different outcomes with different fixes. A failure sends
            # somebody to a log; a cancellation is the thing the person just
            # asked for, and — like the gate timeout — it costs nothing and
            # leaves the document importable, so it is not a failure.
            if isinstance(e.cause, TemporalCancelledError):
                await self._finish(run_id, "cancelled")
                raise
            # The catalog has to record the failure even though the workflow is
            # about to fail: a run that vanished without a row is indistinguishable
            # from one that never started, and the UI has nothing to show the user.
            await self._record_failure(run_id, e)
            raise

    async def _run(
        self, request: IngestRequest, options: StageOptions, run_id: str
    ) -> IngestResult:
        await self._open(request, run_id)

        await self._enter(run_id, "staging")
        staged: Staged = await workflow.execute_activity(
            act.stage_source,
            request,
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        await self._enter(run_id, "registering")
        registered: Registered = await workflow.execute_activity(
            act.register_document,
            args=[request, staged, run_id, workflow.info().workflow_id],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )
        # The run row exists from here, so the two transitions that preceded it
        # can be written and every later one goes straight through.
        await self._flush_pending(run_id)

        # These exact bytes are already indexed under another path. The link was
        # written by `register_document`; re-running the pipeline would spend
        # money to produce vectors that already exist and would then let the two
        # copies compete in ranking.
        # `reindex` is the user asking for this on purpose — after a collection
        # was dropped, or to pick up a corrected profile — so the duplicate
        # guard is exactly what must not fire.
        if registered.already_indexed and not request.reindex:
            # The catalog linked the new path inside `register_document`; the
            # graph has to learn about it too, or the Library screen shows one
            # copy and the catalog holds two.
            await workflow.execute_activity(
                act.link_duplicate,
                args=[request, staged, registered],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=_RETRY,
            )
            await self._finish(run_id, "succeeded")
            return IngestResult(
                run_id=run_id,
                document_id=registered.document_id,
                version_id=registered.version_id,
                state="already_indexed",
                detail="contenido idéntico ya indexado; se enlazó la nueva ruta",
            )

        # The first extraction runs with no rules, and has to: the fingerprint
        # that selects a profile is computed from the evidence this pass
        # produces, so which rules apply is unknowable until the document has
        # been read once.
        await self._enter(run_id, "extracting")
        extraction: Extraction = await workflow.execute_activity(
            act.extract_text,
            # `None` is passed explicitly rather than left to the default.
            # Temporal maps payloads onto an activity's parameters by **arity**:
            # hand a three-parameter activity two arguments and the converter
            # cannot line them up, so it gives up and passes raw dicts — the
            # activity then dies on `'dict' object has no attribute
            # 'source_path'`, three frames from anything that names the cause.
            args=[request, run_id, None],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        await self._enter(run_id, "profiling")
        decision: ProfileDecision = await workflow.execute_activity(
            act.resolve_profile,
            args=[registered.version_id, run_id, extraction, options],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )
        extraction = await self._reextract(request, run_id, extraction, decision)

        await self._enter(run_id, "previewing")
        preview: Preview = await workflow.execute_activity(
            act.preview_chunks,
            args=[run_id, extraction, options, decision],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        estimate: Estimate = await workflow.execute_activity(
            act.estimate_cost,
            args=[preview, options, decision, run_id],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

        self._report = GateReport(
            run_id=run_id,
            document_id=registered.document_id,
            version_id=registered.version_id,
            preview=preview,
            estimate=estimate,
            profile_warnings=decision.warnings,
            profile=decision,
        )

        approval = await self._gate(request, options, run_id)
        if not approval.approved:
            await self._finish(run_id, "cancelled")
            return IngestResult(
                run_id=run_id,
                document_id=registered.document_id,
                version_id=registered.version_id,
                state="rejected",
                detail=approval.reason or "no aprobado en la compuerta",
            )

        approved = approval.options
        spent: list[Spend] = []

        # Learning comes first among the paid stages, and before correction, so
        # that correction runs on header-stripped text and the final chunking
        # sees the final rules. Learning after correction would mean correcting
        # text that a re-extraction is about to replace.
        if (
            approved.learn_profile
            and decision.source == "default"
            and not approved.ignore_profile
            and not extraction.structured
        ):
            await self._enter(run_id, "learning")
            decision = await workflow.execute_activity(
                paid.learn_profile,
                args=[run_id, extraction, decision],
                start_to_close_timeout=PAID_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            if decision.spend is not None:
                spent.append(decision.spend)
            extraction = await self._reextract(request, run_id, extraction, decision)

        # Correction next, and *before* chunking, because it changes the text's
        # length and every char_span is a byte offset into that text. Structured
        # sources skip it: an LLM must never rewrite a cell value.
        # Which artifact holds the text to correct and chunk. A profile with
        # header patterns caused a second extraction, and that pass wrote
        # `extracted_text`; without one there is only the rules-free `raw_text`.
        text_kind = (
            "extracted_text" if decision.rules.needs_reextraction else "raw_text"
        )
        correction: Correction | None = None
        if approved.correct and not extraction.structured:
            await self._enter(run_id, "correcting")
            correction = await workflow.execute_activity(
                paid.correct_text,
                args=[run_id, extraction],
                start_to_close_timeout=PAID_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            spent.append(correction.spend)
            text_kind = "corrected_text"
            self._correction = correction

            if approved.review_correction:
                # **`review`, not `decision`.** This used to reassign `decision`,
                # which is the `ProfileDecision` every later stage reads — so a
                # run that went through the second gate handed an `Approval` to
                # `chunk_final` in its place. Temporal's converter coerced it to
                # a `ProfileDecision` with defaults rather than failing, so the
                # document was silently chunked with the engine's built-in rules
                # and the profile it had just paid to learn was discarded. No
                # test saw it: the run still succeeded and still produced chunks.
                #
                # It surfaced only when a later stage read `decision.source` and
                # got an attribute that is not on an `Approval` — which failed
                # the workflow task, which Temporal retries for ever, which is a
                # hung run rather than a wrong one.
                review = await self._second_gate(run_id)
                if not review.approved:
                    await self._finish(run_id, "cancelled")
                    return IngestResult(
                        run_id=run_id,
                        document_id=registered.document_id,
                        version_id=registered.version_id,
                        state="rejected_after_correction",
                        total_usd=_total(spent),
                        detail=review.reason or "corrección no aceptada",
                    )
                approved = review.options

        # The chunks that actually get indexed. Always recomputed rather than
        # reused from the preview: after correction the preview's offsets index
        # a byte stream that no longer exists.
        await self._enter(run_id, "chunking")
        chunked: Chunked = await workflow.execute_activity(
            paid.chunk_final,
            args=[
                run_id,
                text_kind if not extraction.structured else "structured_chunks",
                decision,
            ],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        await self._enter(run_id, "projecting")
        projected: dict[str, int] = await workflow.execute_activity(
            act.project_structure,
            args=[request, staged, registered, run_id, chunked.chunks],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        # Between projecting and embedding, and that position is a decision.
        # After projecting, because the book is made of the same chunk rows the
        # graph was just built from and a failure here must not cost a document
        # its structure. Before embedding, because it is the free half of the
        # run: a person who ticked this and nothing else gets their book without
        # waiting on the longest paid stage in the pipeline.
        if approved.build_epub:
            await self._enter(run_id, "epub")
            metadata: Spend | None = await workflow.execute_activity(
                export.resolve_book_metadata,
                args=[run_id, registered, chunked.chunks],
                start_to_close_timeout=FREE_TIMEOUT,
                # Paid, and therefore two attempts rather than three. It returns
                # `None` far more often than it spends — once per document ever,
                # and never for a video.
                retry_policy=_PAID_RETRY,
            )
            if metadata is not None:
                spent.append(metadata)
            await workflow.execute_activity(
                export.build_epub,
                args=[run_id, request.library_id, registered, chunked.chunks],
                start_to_close_timeout=FREE_TIMEOUT,
                retry_policy=_RETRY,
            )

        indexed: Indexed | None = None
        if approved.embed:
            await self._enter(run_id, "embedding")
            indexed = await workflow.execute_activity(
                paid.embed_and_index,
                args=[run_id, request.library_id, registered, staged, chunked],
                start_to_close_timeout=PAID_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            spent.append(indexed.spend)

        # Measured *before* semantics, and only when there is an index to
        # measure. Semantics is the longest paid stage and the one most likely to
        # be cut short; losing the measurement to it would mean paying for the
        # questions and never asking them.
        scores: Scores | None = None
        if approved.generate_evalset and approved.embed:
            await self._enter(run_id, "evaluating")
            evalset: EvalSet = await workflow.execute_activity(
                paid.build_evalset,
                args=[
                    run_id, extraction, chunked, decision,
                    # Tuning needs the bigger sample or its own comparison
                    # cannot resolve the effect it is looking for.
                    paid.EVAL_SAMPLE_TUNING if approved.tune else paid.EVAL_SAMPLE,
                ],
                start_to_close_timeout=PAID_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            if evalset.questions:
                # Reusing this family's questions costs nothing, and a `Spend`
                # of zero is not the same claim as no spend at all.
                if not evalset.reused:
                    spent.append(evalset.spend)
                scores = await workflow.execute_activity(
                    paid.evaluate_index,
                    # `"scores"` is passed explicitly rather than left to the
                    # default. **Temporal maps payloads onto an activity's
                    # parameters by arity**: hand a six-parameter activity five
                    # arguments and the converter cannot line them up, so it
                    # gives up and passes raw dicts — and the activity then died
                    # on `'builtin_function_or_method' object has no attribute
                    # 'path'`, because `evalset` had arrived as a dict and
                    # `dict.items` is a method. Same failure as the
                    # `'dict' object has no attribute 'source_path'` this file
                    # already carries a warning about, reached from the other
                    # direction: there by adding a parameter, here by adding one
                    # and leaving an older call site short.
                    args=[run_id, registered, chunked, evalset, decision, "scores"],
                    start_to_close_timeout=PAID_TIMEOUT,
                    retry_policy=_PAID_RETRY,
                )
                if scores.spend is not None:
                    spent.append(scores.spend)
                if approved.tune:
                    chunked, indexed, scores = await self._tune_once(
                        run_id, request, registered, staged, chunked, evalset,
                        decision, scores, text_kind, spent, indexed,
                    )

                # Free, and last: the artifact is the authority, so a failure to
                # write the profile copy must not cost the run its measurement.
                await workflow.execute_activity(
                    paid.persist_profile_scores,
                    args=[run_id, extraction, decision, evalset, scores],
                    start_to_close_timeout=WRITE_TIMEOUT,
                    retry_policy=_RETRY,
                )

        semantics: Semantics | None = None
        if approved.extract_semantics:
            await self._enter(run_id, "semantics")
            semantics = await workflow.execute_activity(
                paid.extract_semantics,
                args=[run_id, registered, chunked, approved],
                start_to_close_timeout=PAID_TIMEOUT,
                # Set here and nowhere else. It used to say this was "the only
                # activity that heartbeats", and that was never true of the
                # code: `embed_and_index` is given a heartbeat callback too, and
                # documents at length why it needs one. It read as true because
                # those heartbeats never *arrived* — the stage ran its embedding
                # inline on the worker's event loop, so the same call that
                # recorded a heartbeat was holding the loop that had to flush
                # it. Both stages hand their work to a thread now and both
                # really do heartbeat, so this could be set on `embed_and_index`
                # as well. It deliberately is not: arming a timeout on a stage
                # that spends is a decision to make on purpose and measure, not
                # a side effect of fixing the loop.
                heartbeat_timeout=PAID_HEARTBEAT_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            spent.append(semantics.spend)
            if semantics.condense_spend is not None:
                spent.append(semantics.condense_spend)

        # **A structural collision withholds activation.** The fingerprint that
        # selects a profile is structural, and structure is not subject matter:
        # a hermeneutics chapter and a church-history book landed on one
        # fingerprint on the real corpus, and the second was chunked with the
        # first's heading rules. Retrieval metrics cannot see that — the eval
        # questions are generated from the very chunks the wrong rules produced —
        # so the only automatic signal is that the two rule sets disagree about
        # how many chapters this document has.
        #
        # Everything else already ran. The index exists, the graph is projected,
        # the artifacts are kept and the *previous* version stays answerable —
        # which is the point: the cost of being wrong here is a document nobody
        # can find, and that is worse than a document one click from being
        # findable. Only `activate_version` is skipped.
        #
        # A rule-learning fallback is deliberately **not** in this list. After
        # three failed attempts the engine adopts its built-in defaults, which
        # were themselves measured on a real book; treating that as a blocker
        # would refuse to publish documents whose only fault is being ordinary.
        blocked = self._structural_block(decision, approved)
        if blocked:
            # Before `_finish`, not after: the terminal event names the stage
            # the run was in when it ended, and this is that stage.
            self._stage = "blocked"
            await self._finish(run_id, "blocked", "structural_mismatch", blocked)
            return IngestResult(
                run_id=run_id,
                document_id=registered.document_id,
                version_id=registered.version_id,
                state="blocked_structural",
                indexed_chunks=indexed.points if indexed else chunked.count,
                projected=projected,
                total_usd=_total(spent),
                scores=scores,
                detail=blocked,
            )

        # Activation is last, and only reached once every projection completed.
        # A version that became answerable halfway through would return chunks
        # with no citations, or citations pointing at text that was replaced.
        await self._enter(run_id, "activating")
        await workflow.execute_activity(
            act.activate_version,
            args=[request, staged, registered],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

        self._stage = "done"
        await self._finish(run_id, "succeeded")

        return IngestResult(
            run_id=run_id,
            document_id=registered.document_id,
            version_id=registered.version_id,
            state="indexed" if indexed else "structure_indexed",
            scores=scores,
            indexed_chunks=indexed.points if indexed else chunked.count,
            projected=projected
            | ({"concepts": semantics.concepts, "claims": semantics.claims,
                "semantic_edges": semantics.edges} if semantics else {}),
            total_usd=_total(spent),
            detail=_describe(correction, indexed, semantics),
        )

    # -- the gate ----------------------------------------------------------

    async def _gate(
        self, request: IngestRequest, options: StageOptions, run_id: str
    ) -> Approval:
        """Wait for a person, unless the caller asked not to be asked.

        `auto_approve` exists for folder watching, where a user has already said
        "index everything in here" once and being asked per file would make the
        feature useless. It is off by default because the alternative — spending
        unless told not to — is the wrong way round for money.
        """
        if request.auto_approve:
            return Approval(approved=True, options=options, reason="auto")

        await self._enter(run_id, "awaiting_approval", "awaiting_approval")

        try:
            await workflow.wait_condition(
                lambda: self._approval is not None, timeout=GATE_TIMEOUT
            )
        except TimeoutError:
            # Not a failure: nobody answered. Cancelling is the outcome that
            # costs nothing and leaves the document re-importable.
            return Approval(
                approved=False,
                reason=f"nadie respondió en {GATE_TIMEOUT.days} días",
            )

        assert self._approval is not None
        return self._approval

    async def _second_gate(self, run_id: str) -> Approval:
        """Show the correction diff before the remaining spend.

        Optional, because most runs do not want it — but correction is the stage
        that rewrites the user's text, and it is the one whose result a person
        may reasonably want to look at before paying to embed it.
        """
        self._approval = None
        await self._enter(
            run_id, "awaiting_correction_review", "awaiting_approval"
        )
        try:
            await workflow.wait_condition(
                lambda: self._approval is not None, timeout=GATE_TIMEOUT
            )
        except TimeoutError:
            return Approval(
                approved=False,
                reason=f"nadie revisó la corrección en {GATE_TIMEOUT.days} días",
            )
        assert self._approval is not None
        return self._approval

    @staticmethod
    def _structural_block(decision: ProfileDecision, approved: StageOptions) -> str:
        """Why activation is being withheld, or empty.

        Only a `heading_disagreement` blocks. A plain `collision` is ordinary —
        sharing a fingerprint is what makes the second document of a family
        cheaper than the first — and blocking on it would fire on every
        successful reuse, which is how a warning becomes something an operator
        clicks past.

        `ignore_profile` is already the person's way out: it declines the
        inherited rules and chunks with the measured defaults, so there is no
        disagreement left to act on and nothing to withhold.
        """
        if decision is None or decision.source != "reused" or approved.ignore_profile:
            return ""
        for warning in decision.warnings:
            if warning.kind == "heading_disagreement":
                return warning.detail
        return ""

    async def _tune_once(
        self, run_id, request, registered, staged, chunked, evalset, decision,
        scores, text_kind, spent, indexed,
    ):
        """One bounded tuning round, and never a loop.

        A loop is what the engine's CLI runs, and it is the right shape there:
        three rounds against a corpus a person is watching. Here every round is a
        full second embedding pass — ~100 minutes for a 600-chunk book against
        the measured per-minute quota — and each one would also be an entry in a
        workflow history kept for the namespace's whole retention period. So this
        is a single conditional block: try, measure, keep or put it back.

        **The revert re-chunks and re-indexes rather than only rewriting the
        profile.** That was a measured bug in the engine: reverting the profile
        left the *collection* holding the candidate's chunks, so the next round's
        baseline belonged to a configuration already rejected, and three rounds
        drifted 0.729 → 0.762 → 0.700 while each had reverted. Going back through
        chunking is what makes the comparison honest, and the writer's tail prune
        is what stops the longer chunking's leftovers surviving the trip.
        """
        await self._enter(run_id, "tuning")
        outcome: TuneOutcome = await workflow.execute_activity(
            paid.propose_tuning,
            args=[run_id, registered, chunked, evalset, decision, scores],
            start_to_close_timeout=PAID_TIMEOUT,
            retry_policy=_PAID_RETRY,
        )
        if outcome.kind != "chunking" or outcome.candidate is None:
            # `retrieval` was adopted inside the activity and costs nothing more;
            # `none` means there was nothing left worth a full re-embed. Either
            # way the index is the one `embed_and_index` already wrote, so its
            # `Indexed` is handed straight back — replacing it with `None` here
            # would report a structure-only run for a document that has vectors.
            return chunked, indexed, scores

        candidate_chunked = await workflow.execute_activity(
            paid.chunk_final,
            args=[run_id, text_kind, outcome.candidate],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )
        candidate_indexed = await workflow.execute_activity(
            paid.embed_and_index,
            args=[run_id, request.library_id, registered, staged, candidate_chunked],
            start_to_close_timeout=PAID_TIMEOUT,
            retry_policy=_PAID_RETRY,
        )
        spent.append(candidate_indexed.spend)
        candidate_scores: Scores = await workflow.execute_activity(
            paid.evaluate_index,
            # Its own artifact. Both measurements happen in this one run, and
            # writing both to `scores` left a reverted candidate describing an
            # index that had already been thrown away — the artifact said 676
            # chunks while the collection held the reverted 600.
            args=[
                run_id, registered, candidate_chunked, evalset, decision,
                "scores_candidate",
            ],
            start_to_close_timeout=PAID_TIMEOUT,
            retry_policy=_PAID_RETRY,
        )
        if candidate_scores.spend is not None:
            spent.append(candidate_scores.spend)

        # Judged against the margin computed *before* the candidate ran. One
        # derived from the candidate's own run moves with it, and the comparison
        # would be between two things that both changed.
        gain = candidate_scores.mrr_at_10 - outcome.baseline_objective
        if gain > outcome.margin:
            # Kept, so its measurement describes the index that stands and
            # becomes the run's. On a revert this does not run and `scores` is
            # still the baseline's, untouched.
            promoted: Scores = await workflow.execute_activity(
                paid.promote_candidate_scores,
                args=[run_id, candidate_scores],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=_RETRY,
            )
            return candidate_chunked, candidate_indexed, promoted

        reverted = await workflow.execute_activity(
            paid.chunk_final,
            args=[run_id, text_kind, decision],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )
        reverted_indexed = await workflow.execute_activity(
            paid.embed_and_index,
            args=[run_id, request.library_id, registered, staged, reverted],
            start_to_close_timeout=PAID_TIMEOUT,
            retry_policy=_PAID_RETRY,
        )
        spent.append(reverted_indexed.spend)
        # The original scores stand: they measured this exact index, and the
        # candidate's did not.
        return reverted, reverted_indexed, scores

    async def _reextract(
        self,
        request: IngestRequest,
        run_id: str,
        extraction: Extraction,
        decision: ProfileDecision,
    ) -> Extraction:
        """Re-read the document with the profile's header patterns applied.

        Header patterns are the only `DocRules` field any profile ever learns,
        and they are applied by the *extractor*, not the chunker — a running page
        header has to be stripped before the byte stream exists, or every
        char_span indexes text that includes it. So a profile carrying them means
        reading the file a second time.

        A profile without them means it does not: re-reading a 900-page PDF to
        apply nothing is minutes of CPU for no change, which is why this asks
        `needs_reextraction` rather than re-running unconditionally.
        """
        if extraction.structured or not decision.rules.needs_reextraction:
            return extraction
        await self._enter(run_id, "extracting")
        return await workflow.execute_activity(
            act.extract_text,
            args=[request, run_id, decision.rules],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _open(self, request: IngestRequest, run_id: str) -> None:
        """Open the run row before `stage_source` can fail against nothing.

        A file that is missing, unreadable, or outside the tenant's own tree
        fails in the **first** activity — and until this existed the `run` row
        was INSERTed by the second one, so that failure was recorded nowhere:
        `finish_run` is a bare UPDATE, zero rows affected raises nothing, and the
        import queue reads the catalog. This half has never fired in production;
        its twin in `VideoIngestWorkflow` did, on 2026-09-05, and produced a
        workflow that failed in 2.8 s with no row, no error and no trace.

        **Patched, because inserting a first activity changes the command
        sequence.** A history recorded before this shipped carries no marker
        here, so `patched` returns False on replay and that run finishes exactly
        as it began — which is what stops an import parked at its gate for up to
        seven days from failing on non-determinism the moment this deploys. It
        is also why `_pending` and `_flush_pending` stay: they are still the live
        path for every such run. Deprecate the patch once nothing older than this
        deploy is open.
        """
        if not workflow.patched(_RUN_ROW_FIRST):
            return
        await workflow.execute_activity(
            act.open_run,
            RunOpen(
                run_id=run_id,
                workflow_id=workflow.info().workflow_id,
                # Through `run_kind_of`, because `register_document` opens the
                # same row again and `ON CONFLICT DO NOTHING` means this call's
                # kind is the one that survives.
                kind=run_kind_of(request),
                tenant_id=request.tenant_id,
                library_id=request.library_id,
                label=_basename(request.source_path),
            ),
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )
        # The row exists, so every transition from here goes straight through.
        self._registered = True

    async def _enter(
        self, run_id: str, stage: str, state: str = "running"
    ) -> None:
        """Move into a stage, and leave a row saying so.

        **Every** transition goes through here now, including the eight that
        used to be a bare assignment: `staging`, `registering`, `extracting`,
        `profiling`, `previewing`, `chunking`, `activating`. Those were the ones
        that vanished — `run.stage` is a single column each write destroys, so
        the free half of the pipeline left no trace at all once the workflow
        ended, and the only per-stage timestamp that survived a run was
        `cost_entry.created_at`, which exists only for stages that spend.

        Recording them costs one local INSERT and no new dependency: everything
        from `extracting` onward already runs after `register_document`, which
        cannot itself proceed without the catalog. The two that genuinely
        precede it are buffered rather than written, because `_insert_event`
        derives its tenant from the `run` row and would silently drop them.

        `workflow.now()` is deterministic and replay-safe, and it is what makes
        a retried write land the timestamp it was first given rather than the
        one the retry happened at.
        """
        self._stage = stage
        self._seq += 1
        if not self._registered:
            self._pending.append(
                {"seq": self._seq, "at": workflow.now(), "stage": stage}
            )
            return
        await self._set_stage(run_id, stage, state, self._seq, workflow.now())

    async def _flush_pending(self, run_id: str) -> None:
        """Write the transitions that happened before there was a row to hang
        them off. Idempotent for the same reason every other event write is:
        each carries the `seq` it was given."""
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        self._registered = True
        await workflow.execute_activity(
            act.record_run_events,
            args=[run_id, pending],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _set_stage(
        self,
        run_id: str,
        stage: str,
        state: str = "running",
        seq: int | None = None,
        at: datetime | None = None,
    ) -> None:
        """Record the stage, and say the run is running unless it is waiting.

        **The default is `"running"`, and it used to be `None`.** `set_run_stage`
        writes `state = COALESCE(%s, state)`, so passing nothing left whatever was
        there — and the only call that ever set a state was the gate. A run
        therefore reported `awaiting_approval` for the whole paid pipeline, from
        approval to `finish_run`: correcting, embedding and semantics all reported
        as waiting for a person. Observed on a real run that was 96 calls into
        semantic extraction and $3.87 deep while every screen said it was waiting
        for approval, which is the opposite of the one thing that display is for.

        Inverting the default is what makes the column honest, because a run
        executing a stage *is* running, and the two states that are not are the
        two gates — which name themselves already.
        """
        await workflow.execute_activity(
            act.set_run_stage,
            # Every argument passed explicitly, including the two that have
            # defaults. Temporal maps payloads onto parameters by **arity**: a
            # five-parameter activity handed three arguments cannot be lined up,
            # so the converter gives up and passes raw dicts — which surfaces
            # three frames away as an attribute error on a `dict`.
            args=[run_id, stage, state, seq, at],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

    # -- bookkeeping -------------------------------------------------------

    async def _finish(
        self,
        run_id: str,
        state: str,
        error_kind: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Record the run's lifecycle outcome, and optionally why.

        **`blocked` is a real state, added because none of the other five was
        honest for a withheld activation.** The run did every stage and paid for
        them, so it did not fail; it deliberately withheld the last step, so it
        was not a plain success either; and nobody cancelled it. Pairing
        `succeeded` with an `error_kind` was what this did before the migration
        existed, and a succeeded row carrying an error kind is a contradiction a
        reader has to already know about to interpret.

        It is the same distinction this workflow already makes at the other end:
        a gate that times out is `cancelled` rather than `failed`, because
        "it stopped short and that is not a fault" is a different outcome from
        "it broke".

        The value is constrained in `run_state_check`, which Prisma owns in the
        sibling checkout — `20260831140000_run_blocked`.
        """
        self._seq += 1
        await workflow.execute_activity(
            act.record_run_outcome,
            args=[
                run_id, state, error_kind, error_detail,
                self._seq, workflow.now(), self._stage,
            ],
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _record_failure(self, run_id: str, error: ActivityError) -> None:
        cause = error.cause
        kind = "activity_failed"
        detail = str(cause or error)
        if isinstance(cause, ApplicationError):
            kind = cause.type or kind
        try:
            self._seq += 1
            await workflow.execute_activity(
                act.record_run_outcome,
                args=[
                    run_id, "failed", kind, detail[:2000],
                    self._seq, workflow.now(), self._stage,
                ],
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except Exception:  # the original failure is the one worth propagating
            workflow.logger.warning("could not record run failure for %s", run_id)


def _total(spent: list[Spend]) -> float | None:
    """Sum the priced part, or None when nothing priced was spent.

    None is not zero. Zero means "this run was free", which is true for a
    structure-only run and false for one whose models had no known price.
    """
    priced = [s.usd for s in spent if s.usd is not None]
    return sum(priced) if priced else (0.0 if not spent else None)


def _describe(
    correction: "Correction | None",
    indexed: "Indexed | None",
    semantics: "Semantics | None",
) -> str:
    parts: list[str] = []
    if correction is not None:
        parts.append(
            f"corrección: {correction.changed} párrafos corregidos, "
            f"{correction.rejected} rechazados por verificación, "
            f"{correction.missing} no devueltos"
        )
    if indexed is not None:
        parts.append(f"indexado: {indexed.points} puntos en «{indexed.collection}»")
    else:
        parts.append("sin embeddings: el documento no es buscable por vector")
    if semantics is not None:
        # The verified count is stated even when it equals the total, because
        # "12 afirmaciones" and "12 afirmaciones (12 comprobables)" answer
        # different questions, and the second one is the one this product
        # promises an answer to.
        parts.append(
            f"semántica: {semantics.concepts} conceptos, {semantics.claims} "
            f"afirmaciones ({semantics.claims_verified} con cita comprobada), "
            f"{semantics.edges} relaciones"
        )
    return " · ".join(parts)
