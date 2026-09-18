"""`TransformWorkflow`: two gates, a chapter at a time, and a budget that holds.

Activities are mocked. What is under test is the workflow's own reasoning — that
the run row is opened before anything that can fail, that the second gate serves
its own report and not the first one's, that a chapter is handed an allowance
rather than reading one, and that the whole budget cannot be overrun however many
chapters ask for it.

Every double is **typed**. Temporal maps payloads onto parameters by arity, so a
`*args` double accepts a call the real converter cannot make — which is how five
passing tests once hid a workflow that could not run at all.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.artifacts import ArtifactRef
from brainworker.pipeline import Estimate, RunOpen, Spend, StageEstimate
from brainworker.transform.types import (
    ChapterOutcome,
    ChapterRequest,
    ChapterPlan,
    ProbeReading,
    SourceChapter,
    SourceReading,
    SourceSpan,
    TransformApproval,
    TransformOptions,
    TransformPlan,
    TransformRequest,
)
from brainworker.workflows.transform import TransformWorkflow

TASK_QUEUE = "test-transform"
LIBRARY = "lib_teologia"
TENANT = "preprod"

CALLED: list[str] = []
OPENED: list[RunOpen] = []
ALLOWANCES: list[int] = []
#: How many queries each chapter reports spending, in order. The default is
#: "all of it", which is the case that can overrun the budget.
SPENDS: list[int] = []
FAIL: list[Exception] = []
#: How many chapters the stubbed plan proposes.
CHAPTERS: list[int] = [3]
#: What the stubbed probe measured.
BUDGET: list[int] = [12]

QUOTE = Estimate(
    stages=[
        StageEstimate(
            stage="transform-compose", model="gemini-3.6-flash",
            input_tokens=1000, output_tokens=2000, usd=0.02,
            output_tokens_high=3000, usd_high=0.03,
        )
    ],
    total_usd=0.02,
    total_usd_high=0.03,
    price_source="terceros",
)


def request(**kw) -> TransformRequest:
    kw.setdefault("genre", "essay")
    kw.setdefault("mode", "faithful")
    kw.setdefault("purposes", ["context"])
    return TransformRequest(
        library_id=LIBRARY,
        document_id="doc_1",
        version_id="ver_1",
        tenant_id=TENANT,
        **kw,
    )


def _ref(kind: str) -> ArtifactRef:
    return ArtifactRef(kind=kind, path=f"runs/r/{kind}", sha256="0" * 64, bytes=10)


# -- typed doubles -----------------------------------------------------------


@activity.defn(name="open_run")
async def open_run(opening: RunOpen) -> None:
    assert isinstance(opening, RunOpen), f"got {type(opening).__name__}"
    OPENED.append(opening)
    CALLED.append("open_run")


@activity.defn(name="set_run_stage")
async def set_run_stage(
    run_id: str, stage: str, state: str, seq, at, detail: str | None = None
) -> None:
    CALLED.append(f"stage:{stage}")


@activity.defn(name="record_run_outcome")
async def record_run_outcome(
    run_id: str, state: str, kind, detail, seq, at, stage: str
) -> None:
    CALLED.append(f"outcome:{state}:{stage}")


@activity.defn(name="record_run_events")
async def record_run_events(run_id: str, pending: list) -> None:
    return None


@activity.defn(name="read_source")
async def read_source(req: TransformRequest, run_id: str) -> SourceReading:
    assert isinstance(req, TransformRequest), f"got {type(req).__name__}"
    CALLED.append("read")
    return SourceReading(
        source_run_id="ingest-1",
        chunks=_ref("chunks"),
        chapters=[SourceChapter(title=f"Cap {i}", first=i * 5, last=i * 5 + 4, chars=5_000)
                  for i in range(3)],
        chunk_count=15,
        characters=15_000,
        reference_count=4,
        title="El Documento",
        language="es",
    )


@activity.defn(name="probe_library")
async def probe_library(
    req: TransformRequest, source: SourceReading, run_id: str
) -> ProbeReading:
    assert isinstance(source, SourceReading), f"got {type(source).__name__}"
    CALLED.append("probe")
    return ProbeReading(supported=96, sampled=3, budget=BUDGET[0])


@activity.defn(name="estimate_transform")
async def estimate_transform(
    req: TransformRequest, source: SourceReading, probe: ProbeReading, run_id: str
) -> Estimate:
    assert isinstance(probe, ProbeReading), f"got {type(probe).__name__}"
    CALLED.append("estimate")
    return QUOTE


@activity.defn(name="plan_transformation")
async def plan_transformation(
    req: TransformRequest, source: SourceReading, probe: ProbeReading, run_id: str
) -> TransformPlan:
    CALLED.append("plan")
    if FAIL:
        raise FAIL[0]
    return TransformPlan(
        genre=req.genre,
        mode=req.mode,
        title="La Obra Recastada",
        chapters=[
            ChapterPlan(ordinal=i + 1, title=f"Capítulo {i + 1}", intent="x",
                        sources=[SourceSpan(first=i * 5, last=i * 5 + 4)], chars=5_000)
            for i in range(CHAPTERS[0])
        ],
        budget=probe.budget,
        spend=[Spend(stage="transform-plan", model="m", input_tokens=1,
                     output_tokens=1, usd=0.01)],
    )


@activity.defn(name="compose_chapter")
async def compose_chapter(job: ChapterRequest) -> ChapterOutcome:
    # Typed, and the annotation is the test. Written without it first, and the
    # converter handed the double a raw `dict` — `'dict' object has no attribute
    # 'ordinal'`, which is the recorded `'dict' object has no attribute
    # 'source_path'` arriving from the same direction. A `*args` double would
    # have swallowed it silently.
    assert isinstance(job, ChapterRequest), f"got {type(job).__name__}"
    CALLED.append(f"compose:{job.ordinal}")
    ALLOWANCES.append(job.allowance)
    spent = SPENDS.pop(0) if SPENDS else job.allowance
    return ChapterOutcome(
        draft=_ref("transform_draft"),
        continuity=_ref("transform_continuity"),
        report=_ref("transform_report"),
        queries_made=spent,
        removed=1,
        invented=2,
        cited=3,
        spend=[Spend(stage="transform-compose", model="m", input_tokens=1,
                     output_tokens=1, usd=0.05)],
    )


@activity.defn(name="bind_document")
async def bind_document(
    req: TransformRequest, plan: TransformPlan, source: SourceReading,
    draft, run_id: str
) -> ArtifactRef:
    assert isinstance(plan, TransformPlan), f"got {type(plan).__name__}"
    assert draft is not None, "the work was bound from nothing"
    CALLED.append("bind")
    return _ref("transform")


ACTIVITIES = [
    open_run, set_run_stage, record_run_outcome, record_run_events,
    read_source, probe_library, estimate_transform, plan_transformation,
    compose_chapter, bind_document,
]


@pytest.fixture(autouse=True)
def _reset():
    CALLED.clear()
    OPENED.clear()
    ALLOWANCES.clear()
    SPENDS.clear()
    FAIL.clear()
    CHAPTERS[:] = [3]
    BUDGET[:] = [12]
    yield


async def _start(client: Client, req=None, options=None):
    return await client.start_workflow(
        TransformWorkflow.run,
        args=[req or request(), options or TransformOptions()],
        id=f"transform-{uuid.uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )


async def _run(req=None, options=None, answer_gates=2):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue=TASK_QUEUE,
            workflows=[TransformWorkflow], activities=ACTIVITIES,
        ):
            handle = await _start(env.client, req, options)
            for _ in range(answer_gates):
                await _wait_for_gate(handle)
                await handle.signal(TransformWorkflow.approve,
                                    TransformApproval(approved=True))
            return await handle.result(), handle


async def _wait_for_gate(handle) -> None:
    """Wait until the workflow is parked, without racing the signal."""
    import asyncio

    for _ in range(200):
        if await handle.query(TransformWorkflow.stage) in {
            "awaiting_approval", "awaiting_plan_review"
        }:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the workflow never reached a gate")


@pytest.mark.asyncio
async def test_the_run_row_is_opened_before_anything_that_can_fail():
    """`_insert_event` derives its tenant from the run row and silently drops an
    event written before it exists. A video refused by YouTube once failed 2.8 s
    in, reported Completed having written nothing, and showed no row at all."""
    result, _ = await _run()
    assert CALLED[0] == "open_run"
    assert OPENED[0].kind == "transform"
    assert OPENED[0].tenant_id == TENANT
    assert OPENED[0].library_id == LIBRARY
    assert result.state == "succeeded"


@pytest.mark.asyncio
async def test_the_stages_are_entered_in_order():
    _, _ = await _run()
    stages = [c.split(":", 1)[1] for c in CALLED if c.startswith("stage:")]
    assert stages == [
        "reading", "probing", "previewing", "awaiting_approval",
        "planning", "awaiting_plan_review", "composing", "writing", "done",
    ]


@pytest.mark.asyncio
async def test_nothing_is_composed_before_the_first_gate_is_answered():
    """The whole point of a gate: the expensive half must not have started."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue=TASK_QUEUE,
            workflows=[TransformWorkflow], activities=ACTIVITIES,
        ):
            handle = await _start(env.client)
            await _wait_for_gate(handle)
            assert "plan" not in CALLED
            assert not [c for c in CALLED if c.startswith("compose")]
            await handle.signal(TransformWorkflow.approve,
                                TransformApproval(approved=False, reason="no"))
            result = await handle.result()
    assert result.state == "cancelled"
    assert result.reason == "no"


