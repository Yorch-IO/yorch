"""The ingest workflow and its approval gate.

Activities are mocked. What is under test is the workflow's own reasoning —
that nothing paid can happen before someone approves it, that a duplicate stops
early, that a rejection is an outcome rather than a failure — and that all of it
survives replay. Whether an extractor works is what the activity tests measure.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.artifacts import ArtifactRef
from brainworker.pipeline import (
    ChunkKindCount,
    Chunked,
    Correction,
    Estimate,
    EvalSet,
    Extraction,
    Indexed,
    IngestRequest,
    Preview,
    ProfileDecision,
    ProfileRules,
    ProfileWarning,
    Registered,
    Scores,
    ProfileRules,
    Semantics,
    Spend,
    StageEstimate,
    StageOptions,
    Staged,
    TuneOutcome,
)
from brainworker.workflows.ingest import Approval, IngestWorkflow

TASK_QUEUE = "test-ingest"

#: Every paid stage records itself here when a mock stands in for it. The
#: assertion that matters most in this file is that it stays empty on a run
#: nobody approved.
SPENT: list[str] = []
PERSISTED: list[str] = []
PROMOTED: list[str] = []

REF = ArtifactRef(kind="raw_text", path="runs/r/raw.txt", sha256="a" * 64, bytes=10)
CHUNK_REF = ArtifactRef(
    kind="preview_chunks", path="runs/r/chunks.preview.jsonl",
    sha256="b" * 64, bytes=20, rows=3,
)
EVIDENCE_REF = ArtifactRef(
    kind="evidence", path="runs/r/evidence.json", sha256="c" * 64, bytes=5
)


def request(**kw) -> IngestRequest:
    return IngestRequest(
        library_id="lib_1",
        source_path="/workspace/inbox/calvino.pdf",
        source_key="libros/calvino.pdf",
        title="Institución",
        **kw,
    )


# -- activity doubles -------------------------------------------------------


@activity.defn(name="stage_source")
async def stage_source(_: IngestRequest) -> Staged:
    return Staged(
        content_sha256="d" * 64, byte_size=1000, fmt="pdf",
        extractor="pdf_text", title="Institución",
    )


def register(created: bool = True, already_indexed: bool = False):
    @activity.defn(name="register_document")
    async def _register(*_args) -> Registered:
        return Registered(
            document_id="doc_1", version_id="ver_1",
            created=created, already_indexed=already_indexed,
        )

    return _register


@activity.defn(name="extract_text")
async def extract_text(
    request: IngestRequest, run_id: str, rules: ProfileRules | None
) -> Extraction:
    """Typed on purpose, unlike the other doubles.

    Temporal maps payloads onto parameters by arity, so an activity invoked with
    fewer arguments than it declares receives raw dicts instead of dataclasses. A
    `*_args` double swallows that silently — it accepts anything — and the real
    activity then fails on `'dict' object has no attribute 'source_path'`. This
    signature is what makes the workflow's call site wrong *here* instead of on
    a live run.
    """
    assert isinstance(request, IngestRequest), f"got {type(request).__name__}"
    return Extraction(
        text=REF, evidence=EVIDENCE_REF, extractor="pdf_text",
        source_key=request.source_key,
    )


def preview(chunks_are_final: bool = False, count: int = 3):
    @activity.defn(name="preview_chunks")
    async def _preview(*_args) -> Preview:
        return Preview(
            text=REF, chunks=CHUNK_REF, chunk_count=count,
            kinds=[ChunkKindCount("cuerpo", count)], characters=12_000,
            chunks_are_final=chunks_are_final,
            warnings=[] if chunks_are_final else ["la corrección cambia la longitud"],
        )

    return _preview


@activity.defn(name="estimate_cost")
async def estimate_cost(*_args) -> Estimate:
    return Estimate(
        stages=[
            StageEstimate("correction", "gemini-2.5-flash", 3333, 3333, 0.0047),
            StageEstimate("embedding", "gemini-embedding-001", 3333, 0, 0.0005),
        ],
        total_usd=0.0052,
        price_source="third-party list prices over measured counts",
    )


def resolver(
    warnings: list[ProfileWarning] | None = None,
    source: str = "reused",
    rules: ProfileRules | None = None,
):
    """Stand in for profile resolution.

    Defaults to `reused`, because that is the case where nothing paid follows:
    a test about the gate should not have to opt out of a learning stage it is
    not asking about. Tests that want learning pass `source="default"`.
    """

    @activity.defn(name="resolve_profile")
    async def _resolve(*_args) -> ProfileDecision:
        return ProfileDecision(
            fingerprint="fp_1",
            source=source,
            slug="calvino-fp1" if source == "reused" else "",
            rules=rules or ProfileRules(),
            warnings=warnings or [],
        )

    return _resolve


@activity.defn(name="learn_profile")
async def learn_profile(_run_id, _extraction, decision: ProfileDecision) -> ProfileDecision:
    SPENT.append("profile")
    decision.source = "learned"
    decision.slug = "calvino-fp1"
    decision.adopted = ["header_patterns", "heading_guards"]
    decision.spend = Spend("profile", "gemini-3.6-flash", 2000, 500, 0.0075)
    return decision


@activity.defn(name="project_structure")
async def project_structure(*_args) -> dict[str, int]:
    return {"sections": 2, "chunks": 3, "citations": 3}


LINKED: list[str] = []


@activity.defn(name="link_duplicate")
async def link_duplicate(*_args) -> None:
    LINKED.append("linked")


@activity.defn(name="activate_version")
async def activate_version(*_args) -> None:
    return None


#: Terminal states the workflow told the catalog about. A no-op mock could not
#: tell "recorded cancelled" from "recorded nothing", which is the whole point of
#: the cancellation test below.
OUTCOMES: list[str] = []


@activity.defn(name="record_run_outcome")
async def record_run_outcome(*args) -> None:
    if len(args) >= 2:
        OUTCOMES.append(args[1])
    return None


CORRECTED_REF = ArtifactRef(
    kind="corrected_text", path="runs/r/corrected.txt", sha256="d" * 64, bytes=30
)
REPORT_REF = ArtifactRef(
    kind="correction_report", path="runs/r/correction-report.json",
    sha256="e" * 64, bytes=40,
)
FINAL_REF = ArtifactRef(
    kind="chunks", path="runs/r/chunks.jsonl", sha256="f" * 64, bytes=50, rows=3
)


@activity.defn(name="correct_text")
async def correct_text(*_args) -> Correction:
    SPENT.append("correction")
    return Correction(
        text=CORRECTED_REF, report=REPORT_REF, paragraphs=10, changed=7,
        rejected=1, missing=0, cache_hits=2,
        spend=Spend("correction", "gemini-2.5-flash", 3000, 3000, 0.0042),
    )


@activity.defn(name="chunk_final")
async def chunk_final(*_args) -> Chunked:
    return Chunked(chunks=FINAL_REF, count=3, kinds=[ChunkKindCount("cuerpo", 3)])


@activity.defn(name="embed_and_index")
async def embed_and_index(*_args) -> Indexed:
    SPENT.append("embedding")
    return Indexed(
        collection="brain", points=3, dimensions=3072,
        spend=Spend("embedding", "gemini-embedding-001", 3000, 0, 0.00045),
    )


EVALSET_REF = ArtifactRef(
    kind="evalset", path="runs/r/evalset.json", sha256="e" * 64, bytes=40
)
SCORES_REF = ArtifactRef(
    kind="scores", path="runs/r/scores.json", sha256="f" * 64, bytes=30
)


@activity.defn(name="build_evalset")
async def build_evalset(*_args) -> EvalSet:
    SPENT.append("evalset")
    return EvalSet(
        items=EVALSET_REF, questions=40, sample=40,
        spend=Spend("evalset", "gemini-3.6-flash", 12000, 1200, 0.0108),
    )


EVALUATED_INTO: list[str] = []


@activity.defn(name="evaluate_index")
async def evaluate_index(*args) -> Scores:
    SPENT.append("evaluation")
    EVALUATED_INTO.append(args[5] if len(args) > 5 else "scores")
    return Scores(
        recall_at_1=0.675, recall_at_5=0.9, mrr_at_10=0.7642,
        recall_at_5_dense_only=1.0, noise_floor=0.6232,
        chunks=3, eval_questions=40, margin=0.05, leakage="hybrid == dense",
        report=SCORES_REF,
        spend=Spend("evaluation", "gemini-embedding-2", 400, 0, 0.00008),
    )


@activity.defn(name="propose_tuning")
async def propose_tuning(*_args) -> TuneOutcome:
    SPENT.append("tuning")
    return TuneOutcome(
        kind="chunking", label="overlap=300",
        baseline_objective=0.700, margin=0.077,
        candidate=ProfileDecision(
            fingerprint="fp", source="tuned",
            rules=ProfileRules(target_chars=1200, overlap_chars=300),
        ),
        notes=["the free knobs are exhausted"],
    )


@activity.defn(name="promote_candidate_scores")
async def promote_candidate_scores(run_id: str, scores: Scores) -> Scores:
    PROMOTED.append(run_id)
    return scores


@activity.defn(name="persist_profile_scores")
async def persist_profile_scores(*_args) -> bool:
    PERSISTED.append("scores")
    return True


@activity.defn(name="extract_semantics")
async def extract_semantics(*_args) -> Semantics:
    SPENT.append("semantics")
    return Semantics(
        concepts=4, claims=2, edges=6,
        spend=Spend("semantics", "gemini-2.5-flash", 3000, 450, 0.0010),
    )


@activity.defn(name="set_run_stage")
async def set_run_stage(*_args) -> None:
    return None


def activities(**kw):
    return [
        kw.get("stage_source", stage_source),
        kw.get("register", register()),
        extract_text,
        kw.get("preview", preview()),
        estimate_cost,
        kw.get("resolver", resolver()),
        kw.get("learn_profile", learn_profile),
        kw.get("project_structure", project_structure),
        link_duplicate,
        activate_version,
        record_run_outcome,
        set_run_stage,
        kw.get("correct_text", correct_text),
        chunk_final,
        embed_and_index,
        kw.get("build_evalset", build_evalset),
        kw.get("evaluate_index", evaluate_index),
        kw.get("propose_tuning", propose_tuning),
        promote_candidate_scores,
        persist_profile_scores,
        extract_semantics,
    ]


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture(autouse=True)
def _clear():
    SPENT.clear()
    PERSISTED.clear()
    PROMOTED.clear()
    EVALUATED_INTO.clear()
    LINKED.clear()
    OUTCOMES.clear()
    yield
    SPENT.clear()
    OUTCOMES.clear()


async def _start(env: WorkflowEnvironment, req: IngestRequest, opts: StageOptions, acts):
    client: Client = env.client
    handle = await client.start_workflow(
        IngestWorkflow.run,
        args=[req, opts],
        id=f"ingest-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    return handle


# -- the gate ---------------------------------------------------------------


async def test_nothing_paid_happens_before_someone_approves(env: WorkflowEnvironment):
    """The single most important property in this file.

    Correction is the dominant cost ($0.0334 of a $0.0498 run), so a pipeline
    that corrected first and asked afterwards would have spent the money it was
    asking about.
    """
    async with Worker(
        env.client, task_queue=TASK_QUEUE,
        workflows=[IngestWorkflow], activities=activities(),
    ):
        handle = await _start(env, request(), StageOptions(), activities())
        await _wait_for_gate(handle)

        assert SPENT == [], "a paid stage ran before the gate was answered"
        report = await handle.query(IngestWorkflow.gate_report)
        assert report is not None
        assert report.estimate.total_usd == pytest.approx(0.0052)

        await handle.signal(IngestWorkflow.approve, Approval(approved=False))
        result = await handle.result()
    assert result.state == "rejected"
    assert SPENT == []


async def test_the_gate_report_says_the_previewed_chunks_are_not_the_final_ones(
    env: WorkflowEnvironment,
):
    """Correction changes the text's length, which invalidates every char_span,
    so it must run before chunking. The UI cannot imply otherwise."""
    acts = activities(preview=preview(chunks_are_final=False))
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(correct=True), acts)
        report = await _wait_for_gate(handle)
        assert report.preview.chunks_are_final is False
        assert report.preview.warnings
        await handle.signal(IngestWorkflow.approve, Approval(approved=False))
        await handle.result()


async def test_disabling_correction_makes_the_preview_final(env: WorkflowEnvironment):
    acts = activities(preview=preview(chunks_are_final=True))
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(correct=False), acts)
        report = await _wait_for_gate(handle)
        assert report.preview.chunks_are_final is True
        await handle.signal(IngestWorkflow.approve, Approval(approved=False))
        await handle.result()


async def test_rejection_is_an_outcome_not_a_failure(env: WorkflowEnvironment):
    """A failed workflow would show the user a red error for a decision they made."""
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=False, reason="demasiado caro")
        )
        result = await handle.result()
    assert result.state == "rejected"
    assert result.detail == "demasiado caro"


async def test_a_gate_nobody_answers_cancels_instead_of_waiting_forever(
    env: WorkflowEnvironment,
):
    """Time-skipped: the workflow really waits seven days, the test does not."""
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        result = await handle.result()
    assert result.state == "rejected"
    assert "días" in result.detail
    assert SPENT == []


async def test_auto_approve_skips_the_gate_for_watched_folders(env: WorkflowEnvironment):
    """A user who said "index everything in here" once must not be asked per file."""
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(auto_approve=True), StageOptions(), acts)
        result = await handle.result()
    assert result.state == "indexed"
    assert SPENT == ["correction", "embedding", "semantics"]


# -- duplicates -------------------------------------------------------------


async def test_identical_content_stops_before_extraction(env: WorkflowEnvironment):
    """The duplicate fix, at the workflow level.

    Re-indexing byte-identical content would spend money producing vectors that
    already exist, and then let the two copies compete in ranking.
    """
    extracted: list[str] = []

    @activity.defn(name="extract_text")
    async def counting_extract(*_args) -> Extraction:
        extracted.append("ran")
        return Extraction(text=REF, evidence=EVIDENCE_REF, extractor="pdf_text")

    acts = [
        stage_source, register(created=False, already_indexed=True), counting_extract,
        preview(), estimate_cost, resolver(), learn_profile, project_structure,
        link_duplicate, activate_version, record_run_outcome, set_run_stage,
        correct_text, chunk_final, embed_and_index, extract_semantics,
    ]
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        result = await handle.result()

    assert result.state == "already_indexed"
    assert extracted == [], "a duplicate must not be extracted again"
    assert result.version_id == "ver_1"
    # The catalog linked the new path; the graph must learn about it too, or the
    # Library screen shows one copy while the catalog holds two.
    assert LINKED == ["linked"]


async def test_reindex_deliberately_walks_past_the_duplicate_guard(
    env: WorkflowEnvironment,
):
    """The same short-circuit, and the one case where it must not fire.

    `already_indexed` is right for an import: identical bytes arriving at a
    second path must link rather than embed twice. It is wrong for a user who
    pressed "reindexar" — after dropping a collection, or to pick up a corrected
    profile — because the whole request is "do it again".
    """
    extracted: list[str] = []

    @activity.defn(name="extract_text")
    async def counting_extract(*_args) -> Extraction:
        extracted.append("ran")
        return Extraction(text=REF, evidence=EVIDENCE_REF, extractor="pdf_text")

    acts = [
        stage_source, register(created=False, already_indexed=True), counting_extract,
        preview(), estimate_cost, resolver(), learn_profile, project_structure,
        link_duplicate, activate_version, record_run_outcome, set_run_stage,
        correct_text, chunk_final, embed_and_index, extract_semantics,
    ]
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(
            env, request(reindex=True), StageOptions(), acts
        )
        # It reached the gate, which means it did not stop at the duplicate.
        await _wait_for_gate(handle)
        await handle.signal(IngestWorkflow.approve, Approval(approved=False))
        result = await handle.result()

    assert extracted == ["ran"], "a deliberate reindex must extract again"
    assert result.state != "already_indexed"


# -- profile collisions -----------------------------------------------------


async def test_a_profile_collision_reaches_the_gate(env: WorkflowEnvironment):
    """The defect the metrics provably cannot detect has to be shown to a person."""
    warning = ProfileWarning(
        profile_id="actas-1a2b3c4d",
        collides_with="libros/actas-consistorio.pdf",
        similarity=0.04,
        detail="misma huella estructural, materia no relacionada",
    )
    acts = activities(resolver=resolver([warning]))
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        report = await _wait_for_gate(handle)
        assert len(report.profile_warnings) == 1
        assert report.profile_warnings[0].similarity == pytest.approx(0.04)
        await handle.signal(IngestWorkflow.approve, Approval(approved=False))
        await handle.result()


# -- failure ----------------------------------------------------------------


async def test_a_failing_activity_records_the_run_before_failing(
    env: WorkflowEnvironment,
):
    recorded: list[tuple] = []

    @activity.defn(name="stage_source")
    async def missing_file(_: IngestRequest) -> Staged:
        raise FileNotFoundError("no such file: /workspace/inbox/calvino.pdf")

    @activity.defn(name="record_run_outcome")
    async def capture(run_id: str, state: str, kind=None, detail=None) -> None:
        recorded.append((state, kind, detail))

    acts = [
        missing_file, register(), extract_text, preview(), estimate_cost,
        resolver(), learn_profile, project_structure, link_duplicate,
        activate_version, capture, set_run_stage, correct_text, chunk_final,
        embed_and_index, extract_semantics,
    ]
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    assert recorded and recorded[0][0] == "failed"
    assert "no such file" in (recorded[0][2] or "")


async def _wait_for_gate(handle):
    """Poll the query until the free stages have produced a report."""
    for _ in range(200):
        report = await handle.query(IngestWorkflow.gate_report)
        if report is not None:
            return report
    raise AssertionError("the gate report never appeared")


# -- paid stages ------------------------------------------------------------


async def test_each_paid_stage_is_switchable_on_its_own(env: WorkflowEnvironment):
    """"Approve" is a set of switches, not one yes, because the stages cost
    wildly different amounts: correction $0.0334 against embedding $0.0033."""
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve,
            Approval(
                approved=True,
                options=StageOptions(
                    correct=False, embed=True, extract_semantics=False
                ),
            ),
        )
        result = await handle.result()

    assert SPENT == ["embedding"], "only the approved stage may spend"
    assert result.state == "indexed"
    assert result.total_usd == pytest.approx(0.00045)


async def test_the_switches_chosen_at_the_gate_override_the_ones_requested(
    env: WorkflowEnvironment,
):
    """The gate is where the decision is made. A run started with correction on
    and approved with it off must not correct."""
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(correct=True), acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve,
            Approval(approved=True, options=StageOptions(correct=False, embed=False,
                                                         extract_semantics=False)),
        )
        result = await handle.result()
    assert SPENT == []
    assert result.total_usd == 0.0
    assert "sin embeddings" in result.detail


async def test_a_structured_source_is_never_corrected(env: WorkflowEnvironment):
    """An LLM must not rewrite a cell value. Spreadsheets and decks skip it even
    when the user approved correction."""

    @activity.defn(name="extract_text")
    async def structured(*_args) -> Extraction:
        return Extraction(
            text=REF, evidence=EVIDENCE_REF, extractor="excel", structured=True
        )

    acts = [a for a in activities() if a is not extract_text] + [structured]
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(auto_approve=True),
                              StageOptions(correct=True, embed=False,
                                           extract_semantics=False), acts)
        result = await handle.result()
    assert "correction" not in SPENT
    assert result.state == "structure_indexed"


async def test_the_optional_second_gate_stops_before_the_remaining_spend(
    env: WorkflowEnvironment,
):
    """Correction is the stage that rewrites the user's text, so a person may
    reasonably want to look at the result before paying to embed it."""
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve,
            Approval(approved=True, options=StageOptions(review_correction=True)),
        )

        for _ in range(400):
            if await handle.query(IngestWorkflow.stage) == "awaiting_correction_review":
                break
        else:
            raise AssertionError("the second gate never opened")

        assert SPENT == ["correction"], "only correction may have run by now"
        correction = await handle.query(IngestWorkflow.correction)
        assert correction is not None and correction.changed == 7
        assert correction.rejected == 1

        await handle.signal(
            IngestWorkflow.approve,
            Approval(approved=False, reason="la corrección estropeó las citas"),
        )
        result = await handle.result()

    assert result.state == "rejected_after_correction"
    assert SPENT == ["correction"], "rejecting must stop the remaining spend"
    # The money already spent is still reported. It was spent.
    assert result.total_usd == pytest.approx(0.0042)


async def test_accepting_at_the_second_gate_continues_with_its_switches(
    env: WorkflowEnvironment,
):
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve,
            Approval(approved=True, options=StageOptions(review_correction=True)),
        )
        for _ in range(400):
            if await handle.query(IngestWorkflow.stage) == "awaiting_correction_review":
                break
        await handle.signal(
            IngestWorkflow.approve,
            Approval(approved=True,
                     options=StageOptions(embed=True, extract_semantics=False)),
        )
        result = await handle.result()

    assert SPENT == ["correction", "embedding"]
    assert result.state == "indexed"


async def test_a_run_that_spent_nothing_reports_zero_not_unknown(
    env: WorkflowEnvironment,
):
    """None means "we do not know what this cost". Zero means it was free.
    Conflating them would show "not priced" for a run that genuinely was."""
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(
            env, request(auto_approve=True),
            StageOptions(correct=False, embed=False, extract_semantics=False), acts,
        )
        result = await handle.result()
    assert result.total_usd == 0.0
    assert SPENT == []


async def test_activation_happens_after_every_projection(env: WorkflowEnvironment):
    """A version that became answerable halfway through would return chunks with
    no citations, or citations pointing at text that was replaced."""
    order: list[str] = []

    @activity.defn(name="embed_and_index")
    async def note_embed(*_args) -> Indexed:
        order.append("embed")
        SPENT.append("embedding")
        return Indexed("brain", 3, 3072,
                       Spend("embedding", "gemini-embedding-001", 3000, 0, 0.00045))

    @activity.defn(name="activate_version")
    async def note_activate(*_args) -> None:
        order.append("activate")

    acts = [a for a in activities()
            if a not in (embed_and_index, activate_version)] + [note_embed, note_activate]
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(auto_approve=True),
                              StageOptions(correct=False, extract_semantics=False), acts)
        await handle.result()
    assert order == ["embed", "activate"]


async def test_a_cancelled_run_records_the_outcome_rather_than_reading_running(
    env: WorkflowEnvironment,
):
    """Cancelling has to reach the catalog, and the write has to be shielded.

    Without the handler the run row keeps whatever state it had — `running`, now
    that `_set_stage` is honest — and stays there for ever, which is the exact
    lie the state column was fixed to stop telling. Without the *shield* the
    handler runs and still records nothing: the cancellation that triggered it
    cancels the bookkeeping activity too. This test fails in both cases, which is
    why it asserts the recorded value rather than merely that the run ended.
    """
    async with Worker(
        env.client, task_queue=TASK_QUEUE,
        workflows=[IngestWorkflow], activities=activities(),
    ):
        handle = await _start(env, request(), StageOptions(), activities())
        await _wait_for_gate(handle)

        await handle.cancel()
        with pytest.raises(Exception):
            await handle.result()

    assert OUTCOMES == ["cancelled"], (
        "a cancelled run must tell the catalog it was cancelled"
    )
    assert SPENT == [], "cancelling at the gate must not have spent anything"


async def test_cancelling_while_a_paid_activity_runs_still_records_the_outcome(
    env: WorkflowEnvironment,
):
    """The case the shield is actually for.

    Cancelling at the gate is the easy half: nothing is in flight, so the
    bookkeeping activity starts cleanly. This cancels with a paid activity
    already running, which is the situation a person is in when they stop a run
    — 598 chunks into semantic extraction, watching the money — and it is where
    an unshielded cleanup would be cancelled along with everything else.
    """
    import asyncio as _asyncio

    running = _asyncio.Event()

    @activity.defn(name="embed_and_index")
    async def slow_embed(*_args) -> Indexed:
        SPENT.append("embedding")
        running.set()
        await _asyncio.sleep(10)
        return Indexed(
            collection="brain", points=3, dimensions=3072,
            spend=Spend("embedding", "gemini-embedding-001", 3000, 0, 0.00045),
        )

    acts = [a for a in activities() if getattr(a, "__temporal_activity_definition", None)
            and a.__temporal_activity_definition.name != "embed_and_index"]
    acts.append(slow_embed)

    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        await _wait_for_gate(handle)
        await handle.signal(IngestWorkflow.approve, Approval(approved=True))
        await _asyncio.wait_for(running.wait(), timeout=30)

        await handle.cancel()
        with pytest.raises(Exception):
            await handle.result()

    assert OUTCOMES == ["cancelled"], (
        "a run cancelled mid-activity must still tell the catalog"
    )


# -- the measurement --------------------------------------------------------


async def test_evaluation_is_absent_unless_it_was_approved(env: WorkflowEnvironment):
    """It is a paid stage like any other, so the gate governs it.

    Off by default, because a document can be perfectly worth indexing without
    anybody wanting to pay to find out how well it can be found.
    """
    async with Worker(
        env.client, task_queue=TASK_QUEUE,
        workflows=[IngestWorkflow], activities=activities(),
    ):
        handle = await _start(env, request(), StageOptions(), activities())
        await _wait_for_gate(handle)
        await handle.signal(IngestWorkflow.approve, Approval(approved=True))
        result = await handle.result()

    assert "evalset" not in SPENT and "evaluation" not in SPENT
    assert result.scores is None, "None means 'not asked'; 0.0 would be a claim"
    assert PERSISTED == []


async def test_an_approved_evaluation_reports_what_the_index_can_be_asked(
    env: WorkflowEnvironment,
):
    """The figure the product has never had for any of its 74 documents."""
    opts = StageOptions(generate_evalset=True)
    async with Worker(
        env.client, task_queue=TASK_QUEUE,
        workflows=[IngestWorkflow], activities=activities(),
    ):
        handle = await _start(env, request(), opts, activities())
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        result = await handle.result()

    assert SPENT.index("embedding") < SPENT.index("evalset"), (
        "it measured an index that had not been written"
    )
    assert result.scores is not None
    assert result.scores.recall_at_5 == 0.9
    # The noise floor travels with the recall, or the recall is not interpretable.
    assert result.scores.noise_floor == 0.6232
    assert PERSISTED == ["scores"], "the family did not learn what was measured"


async def test_nothing_is_measured_when_there_is_no_index_to_measure(
    env: WorkflowEnvironment,
):
    """Embedding declined, evaluation requested. Asking questions of a collection
    this run never wrote would score somebody else's document."""
    opts = StageOptions(embed=False, generate_evalset=True)
    async with Worker(
        env.client, task_queue=TASK_QUEUE,
        workflows=[IngestWorkflow], activities=activities(),
    ):
        handle = await _start(env, request(), opts, activities())
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        result = await handle.result()

    assert "evalset" not in SPENT
    assert result.scores is None


