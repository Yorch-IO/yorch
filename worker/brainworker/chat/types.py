"""What a conversation looks like on the wire, and what crosses Temporal.

Every dataclass here that reaches a workflow is also a contract with the paid
plane, which hand-builds the same payload in TypeScript. **Temporal's converter
silently ignores a key naming no field** — it does not raise, it does not warn,
and the workflow simply runs with the default. For `top_k` that asks at the
wrong size; for `tenant_id` it is a cross-tenant read with no error anywhere.
`as_json` at the bottom exists so a parity spec on the other side can compare
field for field against this module rather than against a snapshot of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Literal

from ..answering.effort import DEFAULT_EFFORT
from ..answering.types import Answer

#: Prior exchanges the rewrite is allowed to see, and the whole reason this is a
#: *bounded* window rather than a transcript.
#:
#: A Temporal payload caps around 2 MB and workflow history persists to Postgres
#: for the namespace's whole retention period, so a conversation that carried its
#: full transcript in workflow state would grow without bound in the one place
#: this codebase has a standing rule against — the rule `artifacts.py` exists to
#: keep. Six is what a pronoun reaches back through in practice; the full
#: transcript lives in the catalog, which is what a reader loads.
WINDOW_TURNS = 6

#: How much of a prior answer travels in the window. The rewrite needs enough to
#: resolve "ese libro" and "su muerte"; it does not need the citations, the
#: evidence, or the last paragraph. Truncating here is what keeps `WINDOW_TURNS`
#: exchanges comfortably inside one payload.
WINDOW_ANSWER_CHARS = 600

#: A field constraint on both planes rather than a hand-raised error, so both
#: answer an oversized body with FastAPI's own 422-with-a-list — the same
#: decision `Question.effort` and `AnswerStyleUpdate.body` already record.
MAX_MESSAGE_CHARS = 4000

#: Characters of the first question kept as a fallback title, used only until
#: `chat-title` produces a real one and whenever that call fails.
FALLBACK_TITLE_CHARS = 80


@dataclass
class TurnRecord:
    """One prior exchange, as much of it as the rewrite needs."""

    question: str
    #: The answer's prose, truncated to `WINDOW_ANSWER_CHARS`. Never the
    #: citations: the rewrite is resolving references in the *user's* next
    #: sentence, and a chunk id has never been the referent of a pronoun.
    answer: str


@dataclass
class ChatTurn:
    """One question arriving at a live conversation. Crosses Temporal as a signal."""

    conversation_id: str
    #: Assigned by the control plane before the signal is sent, so a retried or
    #: duplicated signal names the turn it already named. It is also the
    #: idempotency key on `conversation_message`, for the same reason `run_event`
    #: keys on `(run_id, seq)` rather than trusting arrival order.
    turn_seq: int
    text: str
    library_id: str
    #: Set by the control plane from the authenticated request. Never by a model,
    #: never from the request body — the same rule as `Question.tenant_id`, which
    #: this is copied into.
    #:
    #: **Required, unlike `Question.tenant_id`, and the difference is deliberate.**
    #: A tenant default on a *field* is the mistake that put a paying
    #: organisation's `Document`, `DocumentVersion`, 5 `Section`s, 13 `Chunk`s and
    #: 13 `Citation`s into the legacy tenant's graph under an unsalted id, with
    #: nothing failing anywhere — four construction sites simply forgot to pass
    #: it, and the default made forgetting invisible. `Question` carries that
    #: default because it predates the rule and both planes set it at every call
    #: site; a dataclass added afterwards has no such excuse. The cost is one
    #: word at each construction site. The benefit is that a site that forgets
    #: raises `TypeError` here instead of quietly reading another organisation's
    #: corpus.
    tenant_id: str
    effort: Literal["brief", "standard", "thorough"] = DEFAULT_EFFORT
    #: The same four narrowings `Question` carries, per turn, copied into the
    #: `Question` the turn asks. Per turn rather than per conversation because
    #: a reader narrows and widens as they go, and a filter that outlived the
    #: turn that set it would be a silent one.
    recorded_from: str = ""
    recorded_to: str = ""
    scripture: str = ""
    source_name: str = ""


@dataclass
class ChatStart:
    """The workflow's own argument: which conversation, and what it remembers.

    `window` is non-empty only on a `continue_as_new` or on a session resumed
    after the previous one went dormant — a conversation whose workflow has aged
    out is restarted from the catalog, which is what makes the transcript outlive
    Temporal's retention.
    """

    conversation_id: str
    library_id: str
    #: Required for the reason `ChatTurn.tenant_id` gives at length.
    tenant_id: str
    window: list[TurnRecord] = field(default_factory=list)
    #: Turns already answered before this run began. The seq of the next turn is
    #: assigned by the control plane, so this is for the audit trail and for
    #: deciding whether the title has been generated, not for numbering.
    answered: int = 0


@dataclass
class TurnResult:
    """What the paid activity hands back to the workflow. Deliberately small.

    The `Answer` itself never travels: it carries up to 48 chunks of evidence
    text, and this workflow — unlike `AskWorkflow`, which ends — keeps a window
    across turns and carries it through `continue_as_new`. A window of full
    answers would put a growing multiple of the corpus into workflow history,
    which is the one place this codebase has a standing rule against.

    So the activity writes the answer to the catalog before it returns, and the
    workflow learns only what it needs: whether the turn worked, what was
    actually searched for, and enough of the prose to resolve the next
    follow-up's pronouns.
    """

    seq: int
    state: str
    searched: str = ""
    #: What the turn cost, for the workflow to book. Carried back rather than
    #: written here so the charge lands through a *retryable* activity: losing
    #: the record of money that was spent is worth a retry, and the paid call
    #: itself is emphatically not.
    spend: list = field(default_factory=list)
    #: The answer's opening, capped at `WINDOW_ANSWER_CHARS`. What the *rewrite*
    #: reads on the next turn — never what a reader reads, which comes from the
    #: catalog.
    head: str = ""
    error: dict[str, str] | None = None


@dataclass
class TurnOutcome:
    """What a poller reads for one turn. Mirrors `AskOutcome`'s shape on purpose.

    A failed turn is a *completed* turn carrying `state: "failed"`, never a
    Temporal failure — the same decision `AskWorkflow` records: the person asked
    something and deserves an answer about it either way, and a red line in the
    namespace for a question somebody can retype helps nobody.
    """

    state: str = "running"
    answer: Answer | None = None
    error: dict[str, str] | None = None
    #: The standalone question that was actually embedded and searched for.
    #:
    #: Shown to the reader, not kept for debugging. A follow-up is answered
    #: against a question the person did not type — that is the whole mechanism —
    #: and an answer that quietly addresses something adjacent to what was asked
    #: is indistinguishable from a bad answer unless the substitution is visible.
    #: Equal to the message itself on a first turn, and whenever the rewrite
    #: declined or failed.
    searched: str = ""


def as_json() -> str:
    """The contract, dumped for the other plane's parity spec.

    Live rather than a checked-in fixture, for the reason
    `queries.parity.spec.ts` records about its own: a snapshot only detects
    drift if something forces it to be refreshed, and nothing does.
    """
    import dataclasses
    import json

    def spec(cls: Any) -> list[dict[str, Any]]:
        """Name, requiredness and default of every field, in declaration order.

        Requiredness is "has neither a default nor a default_factory", which is
        what the converter actually keys on — not whether the annotation is
        Optional.
        """
        out = []
        for f in fields(cls):
            has_default = f.default is not dataclasses.MISSING
            has_factory = f.default_factory is not dataclasses.MISSING
            out.append({
                "name": f.name,
                "required": not (has_default or has_factory),
                "default": f.default if has_default else None,
            })
        return out

    return json.dumps(
        {
            "turn_fields": spec(ChatTurn),
            "start_fields": spec(ChatStart),
            "window_turns": WINDOW_TURNS,
            "window_answer_chars": WINDOW_ANSWER_CHARS,
            "max_message_chars": MAX_MESSAGE_CHARS,
            "fallback_title_chars": FALLBACK_TITLE_CHARS,
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":  # pragma: no cover - the parity spec's entry point
    print(as_json())
