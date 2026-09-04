"""One question, end to end: plan, retrieve, answer.

Deliberately synchronous and not a Temporal workflow. A question is interactive
and short-lived — the user is watching — and durability buys nothing when the
remedy for a failure is to ask again. Ingestion is the opposite case, which is
why it is a workflow and this is not.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..providers import Provider
from . import answer as answer_mod
from . import effort as effort_mod
from . import planner as planner_mod
from .retrieve import OffCorpus, search
from .types import Answer, Question

log = logging.getLogger(__name__)


def ask(settings: Settings, question: Question) -> Answer:
    provider = Provider(settings.gemini)

    plan = planner_mod.plan(provider, question)
    spend = [plan.spend] if plan.spend else []

    try:
        evidence = search(settings, provider, question, plan, spend)
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

    level = effort_mod.effective_style_level(question.effort, len(evidence))
    result = answer_mod.compose(
        provider, question, evidence, plan, style=_style(settings, question, level)
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
