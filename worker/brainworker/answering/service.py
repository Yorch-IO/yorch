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
from . import planner as planner_mod
from .retrieve import OffCorpus, search
from .types import Answer, Question

log = logging.getLogger(__name__)


def ask(settings: Settings, question: Question) -> Answer:
    provider = Provider(settings.gemini)

    plan = planner_mod.plan(provider, question)
    spend = [plan.spend] if plan.spend else []

    try:
        evidence = search(settings, provider, question, plan)
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
        )

    result = answer_mod.compose(provider, question, evidence, plan)
    result.spend = spend + result.spend
    return result
