"""One question, end to end: plan, retrieve, answer.

Deliberately synchronous and not a Temporal workflow. A question is interactive
and short-lived — the user is watching — and durability buys nothing when the
remedy for a failure is to ask again. Ingestion is the opposite case, which is
why it is a workflow and this is not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..config import Settings
from ..providers import Provider
from . import answer as answer_mod
from . import effort as effort_mod
from . import planner as planner_mod
from .retrieve import OffCorpus, search
from .types import Answer, Question

log = logging.getLogger(__name__)


def ask(
    settings: Settings,
    question: Question,
    on_delta: Callable[[str], None] | None = None,
    on_stage: Callable[..., None] | None = None,
) -> Answer:
    """Plan, retrieve, answer.

    `on_delta` is forwarded to `answer.compose` and does not reach the two stages
    before it. That is not an omission: planning and retrieval produce no prose,
    they produce a template id and a list of chunks, and there is nothing about
    either that a reader could be shown a character at a time.

    `on_stage` is what covers them, and it is the more useful of the two.
    Measured 2026-09-06: streamed prose is about 8% of a turn's wait — 0.8s of a
    10s turn on `brief`, and 75s for a `standard` turn on the same library — and
    the rest was one unchanging line over four things that can each take seconds.
    It is called with a stage name, and `retrieving` carries the two counts
    below.

    **`evidence` reports the dense-floor count, which was already being computed
    and discarded.** `search` runs the topicality probe at full width and returns
    how many cleared `MIN_SCORE`; that number is what tells a narrow question
    from a well-supported one, and `effective_style_level` already steps the
    style down on it. Reporting it costs nothing and no tokens.

    Note the ordering both callbacks imply: an `off_corpus` question returns
    *before* `compose` is reached, so `on_delta` is never called at all and the
    first thing the caller learns after `retrieving` is the finished refusal.
    """
    def stage(name: str, **detail: object) -> None:
        # Never allowed to fail the question it is describing.
        if on_stage is None:
            return
        try:
            on_stage(name, detail or None)
        except Exception as e:  # noqa: BLE001 - progress is not the product
            log.warning("stage report %r failed: %s", name, e)

    provider = Provider(settings.gemini)

    stage("planning")
    plan = planner_mod.plan(provider, question)
    spend = [plan.spend] if plan.spend else []

    stage("retrieving")
    try:
        supported: list[int] = []
        evidence = search(settings, provider, question, plan, spend, supported)
    except OffCorpus as e:
        # Told apart from "not enough evidence" because the remedy differs: this
        # question belongs to a different corpus, not to a gap in this one.
        return Answer(
            state="off_corpus",
            reason=(
                "ningún fragmento de esta biblioteca supera el umbral de "
                "similitud: la pregunta parece ser sobre otra materia"
            ),
            evidence=e.nearby,
            plan=plan,
            spend=spend,
            effort=question.effort,
            # Off-corpus returns before any style is chosen, and it is the one
            # path that used to leave this unset — the assignment below is
            # unreachable from here. `brief` because that is what three nearby
            # fragments would have justified anyway.
            style_effort=effort_mod.EFFORT_LEVELS[0],
        )

    # Both counts, because they answer different questions: how much reached the
    # prompt, and how much of it cleared the dense floor. The second is what
    # distinguishes a narrow question from one the corpus supports well, and it
    # is the number `effective_style_level` steps the style down on.
    stage(
        "evidence",
        chunks=len(evidence),
        dense=supported[0] if supported else None,
    )
    level = effort_mod.effective_style_level(
        question.effort, len(evidence), supported[0] if supported else None
    )
    stage("generating")
    result = answer_mod.compose(
        provider, question, evidence, plan,
        style=_style(settings, question, level),
        on_delta=on_delta,
    )
    result.spend = spend + result.spend
    # The level the *style* used, which is the level asked for unless the corpus
    # supplied too little to justify it. Recorded because a `thorough` question
    # that comes back terse is otherwise indistinguishable from the control not
    # working, and the honest answer is "there were only five fragments".
    result.style_effort = level
    return result


def _style(settings: Settings, question: Question, level: str) -> str:
    """This organisation's wording for a level, or the built-in default.

    Best-effort against the catalog, like every other bookkeeping read on this
    path: a question must not fail because the catalog is down, and falling back
    to the default only changes how the answer is worded. Unpooled for the
    recorded reason — a pool retries a refused connection in the background, so
    a catalog that is merely down would turn this into a full-timeout stall in
    front of somebody waiting for an answer.
    """
    default = effort_mod.budget_for(level).style
    try:
        from ..catalog import Catalog

        with Catalog(settings.database_url, pooled=False) as catalog:
            return catalog.answer_styles(tenant_id=question.tenant_id).get(
                level, default
            )
    except Exception as e:
        log.warning("answer style unavailable, using the default: %s", e)
        return default