@pytest.mark.asyncio
async def test_the_second_gate_serves_its_own_report_and_not_the_first_s():
    """The recorded defect, pinned. `IngestWorkflow._report` is assigned once
    before the first gate and never cleared, so its second gate serves the first
    one's pre-correction preview — seen in the real window telling a reader
    "Nothing has been paid for yet" over a run that had spent $0.58."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue=TASK_QUEUE,
            workflows=[TransformWorkflow], activities=ACTIVITIES,
        ):
            handle = await _start(env.client)
            await _wait_for_gate(handle)
            assert await handle.query(TransformWorkflow.transform_gate) is not None
            assert await handle.query(TransformWorkflow.transform_plan) is None, (
                "the plan report exists before the outline does"
            )
            await handle.signal(TransformWorkflow.approve,
                                TransformApproval(approved=True))
            await _wait_for_gate(handle)
            plan_report = await handle.query(TransformWorkflow.transform_plan)
            gate_report = await handle.query(TransformWorkflow.transform_gate)
            assert plan_report is not None
            assert len(plan_report.chapters) == 3
            assert plan_report.spent_so_far is not None
            assert gate_report.projection is True
            await handle.signal(TransformWorkflow.approve,
                                TransformApproval(approved=True))
            await handle.result()


@pytest.mark.asyncio
async def test_the_second_gate_cannot_be_satisfied_by_the_first_gate_s_signal():
    """`_answer` is cleared before waiting, not after."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue=TASK_QUEUE,
            workflows=[TransformWorkflow], activities=ACTIVITIES,
        ):
            handle = await _start(env.client)
            await _wait_for_gate(handle)
            await handle.signal(TransformWorkflow.approve,
                                TransformApproval(approved=True))
            await _wait_for_gate(handle)
            assert await handle.query(TransformWorkflow.stage) == "awaiting_plan_review"
            assert not [c for c in CALLED if c.startswith("compose")]
            await handle.signal(TransformWorkflow.approve,
                                TransformApproval(approved=True))
            await handle.result()


