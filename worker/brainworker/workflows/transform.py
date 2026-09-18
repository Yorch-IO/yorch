"""Recasting one indexed document into another literary genre.

Two gates, a chapter at a time, and a budget spent across the whole run.

**Two gates, because one quote could not be honest.** The first is arithmetic
over the source's own character and chapter counts — everything knowable before
a model has read it — so the chapter count in it is a projection and the report
says so. The outline exists only after `planning`, and with it the real chapter
count, the real coverage and a quote computed from them. `awaiting_approval`
buys the planning calls; `awaiting_plan_review` buys the composition, which is
where essentially the whole bill is. It is `awaiting_correction_review`'s shape:
look at what the expensive-to-undo step produced before paying for the next one.

**Each gate has its own query and its own report type**, and neither is the
other reassigned. `IngestWorkflow._report` is assigned once before the first
gate and never cleared, so its second gate serves the *first* one's
pre-correction preview and estimate — seen in the real window telling a reader
"Nothing has been paid for yet" over a run that had spent $0.58, over a chunk
count measured before the correction that had changed every offset.

**Chapters are composed one at a time, never fanned out.** Each is written
against what the previous ones actually wrote, which a parallel fan-out cannot
see. The drift that produces is measured in this repository from the other
direction: *207 of 12,196 concepts carry a display name that is not the majority
spelling*, because independent passes over one corpus disagree with themselves
about how to say a word. Sequential also makes the research budget arithmetic
correct by construction — one activity, one return, one decrement.

**No LangGraph symbol appears here.** The graphs live inside the activities;
this module imports the activity functions and the payload types and nothing
else. A workflow that pulled in the node set would make every replay depend on
it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from ..activities.transform import (
        bind_document,
        compose_chapter,
        estimate_transform,
        plan_transformation,
        probe_library,
        read_source,
    )
    # Imported although this module names neither in a signature, and that is
    # the point: Temporal decodes an activity's result against the *return
    # annotation*, and resolving `Estimate`'s own `list[StageEstimate]` needs
    # both names reachable from inside the workflow sandbox. Without them the
    # run dies on `Failed decoding arguments / NameError: name 'StageEstimate'
    # is not defined` — after the activity has run and been paid for, which is
    # the expensive half of the failure. Found by `tests/workflows/test_transform.py`,
    # which is the only thing in the suite that decodes a payload the way the
    # real converter does.
    from ..pipeline import Estimate, StageEstimate  # noqa: F401
    from ..transform.budget import allowance_for
    from ..transform.estimate import projected_chapters
    from ..transform.genres import GENRES
    from ..transform.types import (
        ChapterRequest,
        TransformApproval,
        TransformGateReport,
        TransformOptions,
        TransformPlanReport,
        TransformRequest,
        TransformResult,
    )
    from .tracked import Tracked
    from .video import failure_of

#: Reading a `chunks.jsonl` and writing a Markdown file. Generous rather than
#: tuned: what this guards against is a store that has stopped answering, not a
#: slow one.
FREE_TIMEOUT = timedelta(minutes=20)

#: Eight query embeddings against a per-minute quota that has been measured at
#: about six a minute sustained, with `RATE_LIMIT_ATTEMPTS` waiting 2/8/32/60 on
#: a 429. Eight of those in the worst case is most of this.
PROBE_TIMEOUT = timedelta(minutes=20)

#: One classification call and up to three outline proposals.
PLAN_TIMEOUT = timedelta(minutes=30)

#: One chapter: its research, its draft, and at most one revision. An hour,
#: because a single answering call on this corpus has been measured at 6m42s
#: when the model spent its whole output ceiling reasoning.
CHAPTER_TIMEOUT = timedelta(hours=1)

#: Sixty times the activity's own `HEARTBEAT_INTERVAL`, which is the calibration
#: `activities/paid.py` documents for the same pair. It is armed on composition
#: deliberately: the recorded failure is an activity that *was* working and was
#: killed at minute five because the heartbeat it recorded could not be sent,
#: and the fix for that is on the activity's side, where it now is.
CHAPTER_HEARTBEAT_TIMEOUT = timedelta(minutes=5)

#: Seven days, the same as every other gate here: a user may close the lid on a
#: Friday. Not unbounded, because a workflow that never reports an outcome
#: accumulates in the namespace — and a timeout costs nothing and leaves the
#: document re-castable, so it is not a failure.
GATE_TIMEOUT = timedelta(days=7)

_RETRY = RetryPolicy(maximum_attempts=3)

#: Two attempts for anything that spends. A retry re-buys whatever the first
#: attempt paid for, which is why this is not three.
_PAID_RETRY = RetryPolicy(maximum_attempts=2)


@workflow.defn(name="TransformWorkflow")
class TransformWorkflow(Tracked):
    kind = "transform"

    def __init__(self) -> None:
        super().__init__()
        self._gate_report: TransformGateReport | None = None
        self._plan_report: TransformPlanReport | None = None
        self._answer: TransformApproval | None = None
        #: The research budget, spent across the run. It lives here and nowhere
        #: else: a counter read from a file inside an activity is one two
        #: overlapping attempts can both read, which is the exact shape of the
        #: recorded double-spend. An activity is *handed* an allowance and
        #: *returns* what it used, and only a successful return decrements this.
        self._queries_left = 0

    @workflow.signal
    def approve(self, decision: TransformApproval) -> None:
        """Last write wins, like every other gate here."""
        self._answer = decision

    @workflow.query
    def transform_gate(self) -> TransformGateReport | None:
        return self._gate_report

    @workflow.query
    def transform_plan(self) -> TransformPlanReport | None:
        return self._plan_report

    @workflow.run
    async def run(
        self, request: TransformRequest, options: TransformOptions
    ) -> TransformResult:
        run_id = workflow.info().workflow_id
        try:
            return await self._run(request, options, run_id)
        except asyncio.CancelledError:
            # A person stops a run while it is spending, not while it waits at a
            # gate. Without this branch the outcome is recorded as `failed`,
            # which is a different fact with a different remedy.
            await self._finish(run_id, "cancelled")
            raise
        except ActivityError as e:
            cause = getattr(e, "cause", None)
            if type(cause).__name__ == "CancelledError":
                await self._finish(run_id, "cancelled")
                raise
            await self._record_failure(run_id, e)
            kind, detail = failure_of(e)
            return TransformResult(
                state="failed", run_id=run_id, reason=f"{kind}: {detail}"[:2000]
            )

    async def _run(
        self, request: TransformRequest, options: TransformOptions, run_id: str
    ) -> TransformResult:
        # The run row first, so nothing it writes afterwards is dropped:
        # `_insert_event` derives its tenant from the row and silently discards
        # an event written before it exists. No `workflow.patched` guard is
        # needed — this workflow is new, so nothing older than it can replay.
        await self._open(
            run_id,
            tenant_id=request.tenant_id,
            library_id=request.library_id,
            label=request.label or request.genre,
        )

        await self._enter(run_id, "reading")
        source = await workflow.execute_activity(
            read_source,
            args=[request, run_id],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        await self._enter(run_id, "probing")
        probe = await workflow.execute_activity(
            probe_library,
            args=[request, source, run_id],
            start_to_close_timeout=PROBE_TIMEOUT,
            retry_policy=_PAID_RETRY,
        )
        spent = list(probe.spend)

        genre = GENRES[request.genre]
        projected = projected_chapters(
            len(source.chapters), source.characters, genre
        )

        await self._enter(run_id, "previewing")
        estimate = await workflow.execute_activity(
            estimate_transform,
            args=[request, source, probe, run_id],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )
        self._gate_report = TransformGateReport(
            genre=request.genre,
            mode=request.mode,
            purposes=list(request.purposes),
            source_title=source.title,
            source_chapters=len(source.chapters),
            characters=source.characters,
            projected_chapters=projected,
            research_budget=probe.budget,
            supported=probe.supported,
            projection=True,
            estimate=estimate,
        )

        if request.auto_approve:
            answer = TransformApproval(approved=True, reason="auto")
        else:
            await self._enter(run_id, "awaiting_approval", "awaiting_approval")
            answer = await self._await_answer()
        if not answer.approved:
            await self._finish(run_id, "cancelled")
            return TransformResult(
                state="cancelled", run_id=run_id, reason=answer.reason, spend=spent
            )
        options = answer.options or options

        await self._enter(run_id, "planning")
        plan = await workflow.execute_activity(
            plan_transformation,
            args=[request, source, probe, run_id],
            start_to_close_timeout=PLAN_TIMEOUT,
            retry_policy=_PAID_RETRY,
        )
        spent += list(plan.spend)
        self._queries_left = plan.budget if options.research else 0
        self._plan_report = TransformPlanReport(
            genre=request.genre,
            mode=request.mode,
            chapters=list(plan.chapters),
            uncovered_fraction=plan.uncovered_fraction,
            research_budget=self._queries_left,
            fallback=plan.fallback,
            notes=list(plan.notes),
            spent_so_far=_total(spent),
            estimate=estimate,
        )

        if options.review_plan and not request.auto_approve:
            await self._enter(run_id, "awaiting_plan_review", "awaiting_approval")
            answer = await self._await_answer()
            if not answer.approved:
                await self._finish(run_id, "cancelled")
                return TransformResult(
                    state="cancelled",
                    run_id=run_id,
                    reason=answer.reason,
                    spend=spent,
                )
            options = answer.options or options
            # Re-derived, not left as it was. Turning research off at the gate
            # that shows the budget has to change the budget, and the first
            # version of this read `options` before the gate and never again —
            # so the one switch this gate exists to offer did nothing.
            self._queries_left = plan.budget if options.research else 0
            self._plan_report.research_budget = self._queries_left

        await self._enter(run_id, "composing")
        draft = continuity = report = None
        removed = invented = queries = 0
        total = len(plan.chapters)
        for index, chapter in enumerate(plan.chapters):
            outcome = await workflow.execute_activity(
                compose_chapter,
                ChapterRequest(
                    run_id=run_id,
                    request=request,
                    plan=plan,
                    reading=source,
                    ordinal=chapter.ordinal,
                    allowance=allowance_for(self._queries_left, total - index),
                    draft=draft,
                    continuity=continuity,
                    report=report,
                ),
                start_to_close_timeout=CHAPTER_TIMEOUT,
                heartbeat_timeout=CHAPTER_HEARTBEAT_TIMEOUT,
                retry_policy=_PAID_RETRY,
            )
            draft, continuity, report = (
                outcome.draft,
                outcome.continuity,
                outcome.report,
            )
            # Only a *returned* count is subtracted. A retried attempt replays
            # with the allowance it was handed and reports once, so no arithmetic
            # here can be applied twice.
            self._queries_left = max(0, self._queries_left - outcome.queries_made)
            queries += outcome.queries_made
            removed += outcome.removed
            invented += outcome.invented
            spent += list(outcome.spend)

        await self._enter(run_id, "writing")
        document = await workflow.execute_activity(
            bind_document,
            args=[request, plan, source, draft, run_id],
            start_to_close_timeout=FREE_TIMEOUT,
            retry_policy=_RETRY,
        )

        await self._enter(run_id, "done")
        await self._finish(run_id, "succeeded")
        return TransformResult(
            state="succeeded",
            run_id=run_id,
            document=document,
            report=report,
            chapters=len(plan.chapters),
            characters=document.bytes,
            removed=removed,
            invented=invented,
            queries_made=queries,
            spend=spent,
            total_usd=_total(spent),
        )

    async def _await_answer(self) -> TransformApproval:
        """Wait for a person.

        The stage is entered by the caller, with the name written out there,
        rather than passed in here. That is not style: `tests/unit/test_stages.py`
        reads the stage names out of this file's *source*, and a name reaching
        `_enter` as a parameter is a stage the audit view can be handed and the
        trail's own guard cannot see. Two extra lines at each of two call sites
        buys a property a test can hold.

        `self._answer` is cleared **before** waiting, not after, so the second
        gate can never be satisfied by the signal that answered the first — the
        same family as the recorded defect where the second gate served the first
        one's report.
        """
        self._answer = None
        try:
            await workflow.wait_condition(
                lambda: self._answer is not None, timeout=GATE_TIMEOUT
            )
        except TimeoutError:
            # Not a failure: nobody answered. Cancelling costs nothing and
            # leaves the document re-castable.
            return TransformApproval(
                approved=False,
                reason=f"nadie respondió en {GATE_TIMEOUT.days} días",
            )
        assert self._answer is not None
        return self._answer


def _total(spend: list) -> float | None:
    """The bill so far, or `None` when no row carries a price.

    `None` and not zero. A missing price means "no known price" and rendering it
    as `$0.00` is a claim about money that was spent.
    """
    priced = [s.usd for s in spend if s.usd is not None]
    return sum(priced) if priced else None


# The projected chapter count at the gate comes from `projected_chapters`, the
# same pure function the estimate prices against, called with the same
# arguments. Deriving it a second way — reading it back out of the estimate's
# token totals, say — would be two separately computed figures for one fact,
# which is how a gate comes to disagree with its own quote.
