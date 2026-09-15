"""The conversation workflow: a session over a record that outlives it.

Activities are mocked. What is under test is the workflow's own behaviour — that
a turn arriving as a signal is answered, that the window carries context forward
and stays bounded, that a failed turn does not end the session, and that going
quiet is a normal ending rather than a failure.

One trap worth knowing before adding to this file: **a test that leaves a
workflow running takes the rest of the module down with it.** The time-skipping
server only advances the clock when every workflow is idle, so a session nobody
closed — an assertion failing inside `async with Worker(...)` is enough — makes
`test_going_quiet_ends_the_session_normally` wait a real hour instead of a
skipped one. It looks like a hang in that test and is a leak in another.

**Every double is typed like the real activity.** Temporal's converter maps
payloads onto parameters by arity, and an untyped `*args` double accepts any of
them — which is how a five-argument call to a six-parameter activity once passed
five workflow tests and failed against the real converter.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from brainworker.chat.types import (
    WINDOW_TURNS,
    ChatStart,
    ChatTurn,
    TurnRecord,
    TurnResult,
)
from brainworker.pipeline import Spend
from brainworker.workflows.chat import TURNS_PER_RUN, ChatWorkflow

TASK_QUEUE = "test-chat"
TENANT = "tnt_" + "a" * 24


@dataclass
class Seen:
    """What the mocked activities were handed, in order."""

    runs: list[tuple[str, str]] = field(default_factory=list)
    windows: list[list[str]] = field(default_factory=list)
    booked: list[tuple[str, int]] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    named: list[str] = field(default_factory=list)


SEEN = Seen()


@pytest.fixture(autouse=True)
def _clear():
    global SEEN
    SEEN = Seen()
    yield


@activity.defn(name="open_chat_run")
async def open_chat_run(run_id: str, turn: ChatTurn, label: str) -> None:
    SEEN.runs.append((run_id, label))


@activity.defn(name="run_chat_turn")
async def answers(turn: ChatTurn, window: list[TurnRecord]) -> TurnResult:
    SEEN.windows.append([w.question for w in window])
    return TurnResult(
        seq=turn.turn_seq,
        state="answered",
        searched=f"[reescrita] {turn.text}",
        head=f"respuesta a {turn.text}",
        spend=[Spend("answering", "m", 10, 5, 0.001)],
    )


@activity.defn(name="run_chat_turn")
async def refuses(turn: ChatTurn, window: list[TurnRecord]) -> TurnResult:
    raise ApplicationError(
        "Vertex AI quota exhausted", "provider_quota",
        type="ProviderError", non_retryable=True,
    )


@activity.defn(name="fail_chat_turn")
async def fail_chat_turn(turn: ChatTurn, error: dict) -> None:
    SEEN.failed.append(error)


@activity.defn(name="name_conversation")
async def name_conversation(turn: ChatTurn, question: str, answer: str) -> list[Spend]:
    SEEN.named.append(question)
    return [Spend("chat-title", "m", 5, 2, 0.0001)]


@activity.defn(name="record_turn_cost")
async def record_turn_cost(run_id: str, state: str, spend: list[Spend]) -> None:
    SEEN.booked.append((state, len(spend)))


ACTS = [open_chat_run, fail_chat_turn, name_conversation, record_turn_cost]


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


def start(**kw) -> ChatStart:
    base = dict(conversation_id="cnv_1", library_id="lib_a", tenant_id=TENANT)
    return ChatStart(**{**base, **kw})


def turn(seq: int, text: str) -> ChatTurn:
    return ChatTurn(
        conversation_id="cnv_1", turn_seq=seq, text=text,
        library_id="lib_a", tenant_id=TENANT,
    )


async def settled(handle, seq: int, tries: int = 200):
    """Poll one turn's outcome until it stops being `running`.

    A signal returns as soon as it is *accepted*, not when the turn it queued
    has been answered, so a query straight afterwards races the activity. Only
    the tests that have to observe a live session use this; the rest close the
    session and read the outcomes off the finished workflow, which cannot race.
    """
    for _ in range(tries):
        outcome = await handle.query(ChatWorkflow.turn, seq)
        if outcome.state != "running":
            return outcome
        await asyncio.sleep(0.02)
    raise AssertionError(f"turn {seq} never settled")


async def _session(env: WorkflowEnvironment, act, texts, *, begin=None):
    """Start a session, signal every turn, close it, and read what it did.

    The queries come *after* `result()` on purpose. Temporal answers a query on a
    completed workflow perfectly well, and doing it this way removes the race
    between the signal being accepted and the turn being answered.
    """
    client: Client = env.client
    async with Worker(
        client, task_queue=TASK_QUEUE, workflows=[ChatWorkflow], activities=[act, *ACTS]
    ):
        handle = await client.start_workflow(
            ChatWorkflow.run,
            begin or start(),
            id=f"chat-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        for i, text in enumerate(texts, start=1):
            await handle.signal(ChatWorkflow.ask, turn(i, text))
        await handle.signal(ChatWorkflow.close)
        await handle.result()
        outcomes = [
            await handle.query(ChatWorkflow.turn, i)
            for i in range(1, len(texts) + 1)
        ]
        return outcomes, await handle.query(ChatWorkflow.transcript)


# -- answering --------------------------------------------------------------


async def test_a_turn_signalled_in_is_answered(env: WorkflowEnvironment):
    outcomes, _ = await _session(env, answers, ["¿Quién fue Jesucristo?"])
    assert outcomes[0].state == "done"
    assert outcomes[0].searched == "[reescrita] ¿Quién fue Jesucristo?"


async def test_the_second_turn_sees_the_first(env: WorkflowEnvironment):
    """The whole multi-turn mechanism: the rewrite is handed the window."""
    await _session(env, answers, ["¿Quién fue Jesucristo?", "¿y su muerte?"])
    assert SEEN.windows[0] == [], "the first turn has nothing to remember"
    assert SEEN.windows[1] == ["¿Quién fue Jesucristo?"]


async def test_the_window_stops_growing(env: WorkflowEnvironment):
    texts = [f"pregunta {i}" for i in range(WINDOW_TURNS + 3)]
    _, window = await _session(env, answers, texts)
    assert len(window) == WINDOW_TURNS
    # The *recent* end is what a pronoun reaches into.
    assert [w.question for w in window] == texts[-WINDOW_TURNS:]


async def test_every_turn_gets_its_own_run_row(env: WorkflowEnvironment):
    """`record_cost` derives its tenant from the run a charge hangs off, so a
    conversation-wide row would make "what did this turn cost" unanswerable."""
    await _session(env, answers, ["una", "dos"])
    assert [label for _, label in SEEN.runs] == ["una", "dos"]
    assert len({run_id for run_id, _ in SEEN.runs}) == 2


async def test_the_bill_reaches_the_ledger(env: WorkflowEnvironment):
    await _session(env, answers, ["una"])
    # answering + chat-title on the first turn.
    assert SEEN.booked == [("answered", 2)]


# -- naming -----------------------------------------------------------------


async def test_a_conversation_is_named_once(env: WorkflowEnvironment):
    await _session(env, answers, ["primera", "segunda", "tercera"])
    assert SEEN.named == ["primera"]


async def test_a_resumed_session_does_not_rename(env: WorkflowEnvironment):
    """`answered > 0` means an earlier session already had the chance."""
    await _session(env, answers, ["siguiente"], begin=start(answered=4))
    assert SEEN.named == []


# -- failure ----------------------------------------------------------------


async def test_a_failed_turn_is_settled_not_left_running(env: WorkflowEnvironment):
    """Leaving the row at `running` would make a turn that died look exactly like
    one still being answered."""
    outcomes, _ = await _session(env, refuses, ["¿?"])
    assert outcomes[0].state == "failed"
    assert outcomes[0].error["kind"] == "provider_quota"
    assert SEEN.failed == [{"kind": "provider_quota", "message": "Vertex AI quota exhausted"}]


async def test_a_failed_turn_does_not_end_the_conversation(env: WorkflowEnvironment):
    """The reader can ask again, and a workflow that ended over one refused
    generation would take the session's window with it."""
    client: Client = env.client
    async with Worker(
        client, task_queue=TASK_QUEUE, workflows=[ChatWorkflow],
        activities=[refuses, *ACTS],
    ):
        handle = await client.start_workflow(
            ChatWorkflow.run, start(), id=f"chat-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        await handle.signal(ChatWorkflow.ask, turn(1, "falla"))
        assert (await settled(handle, 1)).state == "failed"
        # Still alive and still accepting.
        await handle.signal(ChatWorkflow.ask, turn(2, "otra"))
        assert (await settled(handle, 2)).state == "failed"
        await handle.signal(ChatWorkflow.close)
        await handle.result()
    assert len(SEEN.failed) == 2


# -- the session's own shape ------------------------------------------------


async def test_a_turn_this_session_never_saw_reads_as_running(env: WorkflowEnvironment):
    """After a continue_as_new or a resumed session, an older turn's outcome is
    in the catalog. Raising here would turn "ask somewhere else" into "failed".
    """
    client: Client = env.client
    async with Worker(
        client, task_queue=TASK_QUEUE, workflows=[ChatWorkflow], activities=[answers, *ACTS]
    ):
        handle = await client.start_workflow(
            ChatWorkflow.run, start(), id=f"chat-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        assert (await handle.query(ChatWorkflow.turn, 99)).state == "running"
        await handle.signal(ChatWorkflow.close)
        await handle.result()


async def test_a_repeated_signal_is_not_asked_twice(env: WorkflowEnvironment):
    """The sequence number is claimed in Postgres before the signal is sent, so a
    duplicated delivery names a turn already queued — and asking it again would
    bill the reader twice for one question."""
    client: Client = env.client
    async with Worker(
        client, task_queue=TASK_QUEUE, workflows=[ChatWorkflow], activities=[answers, *ACTS]
    ):
        handle = await client.start_workflow(
            ChatWorkflow.run, start(), id=f"chat-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        await handle.signal(ChatWorkflow.ask, turn(1, "una"))
        await handle.signal(ChatWorkflow.ask, turn(1, "una"))
        await handle.signal(ChatWorkflow.close)
        await handle.result()
    assert len(SEEN.runs) == 1


async def test_going_quiet_ends_the_session_normally(env: WorkflowEnvironment):
    """Dormant, not failed. The record is in the catalog and the next turn starts
    a new session from it, which is what makes a conversation outlive Temporal's
    retention."""
    client: Client = env.client
    async with Worker(
        client, task_queue=TASK_QUEUE, workflows=[ChatWorkflow], activities=[answers, *ACTS]
    ):
        handle = await client.start_workflow(
            ChatWorkflow.run, start(), id=f"chat-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        # Time-skipping fast-forwards the idle timeout.
        await handle.result()
    assert SEEN.runs == []


# -- continuing as new ------------------------------------------------------


async def answered(n: int, tries: int = 3000):
    """Wait until `n` turns have been answered *and booked*.

    Keyed on `record_turn_cost`, the last activity of a turn, rather than on
    `open_chat_run`, the first. Waiting on the first is waiting for a turn to
    *start*, which is a race dressed as a synchronisation point.

    Polls the doubles rather than the workflow query, and that is not laziness:
    `continue_as_new` does not carry `_outcomes`, so a turn answered by the
    previous run reads as `running` for ever afterwards — which is exactly what
    `turn()` documents and what the catalog is for. A test that polled the query
    across the boundary would be waiting for something that is never coming.
    """
    for _ in range(tries):
        if len(SEEN.booked) >= n:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"only {len(SEEN.booked)} of {n} turns were answered")


async def _drain(handle, texts):
    """Signal every turn and wait for them all to be answered, without closing."""
    for i, text in enumerate(texts, start=1):
        await handle.signal(ChatWorkflow.ask, turn(i, text))
    await answered(len(texts))


async def test_a_long_conversation_continues_as_new(env: WorkflowEnvironment):
    """History is bounded by `TURNS_PER_RUN`, and the boundary must be invisible.

    Deliberately *not* closing the session part-way: `close` is checked before
    the continue-as-new branch, so a test that closed would return from the run
    without ever reaching it — and would pass while proving nothing. What proves
    it is the run id changing under a workflow id that did not.
    """
    client: Client = env.client
    texts = [f"pregunta {i}" for i in range(TURNS_PER_RUN)]
    async with Worker(
        client, task_queue=TASK_QUEUE, workflows=[ChatWorkflow], activities=[answers, *ACTS]
    ):
        handle = await client.start_workflow(
            ChatWorkflow.run, start(), id=f"chat-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        first_run = (await handle.describe()).run_id
        await _drain(handle, texts)

        # The next turn is answered by whatever run is live by then.
        await handle.signal(ChatWorkflow.ask, turn(len(texts) + 1, "después"))
        await answered(len(texts) + 1)

        assert (await handle.describe()).run_id != first_run, "never continued as new"

        window = await handle.query(ChatWorkflow.transcript)
        await handle.signal(ChatWorkflow.close)
        await handle.result()

    assert len(SEEN.runs) == len(texts) + 1, "a turn was lost at the boundary"
    # The window crossed the boundary rather than starting again from empty.
    assert [w.question for w in window] == (texts + ["después"])[-WINDOW_TURNS:]
    # And the run that inherited the conversation did not rename it.
    assert SEEN.named == ["pregunta 0"]


async def test_the_first_turn_after_the_boundary_still_sees_its_predecessors(
    env: WorkflowEnvironment,
):
    """What a rewrite reads after a continue_as_new is the same window it would
    have read before one. The boundary is a Temporal detail, not a conversational
    one."""
    client: Client = env.client
    texts = [f"pregunta {i}" for i in range(TURNS_PER_RUN)]
    async with Worker(
        client, task_queue=TASK_QUEUE, workflows=[ChatWorkflow], activities=[answers, *ACTS]
    ):
        handle = await client.start_workflow(
            ChatWorkflow.run, start(), id=f"chat-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        await _drain(handle, texts)
        await handle.signal(ChatWorkflow.ask, turn(len(texts) + 1, "después"))
        await answered(len(texts) + 1)
        await handle.signal(ChatWorkflow.close)
        await handle.result()

    assert SEEN.windows[TURNS_PER_RUN] == texts[-WINDOW_TURNS:]


async def test_an_older_turn_reads_as_running_after_the_boundary(
    env: WorkflowEnvironment,
):
    """A contract, not a defect, and the client depends on knowing which.

    `continue_as_new` carries the window and not `_outcomes`, so a turn answered
    by the previous run is no longer in this one's memory. The query says
    `running` rather than raising, and the settled turn is in `conversation_turn`
    where the collect path reads it. A client that treated the query as
    authoritative would poll for ever on a turn that was answered, billed and
    written down.
    """
    client: Client = env.client
    texts = [f"pregunta {i}" for i in range(TURNS_PER_RUN)]
    async with Worker(
        client, task_queue=TASK_QUEUE, workflows=[ChatWorkflow], activities=[answers, *ACTS]
    ):
        handle = await client.start_workflow(
            ChatWorkflow.run, start(), id=f"chat-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        await _drain(handle, texts)
        await handle.signal(ChatWorkflow.ask, turn(len(texts) + 1, "después"))
        await answered(len(texts) + 1)

        # Turn 1 was answered by a run that no longer exists. Deliberately not
        # asserting anything about the *last* turn: whether its signal lands
        # before or after the boundary is a race, and a test that pinned one
        # side of it would fail on the other for no reason worth knowing.
        assert (await handle.query(ChatWorkflow.turn, 1)).state == "running"

        await handle.signal(ChatWorkflow.close)
        await handle.result()
