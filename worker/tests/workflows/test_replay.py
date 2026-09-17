"""Replay real histories against the current workflow code.

The one test in this repository that can say "a change to a workflow's body
did not change the commands it issues". Everything else exercises a workflow
forwards, against doubles, and passes whether or not a run recorded under the
previous code would still replay — which is the question that matters for
the eleven video runs parked at their gates in the local namespace on the day
the tail was moved into `workflows/timed.py`.

`fixtures/video_rejected_20260916.json` was exported from the local Temporal
**before** that extraction, with `WorkflowHandle.fetch_history().to_json()`.
It carries the whole pre-gate half — open, probe, register, record artifacts,
estimate — and a rejection, which is the half every parked run has already
issued. Replaying it under the refactored class is the assertion; a
non-determinism error is the failure.

The second test closes the other half: it runs a video through the full tail
against doubles, captures the history, and replays it. That is
self-consistency rather than a before/after — no completed full-tail run
survived retention on the day — and it is what stands until a real one does.
"""

from __future__ import annotations

import json
import pathlib
import uuid

import pytest
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from brainworker.pipeline import StageOptions
from brainworker.workflows.ingest import Approval
from brainworker.workflows.video import VideoIngestWorkflow

import test_video as doubles  # noqa: E402 - sibling module, tests/workflows has no __init__

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


async def test_a_history_recorded_before_the_tail_moved_still_replays():
    raw = (FIXTURES / "video_rejected_20260916.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    scheduled = [
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in payload["events"]
        if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
    ]
    # What the fixture is: the pre-gate half and a rejection. If somebody
    # replaces it with a different run, the test should say what it now covers.
    assert scheduled[:4] == ["open_run", "set_run_stage", "resolve_video", "probe_video"]
    assert scheduled[-1] == "record_run_outcome"

    history = WorkflowHistory.from_json("video-fixture", raw)
    replayer = Replayer(workflows=[VideoIngestWorkflow])
    await replayer.replay_workflow(history)  # raises on non-determinism


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


async def test_a_full_run_through_the_shared_tail_replays_against_itself(env):
    doubles.SPENT.clear(); doubles.CALLED.clear(); doubles.POLLS.clear()
    doubles.QUOTED.clear(); doubles.OPENED.clear(); doubles.BOOKS.clear()
    doubles.CHUNK_WARNINGS.clear()
    queue = "test-replay"
    async with Worker(env.client, task_queue=queue, workflows=[VideoIngestWorkflow],
                      activities=doubles.activities(captions=False,
                                                    statuses=["IN_PROGRESS", "COMPLETED"])):
        handle = await env.client.start_workflow(
            VideoIngestWorkflow.run,
            args=[doubles.request(), StageOptions()],
            id=f"replay-{uuid.uuid4()}",
            task_queue=queue,
        )
        report = await doubles._wait_for_gate(handle)
        # As a client does: echo the recommended switches back, so the run
        # does what the gate quoted. A bare `Approval(approved=True)` carries
        # `StageOptions()` defaults, which is the under-reporting case.
        await handle.signal(
            VideoIngestWorkflow.approve,
            Approval(approved=True, options=report.recommended),
        )
        result = await handle.result()
        assert result.state == "indexed"
        history = await handle.fetch_history()

    scheduled = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert "correct_text" not in scheduled, "Transcribe output is not corrected by default"
    assert scheduled[-3:] == ["activate_version", "set_run_stage", "record_run_outcome"]
    replayer = Replayer(workflows=[VideoIngestWorkflow])
    await replayer.replay_workflow(history)
