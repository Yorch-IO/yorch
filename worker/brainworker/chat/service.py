"""One conversational turn: rewrite, then the ordinary question path.

Two calls and nothing else, which is the point. Everything that makes an answer
trustworthy — the topicality gate, hybrid retrieval, the graph expansion, the
effort ladder, `_verify` — is reached through `answering.service.ask` with a
standalone question, so a turn in a conversation is measured by every figure
already recorded against a one-shot question. There is no second retrieval path
to keep in step and no second set of guarantees.

Deliberately *not* here: any use of the conversation as context for the
**answer**. The history reaches the rewrite and stops. `Provider.generate`
supports a `history` argument and threading it into `answer.compose` would let
the prose refer back naturally ("como decía antes"), at the cost of a second
copy of the conversation in the most expensive prompt in the product and of
`answer.SYSTEM`'s first rule — answer only from the fragments — competing with a
transcript that is not fragments. The measured pipeline stays the measured
pipeline.
"""

from __future__ import annotations

import logging

from ..answering.service import ask
from ..answering.types import Answer, Question
from ..config import Settings
from ..providers import Provider
from . import rewrite as rewrite_mod
from .types import ChatTurn, TurnRecord

log = logging.getLogger(__name__)


def turn(
    settings: Settings,
    request: ChatTurn,
    window: list[TurnRecord],
    on_delta=None,
    on_stage=None,
) -> tuple[Answer, str]:
    """Answer one message in a conversation. Returns the answer and what was searched.

    `library_id`, `tenant_id` and `effort` are copied from the signal the control
    plane sent and never read from anywhere else — the same rule the one-shot
    path states about `Question.tenant_id`, and it matters more here because a
    conversation is long-lived: a tenant taken from the *first* turn and reused
    would be a boundary that erodes as a session goes on.
    """
    provider = Provider(settings.gemini)

    # Reported only when there is something to report. A first turn makes no
    # rewrite call at all, and announcing a stage that does not happen would be
    # a progress display that lies about the cheap direction.
    if on_stage is not None and window:
        on_stage("rewriting", None)
    searched, spend = rewrite_mod.standalone(provider, window, request.text)

    answer = ask(
        settings,
        Question(
            library_id=request.library_id,
            text=searched,
            tenant_id=request.tenant_id,
            effort=request.effort,
        ),
        on_delta=on_delta,
        on_stage=on_stage,
    )

    # Prepended, not appended: it is the first thing the turn paid for, and the
    # audit ledger renders a turn's charges in the order they were incurred.
    if spend is not None:
        answer.spend = [spend] + answer.spend
    return answer, searched


def window_from(records: list[TurnRecord], keep: int) -> list[TurnRecord]:
    """The last `keep` exchanges, oldest first.

    A function rather than a slice at each call site because both the workflow
    and the activity that reseeds one from the catalog have to agree about which
    end is dropped, and getting that backwards would hand the rewrite the
    beginning of a long conversation instead of the part the pronoun refers to.
    """
    return records[-keep:] if keep > 0 else []
