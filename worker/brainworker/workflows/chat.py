"""A conversation, as a session over a record that outlives it.

`AskWorkflow` holds one question and ends. That is right for a question — the
remedy for losing one is to retype it — and wrong for a conversation, which is a
thing people come back to. But making the workflow *be* the conversation would
put the transcript in workflow history, which persists to Postgres for the
namespace's whole retention period and caps around 2 MB per payload; and it
would make the conversation die when that retention expires, which is exactly
what `ask.service.ts` already reports as `question_not_found`.

So the division is: **Temporal owns the session, the catalog owns the record.**

* The workflow carries a *bounded window* of recent turns, because that is what
  the next rewrite needs and nothing more, and `continue_as_new` keeps even that
  from growing.
* Every answer is written to `conversation_turn` by the activity that produced
  it, before it returns.
* When a conversation goes quiet the workflow **ends normally**. It is dormant,
  not failed. The next turn starts a fresh session seeded from the catalog, so
  nothing is lost when a workflow ages out of the namespace.

Entered with signal-with-start, so `POST /chat/{id}/turn` is one call that
starts the session or signals the live one, and the caller does not have to know
which.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from ..activities.chatting import (
        fail_chat_turn,
        name_conversation,
        open_chat_run,
        record_turn_cost,
        run_chat_turn,
    )
    from ..chat.types import (
        WINDOW_TURNS,
        ChatStart,
        ChatTurn,
        TurnOutcome,
        TurnRecord,
        TurnResult,
    )

#: Generous for the same reason `ASK_TIMEOUT` is: a real question against the
#: church-history library once outran a 180-second budget, was computed, was
#: billed, and was discarded. A conversational turn adds a rewrite call in front
#: of that.
TURN_TIMEOUT = timedelta(minutes=15)

#: Bookkeeping is local and small; three attempts, because losing the record of
#: a charge that was made is worth one retry.
BOOK_TIMEOUT = timedelta(minutes=2)
_BOOK_RETRY = RetryPolicy(maximum_attempts=3)

#: Naming is one cheap call and nothing waits on it, so it may be retried.
NAME_TIMEOUT = timedelta(minutes=2)

#: How long a session waits for the next turn before going dormant.
#:
#: Not a limit on the conversation — the record is in Postgres and the next turn
#: starts a new session from it. This bounds how many idle workflows sit in the
#: namespace, which is the resource an abandoned conversation actually consumes.
#: An hour is longer than a reading pause and much shorter than a working day.
IDLE_TIMEOUT = timedelta(hours=1)

#: Turns answered in one run before continuing as new. Well under anything
#: Temporal would complain about; the point is that history is bounded by a
#: number this file states rather than by how long somebody keeps talking.
TURNS_PER_RUN = 25


@workflow.defn(name="ChatWorkflow")
class ChatWorkflow:
    def __init__(self) -> None:
        self._pending: list[ChatTurn] = []
        self._outcomes: dict[int, TurnOutcome] = {}
        self._window: list[TurnRecord] = []
        self._answered = 0
        self._named = False
        self._closed = False

    # -- what the control planes send and read -----------------------------

    @workflow.signal
    def ask(self, turn: ChatTurn) -> None:
        """Queue a turn. Also the start signal, so this is the whole entry point.

        Idempotent on `turn_seq`: the sequence number is claimed in Postgres
        before the signal is sent, so a duplicated delivery names a turn that is
        already queued or already answered and is dropped rather than asked and
        billed twice.
        """
        if turn.turn_seq in self._outcomes:
            return
        self._outcomes[turn.turn_seq] = TurnOutcome(state="running")
        self._pending.append(turn)

    @workflow.signal
    def close(self) -> None:
        """End the session now rather than at the idle timeout."""
        self._closed = True

    @workflow.query
    def turn(self, turn_seq: int) -> TurnOutcome:
        """Answerable from the moment the workflow exists, which is the point.

        A turn this session has never heard of comes back `running` rather than
        raising: after a `continue_as_new` or a resumed session the outcome of an
        older turn is in the catalog, and the collect path reads it there. An
        error here would turn "ask somewhere else" into "this failed".
        """
        return self._outcomes.get(turn_seq) or TurnOutcome(state="running")

    @workflow.query
    def transcript(self) -> list[TurnRecord]:
        """The window this session is carrying — never the whole conversation."""
        return self._window

    # -- the session -------------------------------------------------------

    @workflow.run
    async def run(self, start: ChatStart) -> None:
        self._window = list(start.window)
        self._answered = start.answered
        self._named = start.answered > 0

        while True:
            try:
                await workflow.wait_condition(
                    lambda: bool(self._pending) or self._closed,
                    timeout=IDLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # Dormant, not failed. The record is in the catalog and the next
                # turn will start a new session from it.
                return

            while self._pending:
                await self._answer(self._pending.pop(0))

            if self._closed:
                return

            if self._answered >= TURNS_PER_RUN or workflow.info().is_continue_as_new_suggested():
                # Reached only with an empty queue, and that is what makes it
                # safe: `continue_as_new` carries the window and **not**
                # `_pending`, so a turn still queued here would be dropped after
                # the reader had been told it was accepted. The loop above exits
                # only when `_pending` is empty, and no `await` separates the two
                # — so no signal can arrive in between. The consequence is that a
                # long burst runs past `TURNS_PER_RUN` rather than splitting: the
                # bound is "this many turns, then the first quiet moment", which
                # is the safe direction to be approximate in.
                workflow.continue_as_new(
                    ChatStart(
                        conversation_id=start.conversation_id,
                        library_id=start.library_id,
                        tenant_id=start.tenant_id,
                        window=self._window,
                        answered=self._answered,
                    )
                )

    async def _answer(self, turn: ChatTurn) -> None:
        run_id = f"{workflow.info().workflow_id}-t{turn.turn_seq}"

        # One run row per *turn*, not per conversation: `record_cost` derives its
        # tenant from the run a charge hangs off, and a conversation-wide row
        # would make "what did this turn cost" unanswerable while making the
        # runs list show one endless row.
        await workflow.execute_activity(
            open_chat_run,
            args=[run_id, turn, _label(turn)],
            start_to_close_timeout=BOOK_TIMEOUT,
            retry_policy=_BOOK_RETRY,
        )

        try:
            result = await workflow.execute_activity(
                run_chat_turn,
                args=[turn, self._window],
                start_to_close_timeout=TURN_TIMEOUT,
                # One attempt. Every one is a paid generation.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as e:
            await self._fail(run_id, turn, e)
            return

        self._outcomes[turn.turn_seq] = TurnOutcome(
            state="done", searched=result.searched
        )
        self._remember(turn, result)

        spend = list(result.spend)
        if not self._named and result.state != "failed":
            spend += await self._name(turn, result)

        await self._book(run_id, result.state, spend)

    def _remember(self, turn: ChatTurn, result: TurnResult) -> None:
        """Add the exchange to the window, and drop the oldest beyond it.

        Trimmed here rather than where it is read, so the *payload* carried
        through `continue_as_new` is bounded — trimming at the read would keep
        the whole conversation in history and only hide it from the prompt.
        """
        self._window.append(TurnRecord(question=turn.text, answer=result.head))
        del self._window[:-WINDOW_TURNS]
        self._answered += 1

    async def _name(self, turn: ChatTurn, result: TurnResult) -> list:
        """Name the conversation from its first exchange, once.

        `_named` is set whatever happens, including on failure: retrying on every
        subsequent turn of a long conversation would pay repeatedly for something
        the fallback title already covers.
        """
        self._named = True
        try:
            return await workflow.execute_activity(
                name_conversation,
                args=[turn, turn.text, result.head],
                start_to_close_timeout=NAME_TIMEOUT,
                retry_policy=_BOOK_RETRY,
            )
        except ActivityError:
            return []

    async def _fail(self, run_id: str, turn: ChatTurn, e: ActivityError) -> None:
        """Record a turn that could not be answered, and keep the session alive.

        A failed turn is not a failed conversation. The reader can ask again, and
        the workflow that ended over one refused generation would take the
        session's window with it.
        """
        cause = e.cause
        error = {"kind": "chat_failed", "message": str(cause or e)}
        if isinstance(cause, ApplicationError) and cause.details:
            error = {"kind": str(cause.details[0]), "message": cause.message}

        self._outcomes[turn.turn_seq] = TurnOutcome(state="failed", error=error)
        await workflow.execute_activity(
            fail_chat_turn,
            args=[turn, error],
            start_to_close_timeout=BOOK_TIMEOUT,
            retry_policy=_BOOK_RETRY,
        )
        # A turn that failed still spent whatever it spent before it did — the
        # rewrite runs first and is a paid call — but the activity raised rather
        # than returned, so there is nothing to book. Same gap `AskWorkflow`
        # records at the same point.
        await self._book(run_id, "failed", [])

    async def _book(self, run_id: str, state: str, spend: list) -> None:
        """Write the bill. Never allowed to change the outcome."""
        await workflow.execute_activity(
            record_turn_cost,
            args=[run_id, state, spend],
            start_to_close_timeout=BOOK_TIMEOUT,
            retry_policy=_BOOK_RETRY,
        )


def _label(turn: ChatTurn) -> str:
    """What the runs list calls this turn.

    A chat run has no document, so `title ?? label ?? workflow_id` would fall
    through to a workflow id without one. The question itself is what a person
    scanning the list is looking for.
    """
    text = " ".join(turn.text.split())
    return text if len(text) <= 80 else text[:79].rstrip() + "…"
