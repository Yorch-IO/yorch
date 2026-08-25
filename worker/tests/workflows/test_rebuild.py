"""The rebuild workflow: what it replays, and what it refuses to spend.

Activities are mocked, so what is under test is the workflow's own reasoning.
Two properties carry the weight. Nothing embeds before the gate is answered —
embedding is the only stage here that can spend, so an unanswered gate must cost
zero. And a document with no `semantics` artifact still rebuilds, restoring
structure and vectors while saying it restored nothing else.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.artifacts import ArtifactRef
from brainworker.pipeline import (
    Estimate,
    Indexed,
    IngestRequest,
    RebuildInputs,
    Registered,
    Semantics,
    Spend,
    StageEstimate,
    Staged,
)
from brainworker.workflows.ingest import Approval
from brainworker.workflows.rebuild import RebuildWorkflow

TASK_QUEUE = "test-rebuild"

CHUNKS = ArtifactRef(
    kind="chunks", path="runs/old/chunks.jsonl", sha256="a" * 64, bytes=99, rows=12
)
SEMANTICS = ArtifactRef(
    kind="semantics", path="runs/old/semantics.json", sha256="b" * 64, bytes=42
)

#: Anything that would cost money records itself here.
SPENT: list[str] = []


@pytest.fixture(autouse=True)
def _clear():
    SPENT.clear()
    yield
    SPENT.clear()


def loader(*, semantics: ArtifactRef | None = SEMANTICS):
    @activity.defn(name="load_rebuild_inputs")
    async def _load(*_args) -> RebuildInputs:
        return RebuildInputs(
            request=IngestRequest(
                library_id="lib_1", source_path="/workspace/inbox/a.pdf",
                source_key="libros/a.pdf", title="Institución", reindex=True,
            ),
            staged=Staged(
                content_sha256="c" * 64, byte_size=100, fmt="pdf",
                extractor="", title="Institución",
            ),
            registered=Registered(
                document_id="doc_1", version_id="ver_1",
                created=False, already_indexed=True,
            ),
            chunks=CHUNKS,
            characters=4800,
            semantics=semantics,
            source_run_id="ingest-old",
        )

    return _load


@activity.defn(name="estimate_cost")
async def estimate_cost(*_args) -> Estimate:
    return Estimate(
        stages=[
            StageEstimate(
                stage="embedding", model="gemini-embedding-2",
                input_tokens=1200, output_tokens=0, usd=0.00024,
            )
        ],
        total_usd=0.00024,
        price_source="third-party aggregators, 2026-08-20",
    )


@activity.defn(name="embed_and_index")
async def embed_and_index(*_args) -> Indexed:
    SPENT.append("embedding")
    return Indexed(
        collection="brain", points=12, dimensions=3072,
        spend=Spend(stage="embedding", model="gemini-embedding-2", usd=0.00024),
    )


@activity.defn(name="project_structure")
async def project_structure(*_args) -> dict:
    return {"sections": 4, "chunks": 12}


REPLAYED: list[str] = []


@activity.defn(name="replay_semantics")
async def replay_semantics(*_args) -> Semantics:
    REPLAYED.append("ran")
    return Semantics(
        concepts=13, claims=13, edges=26,
        spend=Spend(stage="semantics-replay", model="", usd=0.0),
    )


@activity.defn(name="activate_version")
async def activate_version(*_args) -> None:
    return None


@activity.defn(name="record_run_outcome")
async def record_run_outcome(*_args) -> None:
    return None


@activity.defn(name="set_run_stage")
async def set_run_stage(*_args) -> None:
    return None


def activities(*, semantics: ArtifactRef | None = SEMANTICS):
    return [
        loader(semantics=semantics), estimate_cost, embed_and_index,
        project_structure, replay_semantics, activate_version,
        record_run_outcome, set_run_stage,
    ]


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


async def _start(env: WorkflowEnvironment):
    client: Client = env.client
    return await client.start_workflow(
        RebuildWorkflow.run,
        args=["lib_1", "doc_1"],
        id=f"rebuild-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )


async def _wait_for_gate(handle):
    for _ in range(200):
        report = await handle.query(RebuildWorkflow.gate_report)
        if report is not None:
            return report
    raise AssertionError("the gate never became ready")


async def test_nothing_is_embedded_before_the_gate_is_answered(env):
    """Embedding is the only stage a rebuild can spend on, so an unanswered
    gate has to cost exactly nothing."""
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[RebuildWorkflow], activities=acts
    ):
        handle = await _start(env)
        report = await _wait_for_gate(handle)

        assert SPENT == [], "a rebuild spent before anyone approved it"
        assert report.chunk_count == 12
        assert report.source_run_id == "ingest-old"
        assert report.estimate.total_usd == pytest.approx(0.00024)

        await handle.signal(RebuildWorkflow.approve, Approval(approved=False))
        result = await handle.result()

    assert result.state == "cancelled"
    assert SPENT == []


async def test_an_approved_rebuild_replays_the_artifacts(env):
    REPLAYED.clear()
    acts = activities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[RebuildWorkflow], activities=acts
    ):
        handle = await _start(env)
        await _wait_for_gate(handle)
        await handle.signal(RebuildWorkflow.approve, Approval(approved=True))
        result = await handle.result()

    assert result.state == "rebuilt"
    assert result.points == 12
    assert result.graph == {"sections": 4, "chunks": 12}
    assert result.semantics_replayed is True
    assert REPLAYED == ["ran"]
    assert SPENT == ["embedding"]


async def test_a_document_with_no_semantics_artifact_still_rebuilds(env):
    """Documents indexed before the `semantics` artifact existed have nothing to
    replay. Restoring structure and vectors is the honest outcome; the report
    says what was not restored rather than leaving a thinner graph unexplained.
    """
    REPLAYED.clear()
    acts = activities(semantics=None)
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[RebuildWorkflow], activities=acts
    ):
        handle = await _start(env)
        report = await _wait_for_gate(handle)
        assert report.semantics_available is False

        await handle.signal(RebuildWorkflow.approve, Approval(approved=True))
        result = await handle.result()

    assert result.state == "rebuilt"
    assert result.semantics_replayed is False
    assert REPLAYED == [], "there was nothing to replay"