async def test_a_reused_eval_set_is_not_billed_twice(env: WorkflowEnvironment):
    """The questions belong to the document, not to the run. A second index of
    the same book reuses them — which is also what makes the two runs comparable,
    since resampling would measure the sample rather than the change."""

    @activity.defn(name="build_evalset")
    async def reused(*_args) -> EvalSet:
        SPENT.append("evalset")
        return EvalSet(
            items=EVALSET_REF, questions=40, sample=40, reused=True,
            spend=Spend("evalset", "gemini-3.6-flash", 0, 0, 0.0),
        )

    opts = StageOptions(generate_evalset=True)
    acts = activities(build_evalset=reused)
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts,
    ):
        handle = await _start(env, request(), opts, acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        result = await handle.result()

    # Correction + embedding + semantics + the evaluation's query embeddings,
    # as the doubles above price them. The eval set itself contributes nothing,
    # because it cost nothing — and a zero-dollar `Spend` appended anyway would
    # be indistinguishable from one that was simply cheap.
    assert result.total_usd == pytest.approx(0.0042 + 0.00045 + 0.0010 + 0.00008)
    assert "evalset" in SPENT, "the stage still ran; only the bill was absent"


async def test_an_eval_set_that_produced_no_questions_measures_nothing(
    env: WorkflowEnvironment,
):
    """A model that returned nothing usable is a smaller eval set, not a failed
    run — and an empty one must not be scored, because recall over zero questions
    is 0.0 and reads exactly like a broken index."""

    @activity.defn(name="build_evalset")
    async def empty(*_args) -> EvalSet:
        SPENT.append("evalset")
        return EvalSet(
            items=EVALSET_REF, questions=0, sample=40,
            spend=Spend("evalset", "gemini-3.6-flash", 100, 0, 0.0),
        )

    opts = StageOptions(generate_evalset=True)
    acts = activities(build_evalset=empty)
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts,
    ):
        handle = await _start(env, request(), opts, acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        result = await handle.result()

    assert "evaluation" not in SPENT
    assert result.scores is None
    assert result.state == "indexed"


# -- bounded tuning ---------------------------------------------------------


async def test_tuning_is_absent_unless_it_was_approved(env: WorkflowEnvironment):
    """It is the largest single line a gate can show: a full second embedding
    pass, about a hundred minutes for a 600-chunk book against the measured
    quota. Off unless somebody said yes to exactly that."""
    opts = StageOptions(generate_evalset=True)
    async with Worker(
        env.client, task_queue=TASK_QUEUE,
        workflows=[IngestWorkflow], activities=activities(),
    ):
        handle = await _start(env, request(), opts, activities())
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        await handle.result()

    assert "tuning" not in SPENT


async def test_a_candidate_inside_the_noise_margin_is_reverted_through_the_collection(
    env: WorkflowEnvironment,
):
    """Reverting the profile alone was the engine's measured bug: the collection
    kept the candidate's chunks, so the next baseline belonged to a configuration
    already rejected and three rounds drifted 0.729 → 0.762 → 0.700 having each
    reverted. Going back through chunking is what makes it honest.
    """
    # The candidate measures 0.7642 against a 0.700 baseline: +0.064, inside the
    # ±0.077 margin. Refusing it is the design working.
    opts = StageOptions(generate_evalset=True, tune=True)
    async with Worker(
        env.client, task_queue=TASK_QUEUE,
        workflows=[IngestWorkflow], activities=activities(),
    ):
        handle = await _start(env, request(), opts, activities())
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        result = await handle.result()

    # Three embeddings: the original index, the candidate, and the revert.
    assert SPENT.count("embedding") == 3
    # The scores that stand are the ones that measured the index that stands.
    assert result.scores is not None
    assert result.scores.recall_at_5 == 0.9
    assert result.state == "indexed", "a reverted round left the run without vectors"


async def test_a_candidate_that_beats_the_margin_is_kept(env: WorkflowEnvironment):
    """And then nothing is re-indexed a third time: the candidate's own index is
    the one that stands."""

    @activity.defn(name="evaluate_index")
    async def better(*_args) -> Scores:
        SPENT.append("evaluation")
        # 0.95 against a 0.700 baseline is +0.25, comfortably past ±0.077.
        return Scores(
            recall_at_1=0.9, recall_at_5=0.98, mrr_at_10=0.95,
            recall_at_5_dense_only=0.9, noise_floor=0.6, chunks=3,
            eval_questions=40, margin=0.05,
            spend=Spend("evaluation", "gemini-embedding-2", 400, 0, 0.00008),
        )

    opts = StageOptions(generate_evalset=True, tune=True)
    acts = activities(evaluate_index=better)
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts,
    ):
        handle = await _start(env, request(), opts, acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        result = await handle.result()

    assert SPENT.count("embedding") == 2, "it re-indexed a candidate it had kept"
    assert result.scores.mrr_at_10 == 0.95


async def test_a_free_knob_costs_no_second_pass(env: WorkflowEnvironment):
    """The retrieval half of a round changes nothing in the index — it is a
    different way of querying the same points — so it is adopted without one."""

    @activity.defn(name="propose_tuning")
    async def knob(*_args) -> TuneOutcome:
        SPENT.append("tuning")
        return TuneOutcome(
            kind="retrieval", label="per_section=3",
            baseline_objective=0.700, margin=0.077,
        )

    opts = StageOptions(generate_evalset=True, tune=True)
    acts = activities(propose_tuning=knob)
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts,
    ):
        handle = await _start(env, request(), opts, acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        result = await handle.result()

    assert "tuning" in SPENT
    assert SPENT.count("embedding") == 1
    assert result.state == "indexed"


async def test_the_second_gate_does_not_cost_the_document_its_profile(
    env: WorkflowEnvironment,
):
    """A defect that shipped, and that nothing could see.

    The second gate reassigned `decision` — the `ProfileDecision` every later
    stage reads — to the `Approval` it returned. `chunk_final` was then handed an
    `Approval` where it expects a decision, and Temporal's converter coerced it
    into a `ProfileDecision` with defaults instead of failing. So a run that
    reviewed its correction was chunked with the engine's built-in rules and the
    profile it had just paid to learn was thrown away, silently: the run
    succeeded, the chunk count looked ordinary, and the only visible symptom was
    a document whose table of contents was worse than its family's.

    What the property needs is the thing the bug destroyed: the rules that reach
    the final chunking are the profile's, after a second gate as before one.
    """
    seen: list[str] = []

    # Typed like the real activity on purpose: an untyped `*args` double is
    # handed raw dicts, and it is precisely the *typed* decoding that turns an
    # `Approval` into a defaulted `ProfileDecision` rather than an error.
    @activity.defn(name="chunk_final")
    async def recording(
        run_id: str, text_kind: str, decision: ProfileDecision | None = None
    ) -> Chunked:
        seen.append(decision.source if decision else "none")
        return Chunked(chunks=FINAL_REF, count=3, kinds=[ChunkKindCount("cuerpo", 3)])

    acts = [a for a in activities() if getattr(a, "__name__", "") != "chunk_final"]
    acts.append(recording)

    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve,
            Approval(approved=True, options=StageOptions(review_correction=True)),
        )
        for _ in range(400):
            if await handle.query(IngestWorkflow.stage) == "awaiting_correction_review":
                break
        await handle.signal(
            IngestWorkflow.approve,
            Approval(approved=True, options=StageOptions(extract_semantics=False)),
        )
        await handle.result()

    assert seen == ["reused"], (
        "the final chunking did not see the profile the gate reported"
    )