@pytest.mark.asyncio
async def test_review_plan_off_composes_straight_after_planning():
    result, _ = await _run(options=TransformOptions(review_plan=False), answer_gates=1)
    assert result.state == "succeeded"
    assert "stage:awaiting_plan_review" not in CALLED


@pytest.mark.asyncio
async def test_auto_approve_answers_both_gates():
    result, _ = await _run(req=request(auto_approve=True), answer_gates=0)
    assert result.state == "succeeded"
    assert not [c for c in CALLED if c.startswith("stage:awaiting")]


@pytest.mark.asyncio
async def test_every_planned_chapter_is_composed_in_order():
    CHAPTERS[:] = [5]
    result, _ = await _run()
    composed = [c for c in CALLED if c.startswith("compose:")]
    assert composed == [f"compose:{i}" for i in range(1, 6)]
    assert result.chapters == 5


@pytest.mark.asyncio
async def test_the_research_budget_cannot_be_overrun():
    """The property the whole allowance design exists for."""
    CHAPTERS[:] = [5]
    BUDGET[:] = [12]
    result, _ = await _run()
    assert sum(ALLOWANCES) >= 12, "the budget should be spendable"
    assert result.queries_made == 12, "and not exceeded"
    assert all(a >= 0 for a in ALLOWANCES)


