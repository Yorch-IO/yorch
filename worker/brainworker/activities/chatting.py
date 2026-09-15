"""One conversational turn, as an activity — and the relay that makes it visible.

`activities/asking.py` explains why a question's state lives in a workflow
rather than in an API process's memory: two control planes make "one process
holds it" false by construction. A conversation adds a second problem that
answer does not solve. **A Temporal activity cannot stream to an HTTP response.**
The generation runs here, in the worker; the SSE response is served by whichever
plane the reader is talking to, in another process and possibly another
container. Nothing carries bytes between them.

So the prose is published to `conversation_delta` as it is decoded and the SSE
route reads it back. That is a table rather than a notification because it is
**replayable**: a reader who reloads, or whose connection drops, resumes from
the last chunk they saw. A notification not heard is gone, and the failure it
would produce — an answer that was computed and billed and never seen — is
precisely the one the two-call `/ask` split exists to prevent.

The relay is best-effort at every point, and gives up for the rest of a turn
after its first failure. A catalog that is refusing connections would otherwise
turn each of a dozen flushes into a full connect timeout, so the mechanism that
exists to show the answer sooner would be what delayed it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import config
from ..answering.types import Answer
from ..catalog import Catalog
from ..chat import service as chat_service
from ..chat import title as title_mod
from ..chat.types import (
    WINDOW_ANSWER_CHARS,
    ChatTurn,
    TurnRecord,
    TurnResult,
)
from ..pipeline import Spend
from ..providers import Provider

log = logging.getLogger(__name__)

#: Same short ceiling and unpooled connection as every other bookkeeping write,
#: for the reason `activities/ingest.py` records: a pool retries a refused
#: connection in the background, so a catalog that is merely down becomes a
#: full-timeout stall per statement instead of one immediate error.
RECORD_TIMEOUT = 3.0

#: Characters buffered before the relay writes a row. A row per token would be
#: tens of thousands of inserts for one answer; a row per few dozen characters
#: is a handful, and is well under the interval at which a reader perceives text
#: as arriving rather than appearing.
FLUSH_CHARS = 40

#: …and the ceiling on how long a partial buffer waits. The end of an answer,
#: and any pause the model takes mid-sentence, would otherwise sit unpublished
#: until enough characters followed them.
FLUSH_SECONDS = 0.25


class _Relay:
    """Publishes a turn's progress and prose, and stops trying once it cannot.

    Called from the worker thread `service.turn` runs in, not from the event
    loop — which is why it uses a plain blocking catalog rather than anything
    async, and why it must never raise: an exception here would propagate out of
    `on_delta`, through the provider's streaming loop, and fail a turn that was
    being answered perfectly well.

    **Two kinds of row in one sequence.** Prose is buffered and flushed in
    batches; a stage is published the moment it happens, because arriving when
    it happens is its entire value. `stage` flushes the buffer first so the
    ordered stream never claims a stage began before prose that preceded it.
    """

    def __init__(self, database_url: str, conversation_id: str, turn_seq: int) -> None:
        self._url = database_url
        self._conversation_id = conversation_id
        self._turn_seq = turn_seq
        self._pending: list[str] = []
        self._chars = 0
        self._next_seq = 1
        self._last = time.monotonic()
        self._live = True

    def __call__(self, piece: str) -> None:
        if not self._live or not piece:
            return
        self._pending.append(piece)
        self._chars += len(piece)
        due = self._chars >= FLUSH_CHARS or (
            time.monotonic() - self._last >= FLUSH_SECONDS
        )
        if due:
            self.flush()

    def stage(self, name: str, detail: dict | None = None) -> None:
        """Announce where the turn has got to.

        The reason this exists: streaming prose was measured to cover about 8% of
        a turn's wait — 0.8s of a 10s turn on `brief`, and a `standard` turn on
        the same library took 75s. The other nine tenths were one unchanging
        line, covering the rewrite call, the planning call, retrieval and the
        model's reasoning, so a slow stage and a hung one looked identical.
        """
        self.flush()
        self._write([(self._claim(), name, "", detail)])

    def flush(self) -> None:
        if not self._live or not self._pending:
            return
        text = "".join(self._pending)
        self._pending.clear()
        self._chars = 0
        self._last = time.monotonic()
        self._write([(self._claim(), "token", text, None)])

    def _claim(self) -> int:
        seq, self._next_seq = self._next_seq, self._next_seq + 1
        return seq

    def _write(self, rows: list) -> None:
        if not self._live:
            return
        try:
            with Catalog(
                self._url, pooled=False, timeout=RECORD_TIMEOUT
            ) as catalog:
                catalog.publish(self._conversation_id, self._turn_seq, rows)
        except Exception as e:
            # Once, then never again for this turn. The answer is unaffected: it
            # is written to the turn row when the activity settles it.
            self._live = False
            log.warning(
                "chat relay stopped for %s turn %d: %s",
                self._conversation_id, self._turn_seq, e,
            )


@activity.defn(name="open_chat_run")
async def open_chat_run(run_id: str, turn: ChatTurn, label: str) -> None:
    """Open the catalog row this turn's charges will hang off.

    Required rather than tidy, and for a reason that has already cost this
    product a ledger: `record_cost` takes no tenant and derives one in the INSERT
    from the run the charge belongs to, so **no run row means no cost row**. A
    conversation spends on `chat-rewrite`, `ask-embedding` and `answering`, and
    on `chat-title` once.

    `library_id` and `label` are carried because the import queue and the runs
    list filter on `COALESCE(d.library_id, r.library_id)` and render
    `title ?? label ?? workflow_id` — a chat run has no document, so without
    these it is a row with no shelf and no name.

    Best-effort, like every other bookkeeping write on an interactive path.
    """
    settings = config.load()
    try:
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            catalog.start_run(
                run_id=run_id,
                workflow_id=run_id,
                kind="chat",
                tenant_id=turn.tenant_id,
                library_id=turn.library_id,
                label=label,
            )
    except Exception as e:
        log.warning("could not open the run row for %s: %s", run_id, e)


@activity.defn(name="run_chat_turn")
async def run_chat_turn(turn: ChatTurn, window: list[TurnRecord]) -> TurnResult:
    """Rewrite, retrieve, answer — and write the answer down before returning.

    Not retried by the workflow that calls it. Every attempt is a paid
    generation, the provider adapter already retries the transport failures worth
    retrying, and a second attempt would bill the reader twice for one question.

    **The answer is persisted here rather than handed back.** Two reasons, and
    the second is the load-bearing one. It keeps `TurnResult` small, so a window
    carried across `continue_as_new` never grows with the size of the corpus. And
    it means the thing the reader paid for is durable the moment it exists,
    rather than in flight through a workflow that could still fail — the
    generalisation `CLAUDE.md` records as "anything expensive that lands in the
    stores before its activity returns is not protected by the run's outcome",
    read in the direction where that is the property you want.
    """
    settings = config.load()
    if not settings.gemini.configured:
        raise ApplicationError(
            "Falta BRAIN_GEMINI_PROJECT_ID: no se puede conversar.",
            "provider_unconfigured",
            type="ProviderError",
            non_retryable=True,
        )

    relay = _Relay(settings.database_url, turn.conversation_id, turn.turn_seq)

    def work() -> tuple[Answer, str]:
        try:
            answer, searched = chat_service.turn(
                settings, turn, window, on_delta=relay, on_stage=relay.stage
            )
        finally:
            relay.flush()
        # Written from the same thread, deliberately. `_settle` is a blocking
        # catalog write with a three-second ceiling, and this is the activity
        # that must stay cancellable — doing it on the event loop would be the
        # small version of the mistake that once held the worker for 45 minutes.
        _settle(settings, turn, answer, searched, answer.state)
        return answer, searched

    # `chat_service.turn` is synchronous and network-bound. A thread keeps this
    # activity's own event loop free, which is what lets Temporal heartbeat and
    # cancel it — and the absence of that `to_thread` is what once held the
    # worker's only event loop for 45 minutes and billed a document twice.
    answer, searched = await asyncio.to_thread(work)

    return TurnResult(
        seq=turn.turn_seq,
        state=answer.state,
        searched=searched,
        head=answer.text[:WINDOW_ANSWER_CHARS],
        spend=answer.spend,
    )


def _settle(
    settings: config.Settings,
    turn: ChatTurn,
    answer: Answer,
    searched: str,
    state: str,
) -> None:
    """Write the turn's outcome, then drop its relay rows.

    In that order, never the reverse: a reader still following the stream has to
    be able to reach the end of it, and the deltas are what they are reading.

    Only the evidence a *verified* citation names is stored. A `thorough` turn
    retrieves up to 48 chunks and the model cites a fraction of them; the
    citations panel needs the text of the ones it cited, and the rest is evidence
    for an answer that did not use it.
    """
    cited = {c.chunk_id for c in answer.citations}
    evidence = [asdict(e) for e in answer.evidence if e.chunk_id in cited]

    # A refusal's `reason` is the only thing that says *which* refusal it was,
    # and it was being dropped: `error` went out as `None` for every state, so a
    # reader saw "not enough evidence" and nothing else. That flattens four
    # different facts with four different remedies into one — nothing cleared the
    # dense floor, the model cited nothing verifiable, the search returned no
    # fragment at all, and the model could not compose an envelope. `answer.py`
    # states the rule this restores: "no answer" with nothing to look at is
    # indistinguishable from a broken index.
    #
    # Measured on the turn that found it, on the real corpus 2026-09-06: an
    # answering call spent **65,521 output tokens — the model's whole ceiling —
    # on reasoning, returned no text, and billed $0.497**, twenty times a normal
    # `standard` turn. It surfaced as "Evidencia insuficiente", which is a claim
    # about the corpus the run had no basis for, and the reason that would have
    # named the truncation was thrown away here.
    #
    # It rides in `error` rather than in a column of its own because both clients
    # already render `error.message` under a settled turn, so this needs no
    # migration and no payload change. `kind` is the *state*, which the guidance
    # maps have never heard of and therefore add nothing to — correct, because a
    # refusal is not a failure and must not borrow a failure's advice.
    refusal = (
        {"kind": state, "message": answer.reason}
        if state != "answered" and answer.reason
        else None
    )
    try:
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            catalog.settle_turn(
                turn.conversation_id,
                turn.turn_seq,
                state=state,
                searched=searched,
                answer=answer.text,
                style_effort=answer.style_effort or None,
                citations=[asdict(c) for c in answer.citations],
                cited_evidence=evidence,
                error=refusal,
            )
            catalog.clear_deltas(turn.conversation_id, turn.turn_seq)
    except Exception as e:
        log.warning(
            "could not settle %s turn %d: %s",
            turn.conversation_id, turn.turn_seq, e,
        )


@activity.defn(name="fail_chat_turn")
async def fail_chat_turn(turn: ChatTurn, error: dict[str, str]) -> None:
    """Record that a turn could not be answered.

    A failed turn is a *settled* turn, not an absent one. Leaving the row at
    `running` would make a turn that died indistinguishable from one still being
    answered — the same distinction `run_event`'s one-row-per-transition shape
    exists to keep, and the same reason `AskOutcome` carries `failed` in a 200
    body rather than raising.
    """
    settings = config.load()
    try:
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            catalog.settle_turn(
                turn.conversation_id, turn.turn_seq, state="failed", error=error
            )
            catalog.clear_deltas(turn.conversation_id, turn.turn_seq)
    except Exception as e:
        log.warning("could not record the failure of %s turn %d: %s",
                    turn.conversation_id, turn.turn_seq, e)


@activity.defn(name="name_conversation")
async def name_conversation(turn: ChatTurn, question: str, answer: str) -> list[Spend]:
    """Name a conversation from its first exchange, once.

    Fire-and-forget from the workflow's point of view: a conversation with a
    truncated title is a working conversation, and a turn must never wait on
    this. Returns what it spent so the caller can book it — the charge is small
    and one per conversation, and a stage that spends and reports nothing is
    exactly how the ledger came to hold $0 for every question ever asked.
    """
    settings = config.load()
    if not settings.gemini.configured:
        return []
    try:
        provider = Provider(settings.gemini)
        title, spend = await asyncio.to_thread(
            title_mod.title_for, provider, question, answer
        )
    except Exception as e:
        log.warning("could not name %s: %s", turn.conversation_id, e)
        return []

    if title:
        try:
            with Catalog(
                settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
            ) as catalog:
                catalog.set_conversation_title(
                    turn.conversation_id, title, tenant_id=turn.tenant_id
                )
        except Exception as e:
            log.warning("could not store the name of %s: %s", turn.conversation_id, e)
    return [spend] if spend else []


@activity.defn(name="record_turn_cost")
async def record_turn_cost(run_id: str, state: str, spend: list[Spend]) -> None:
    """Write what a turn spent, and close its run row.

    Four stages can reach here: `chat-rewrite` on a follow-up, `ask-embedding`,
    `answering`, and `chat-title` on the first turn of a conversation.

    `state` maps the turn's outcome onto the run's the same way a question's
    does: `off_corpus` and `insufficient_evidence` are answers about the corpus,
    not failures of the run.
    """
    settings = config.load()
    try:
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            for item in spend:
                catalog.record_cost(
                    run_id,
                    stage=item.stage,
                    provider="vertex",
                    model=item.model,
                    input_tokens=item.input_tokens,
                    output_tokens=item.output_tokens,
                    usd=item.usd,
                )
            catalog.finish_run(run_id, "failed" if state == "failed" else "succeeded")
    except Exception as e:
        log.warning("could not record what %s spent: %s", run_id, e)