# -- a structural collision withholds activation ----------------------------


ACTIVATED: list[str] = []


@activity.defn(name="activate_version")
async def note_activation(*_args) -> None:
    ACTIVATED.append("activate_version")


def _blocking_activities(**kw):
    acts = [a for a in activities(**kw) if a is not activate_version]
    return acts + [note_activation]


def _collision(kind: str = "heading_disagreement") -> ProfileWarning:
    return ProfileWarning(
        profile_id="hermeneutica-110b1333",
        collides_with="libros/1-desde-agustin.pdf",
        similarity=0.02,
        detail=(
            "las reglas heredadas detectan 4 capítulo(s) donde las reglas por "
            "defecto detectan 1"
        ),
        kind=kind,
    )


async def test_a_collision_leaves_the_prior_version_active(env: WorkflowEnvironment):
    """The failure recall provably cannot see.

    The fingerprint groups by structure and structure is not subject matter: a
    hermeneutics chapter and a church-history book landed on `110b1333` on the
    real corpus, and the second was chunked with the first's heading rules. The
    eval questions are generated from the very chunks those rules produced, so a
    measured recall says nothing about it.

    Everything still runs — the index exists, the graph is projected, the money
    is spent and reported. Only the promotion is withheld, because a document
    indexed under the wrong structure is worse than one that is a click away
    from being published.
    """
    ACTIVATED.clear()
    acts = _blocking_activities(resolver=resolver(warnings=[_collision()]))
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        await _wait_for_gate(handle)
        await handle.signal(IngestWorkflow.approve, Approval(approved=True))
        result = await handle.result()

    assert result.state == "blocked_structural"
    assert "activate_version" not in ACTIVATED
    assert "capítulo" in result.detail
    # It is not a failure: the stages ran and their bill is reported.
    assert result.total_usd is not None and result.total_usd > 0
    assert result.indexed_chunks == 3