@pytest.mark.asyncio
async def test_an_underspending_chapter_leaves_more_for_the_rest():
    CHAPTERS[:] = [4]
    BUDGET[:] = [12]
    SPENDS[:] = [0, 0, 0, 0]
    await _run()
    assert ALLOWANCES[-1] > ALLOWANCES[0], ALLOWANCES


@pytest.mark.asyncio
async def test_research_off_hands_every_chapter_nothing():
    result, _ = await _run(options=TransformOptions(research=False))
    assert ALLOWANCES == [0, 0, 0]
    assert result.queries_made == 0


@pytest.mark.asyncio
async def test_a_library_with_nothing_to_say_still_produces_the_work():
    BUDGET[:] = [0]
    result, _ = await _run()
    assert ALLOWANCES == [0, 0, 0]
    assert result.state == "succeeded"
    assert result.document is not None


@pytest.mark.asyncio
async def test_the_approval_s_options_win_and_none_keeps_the_run_s():
    """The recorded live defect in the other direction: the Angular gate sent
    its options alone, so every stage a person unticked was silently turned back
    on. A `None` here keeps what the run was started with, so a client that
    forgets to merge loses nothing."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue=TASK_QUEUE,
            workflows=[TransformWorkflow], activities=ACTIVITIES,
        ):
            handle = await _start(env.client, options=TransformOptions(review_plan=True))
            await _wait_for_gate(handle)
            # Answer with no options at all: the run's own must survive, so the
            # second gate still happens.
            await handle.signal(TransformWorkflow.approve,
                                TransformApproval(approved=True))
            await _wait_for_gate(handle)
            assert await handle.query(TransformWorkflow.stage) == "awaiting_plan_review"
            # Now turn research off *at* the gate.
            await handle.signal(
                TransformWorkflow.approve,
                TransformApproval(approved=True,
                                  options=TransformOptions(review_plan=True,
                                                           research=False)),
            )
            result = await handle.result()
    assert result.queries_made == 0


@pytest.mark.asyncio
async def test_a_refused_second_gate_cancels_without_composing():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue=TASK_QUEUE,
            workflows=[TransformWorkflow], activities=ACTIVITIES,
        ):
            handle = await _start(env.client)
            await _wait_for_gate(handle)
            await handle.signal(TransformWorkflow.approve,
                                TransformApproval(approved=True))
            await _wait_for_gate(handle)
            await handle.signal(
                TransformWorkflow.approve,
                TransformApproval(approved=False, reason="demasiado caro"),
            )
            result = await handle.result()
    assert result.state == "cancelled"
    assert result.reason == "demasiado caro"
    assert not [c for c in CALLED if c.startswith("compose")]
    assert "outcome:cancelled:awaiting_plan_review" in CALLED


@pytest.mark.asyncio
async def test_a_failure_is_recorded_against_the_stage_it_happened_in():
    # Built through the activity module's own `_refuse`, so what is under test
    # is the shape a real refusal has. A hand-built `ApplicationError` here
    # would have agreed with the assumption rather than with the code — and the
    # assumption was wrong: `timed.failure_of` reads `cause.type`, and a kind
    # carried only in `details` records every refusal as `activity_failed`.
    from brainworker.activities.transform import TransformError, _refuse

    FAIL.append(_refuse(TransformError("no provider", "provider_unconfigured")))
    result, _ = await _run(answer_gates=1)
    assert result.state == "failed"
    assert "provider_unconfigured" in result.reason
    assert "outcome:failed:planning" in CALLED


@pytest.mark.asyncio
async def test_the_result_totals_what_every_stage_spent():
    CHAPTERS[:] = [3]
    result, _ = await _run()
    # One plan call at $0.01 and three chapters at $0.05.
    assert result.total_usd == pytest.approx(0.16)
    assert result.removed == 3
    assert result.invented == 6
