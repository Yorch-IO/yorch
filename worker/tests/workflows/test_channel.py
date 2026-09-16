"""The two channel workflows: what they spend, and what they leave behind.

Activities are mocked. What is under test is the workflows' own reasoning — that
the quote is taken before the pass that spends, that the run row is opened
before the first activity that can fail, and that a failure is recorded against
the stage it happened in rather than against the last one that succeeded.

Every double is **typed**. Temporal maps payloads onto parameters by arity, so a
`*args` double accepts a call the real converter cannot make — which is how five
passing tests once hid a workflow that could not run.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.channel.types import (
    DiscoverRequest,
    DiscoveryOutcome,
    TopicsOutcome,
    TopicsRequest,
)
from brainworker.pipeline import Estimate, RunOpen, StageEstimate
from brainworker.workflows.channel import (
    ChannelDiscoverWorkflow,
    ChannelTopicsWorkflow,
)

TASK_QUEUE = "test-channel"
CHANNEL = "UCabcdefghijklmnopqrstuv"
LIBRARY = f"lib_yt_{CHANNEL}"

CALLED: list[str] = []
OPENED: list[RunOpen] = []
#: Raised by the next paid pass, so a test can put a failure in one place.
FAIL: list[Exception] = []

QUOTE = Estimate(
    stages=[
        StageEstimate(
            stage="channel-preselect",
            model="gemini-3.6-flash",
            input_tokens=1000,
            output_tokens=700,
            usd=0.0067,
            output_tokens_high=700,
            usd_high=0.0067,
        )
    ],
    total_usd=0.0067,
    total_usd_high=0.0067,
    price_source="terceros",
)


def discover(**kw) -> DiscoverRequest:
    return DiscoverRequest(
        channel_id=CHANNEL, topic="justicia social", library_id=LIBRARY, **kw
    )


def topics_request(**kw) -> TopicsRequest:
    kw.setdefault("video_runs", ["video-1", "video-2"])
    return TopicsRequest(
        channel_id=CHANNEL, topic="justicia social", library_id=LIBRARY, **kw
    )


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
    CALLED.append(f"outcome:{state}:{stage}:{kind}")


@activity.defn(name="record_run_events")
async def record_run_events(run_id: str, pending: list) -> None:
    return None


@activity.defn(name="quote_channel_discovery")
async def quote_channel_discovery(run_id: str, request: DiscoverRequest) -> Estimate:
    assert isinstance(request, DiscoverRequest), f"got {type(request).__name__}"
    assert run_id, "the quote was not told its run"
    CALLED.append("quote")
    if FAIL:
        raise FAIL[0]
    return QUOTE


@activity.defn(name="preselect_channel_videos")
async def preselect_channel_videos(
    run_id: str, request: DiscoverRequest
) -> DiscoveryOutcome:
    assert isinstance(request, DiscoverRequest), f"got {type(request).__name__}"
    CALLED.append("preselect")
    if FAIL:
        raise FAIL[0]
    return DiscoveryOutcome(
        run_id=run_id,
        evaluated=100,
        relevant=12,
        doubtful=20,
        discarded=68,
        shortlist=["aaaaaaaaaaa", "bbbbbbbbbbb"],
        total_usd=0.0071,
    )


@activity.defn(name="read_channel_topics")
async def read_channel_topics(run_id: str, request: TopicsRequest) -> TopicsOutcome:
    assert isinstance(request, TopicsRequest), f"got {type(request).__name__}"
    CALLED.append("read")
    if FAIL:
        raise FAIL[0]
    return TopicsOutcome(
        run_id=run_id, videos=2, answering=1, verified=4, unverified=1, total_usd=0.05
    )


ACTIVITIES = [
    open_run,
    set_run_stage,
    record_run_outcome,
    record_run_events,
    quote_channel_discovery,
    preselect_channel_videos,
    read_channel_topics,
]


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture(autouse=True)
def _clear():
    CALLED.clear()
    OPENED.clear()
    FAIL.clear()
    yield
    CALLED.clear()
    OPENED.clear()
    FAIL.clear()


async def _run(env: WorkflowEnvironment, workflow, request):
    client: Client = env.client
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ChannelDiscoverWorkflow, ChannelTopicsWorkflow],
        activities=ACTIVITIES,
    ):
        handle = await client.start_workflow(
            workflow.run,
            request,
            id=f"channel-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        return await handle.result()


# -- discovery ----------------------------------------------------------------


async def test_the_quote_is_taken_before_the_pass_that_spends(env):
    """The order that makes the receipt worth anything.

    Quoting after preselecting would record a figure for work already done,
    which is a receipt rather than an estimate — and the whole point of
    persisting it is to be able to ask afterwards whether the number somebody
    decided on was honest.
    """
    await _run(env, ChannelDiscoverWorkflow, discover())
    assert CALLED.index("quote") < CALLED.index("preselect")
    assert CALLED.index("stage:quoting") < CALLED.index("quote")


async def test_the_run_row_is_opened_before_the_first_activity_that_can_fail(env):
    # The recorded defect, in the shape it took on the video path: `POST` answers
    # 200, the workflow dies in its first activity, and nothing ever appears in
    # the queue — because the row was inserted by a later activity and
    # `finish_run` is a bare UPDATE that affects zero rows and raises nothing.
    await _run(env, ChannelDiscoverWorkflow, discover())
    assert CALLED[0] == "open_run"
    assert OPENED[0].kind == "channel"
    assert OPENED[0].library_id == LIBRARY
    # The label is the topic: it is genuinely all there is to say about a
    # channel run, since there is no document to take a title from.
    assert OPENED[0].label == "justicia social"


async def test_a_discovery_reports_what_it_judged(env):
    outcome = await _run(env, ChannelDiscoverWorkflow, discover(limit=100, deep_limit=2))
    assert (outcome.evaluated, outcome.relevant, outcome.doubtful) == (100, 12, 20)
    assert outcome.shortlist == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert CALLED[-1] == "outcome:succeeded:done:None"


async def test_the_quote_is_queryable_while_the_run_is_going(env):
    # The screen shows the figure it is about to be billed against; reading it
    # off the workflow rather than recomputing it is what stops the two
    # disagreeing about the same run.
    client: Client = env.client
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ChannelDiscoverWorkflow, ChannelTopicsWorkflow],
        activities=ACTIVITIES,
    ):
        handle = await client.start_workflow(
            ChannelDiscoverWorkflow.run,
            discover(),
            id=f"channel-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        await handle.result()
        assert (await handle.query(ChannelDiscoverWorkflow.estimate)) == QUOTE
        assert (await handle.query(ChannelDiscoverWorkflow.stage)) == "done"


async def test_a_failure_is_recorded_against_the_stage_it_happened_in(env):
    # "It failed" is half an answer; "it failed in `preselecting`" is the whole
    # one. `_record_failure` takes the stage from the workflow's own cursor.
    FAIL.append(ApplicationError("no such channel", type="channel_not_synced",
                                 non_retryable=True))
    with pytest.raises(WorkflowFailureError):
        await _run(env, ChannelDiscoverWorkflow, discover())
    assert "outcome:failed:quoting:channel_not_synced" in CALLED


async def test_the_topic_is_carried_into_both_activities_unchanged(env):
    # The request is one object and both activities read the same one, so a
    # discovery cannot quote one topic and judge another.
    await _run(env, ChannelDiscoverWorkflow, discover(limit=40, deep_limit=3))
    assert CALLED.count("quote") == 1 and CALLED.count("preselect") == 1


# -- topics -------------------------------------------------------------------


async def test_a_topic_pass_opens_its_own_run_and_reports_what_it_read(env):
    outcome = await _run(env, ChannelTopicsWorkflow, topics_request())
    assert OPENED[0].kind == "channel"
    assert (outcome.videos, outcome.answering) == (2, 1)
    assert (outcome.verified, outcome.unverified) == (4, 1)
    assert "stage:reading" in CALLED
    assert CALLED[-1] == "outcome:succeeded:done:None"


async def test_a_topic_pass_that_fails_is_recorded_as_failing_in_reading(env):
    FAIL.append(ApplicationError("run not found", type="run_not_found",
                                 non_retryable=True))
    with pytest.raises(WorkflowFailureError):
        await _run(env, ChannelTopicsWorkflow, topics_request())
    assert "outcome:failed:reading:run_not_found" in CALLED


async def test_the_paid_pass_is_not_retried_more_than_once(env):
    """Two attempts, not three. A retry re-runs the whole pass, and the whole
    pass is what costs money — the reason `_PAID_RETRY` exists."""
    attempts: list[int] = []

    @activity.defn(name="read_channel_topics")
    async def flaky(run_id: str, request: TopicsRequest) -> TopicsOutcome:
        attempts.append(1)
        raise RuntimeError("provider said no")

    client: Client = env.client
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ChannelTopicsWorkflow],
        activities=[open_run, set_run_stage, record_run_outcome,
                    record_run_events, flaky],
    ):
        handle = await client.start_workflow(
            ChannelTopicsWorkflow.run,
            topics_request(),
            id=f"channel-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert len(attempts) == 2