async def test_a_plain_collision_does_not_block(env: WorkflowEnvironment):
    """Sharing a fingerprint is ordinary — it is what makes the second document
    of a family cheaper than the first. Blocking on it would fire on every
    successful reuse, and a warning that fires on success is one an operator
    learns to click past."""
    ACTIVATED.clear()
    acts = _blocking_activities(resolver=resolver(warnings=[_collision(kind="collision")]))
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        await _wait_for_gate(handle)
        await handle.signal(IngestWorkflow.approve, Approval(approved=True))
        result = await handle.result()

    assert result.state == "indexed"
    assert "activate_version" in ACTIVATED


async def test_declining_the_inherited_profile_is_already_the_way_out(
    env: WorkflowEnvironment,
):
    """`ignore_profile` chunks with the measured defaults, so there is no
    disagreement left to act on and nothing to withhold."""
    opts = StageOptions(ignore_profile=True)
    acts = activities(resolver=resolver(warnings=[_collision()]))
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), opts, acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        result = await handle.result()

    assert result.state == "indexed"


async def test_a_learned_profile_is_never_blocked_by_its_own_rules(
    env: WorkflowEnvironment,
):
    """The check is about *inherited* rules. A document that learned its own
    cannot be colliding with anybody."""
    acts = activities(
        resolver=resolver(source="default", warnings=[_collision()])
    )
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts
    ):
        handle = await _start(env, request(), StageOptions(), acts)
        await _wait_for_gate(handle)
        await handle.signal(IngestWorkflow.approve, Approval(approved=True))
        result = await handle.result()

    assert result.state == "indexed"


async def test_a_reverted_candidate_does_not_overwrite_the_baselines_measurement(
    env: WorkflowEnvironment,
):
    """Found by running a real tuning round, not by reading the code.

    Both measurements happen inside one run, and both were writing `scores.json`.
    So a candidate that was measured and then reverted left the artifact
    describing an index that had already been thrown away: `scores.json` said 676
    chunks and recall@5 0.8375 while the collection and `chunks.jsonl` both held
    the reverted 600. `/runs/{id}` and the Library screen read that artifact, so
    the product would have reported a recall for an index nobody could query.

    Same shape as the engine's own measured bug at the other end — reverting the
    profile while leaving the collection holding the candidate's chunks.
    """
    opts = StageOptions(generate_evalset=True, tune=True)
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts,
    ):
        handle = await _start(env, request(), opts, acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        await handle.result()

    # The default double proposes a candidate that scores 0.7642 against a 0.700
    # baseline: +0.064, inside the ±0.077 margin, so it is reverted.
    assert EVALUATED_INTO == ["scores", "scores_candidate"]
    assert PROMOTED == [], "a reverted candidate was promoted over the baseline"


async def test_a_kept_candidate_becomes_the_runs_measurement(env: WorkflowEnvironment):
    """The other half: when it wins, its measurement *is* the one that describes
    the index that stands, so it is promoted over the baseline's."""

    @activity.defn(name="evaluate_index")
    async def better(*args) -> Scores:
        SPENT.append("evaluation")
        EVALUATED_INTO.append(args[5] if len(args) > 5 else "scores")
        return Scores(
            recall_at_1=0.9, recall_at_5=0.98, mrr_at_10=0.95,
            recall_at_5_dense_only=0.9, noise_floor=0.6, chunks=3,
            eval_questions=40, margin=0.05,
            spend=Spend("evaluation", "gemini-embedding-2", 400, 0, 0.00008),
        )

    opts = StageOptions(generate_evalset=True, tune=True)
    acts = activities(evaluate_index=better)
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestWorkflow], activities=acts,
    ):
        handle = await _start(env, request(), opts, acts)
        await _wait_for_gate(handle)
        await handle.signal(
            IngestWorkflow.approve, Approval(approved=True, options=opts)
        )
        await handle.result()

    assert EVALUATED_INTO == ["scores", "scores_candidate"]
    assert PROMOTED, "a kept candidate left the run describing the old index"
