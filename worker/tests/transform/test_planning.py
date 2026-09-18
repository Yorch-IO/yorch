"""The planning graph's loop: propose, judge, refine, and fall back rather than refuse."""

from __future__ import annotations

import asyncio

import pytest

from brainworker.pipeline import Spend
from brainworker.transform import planning, reading
from brainworker.transform.genres import GENRES


def _passages(n=12, chars=500):
    return reading.passages_of(
        [{"index": i, "kind": "cuerpo", "chapter": f"Cap {i // 3 + 1}", "text": "x" * chars}
         for i in range(n)]
    )


def _deps(passages, **over):
    base = dict(
        provider=object(),
        genre=GENRES["treatise"],
        mode="faithful",
        purposes=["context", "verification"],
        passages=passages,
        source_chapters=reading.chapters_of(passages),
        excerpt=reading.excerpt(passages),
        document_title="El Documento",
        target_chapters=2,
        max_chapters=4,
        supported=40,
    )
    base.update(over)
    return planning.PlanDeps(**base)


def _stub(monkeypatch, proposals, genre="lecture transcript", fail_detect=False):
    """Serve one proposal per call to the plan stage; repeat the last."""
    calls: list[str] = []

    async def fake(provider, prompt, *, system, schema, stage, max_output_tokens=0):
        calls.append(stage)
        spend = Spend(stage=stage, model="m", input_tokens=10, output_tokens=5, usd=0.01)
        if stage == planning.STAGE_GENRE:
            if fail_detect:
                raise RuntimeError("the classifier is down")
            return {"genre": genre, "confidence": 0.7}, spend
        n = sum(1 for c in calls if c == planning.STAGE_PLAN)
        return proposals[min(n - 1, len(proposals) - 1)], spend

    monkeypatch.setattr(planning, "generate_json", fake)
    return calls


def _proposal(*spans, title="Tratado de Prueba", intent="scope and limits"):
    return {
        "title": title,
        "chapters": [
            {"title": f"Capítulo {i + 1}", "intent": intent,
             "sources": [{"first": a, "last": b}]}
            for i, (a, b) in enumerate(spans)
        ],
    }


def test_a_validated_proposal_is_adopted_on_the_first_try(monkeypatch):
    passages = _passages()
    calls = _stub(monkeypatch, [_proposal((0, 5), (6, 11))])
    plan = asyncio.run(planning.plan(_deps(passages)))
    assert calls.count(planning.STAGE_PLAN) == 1
    assert plan.attempts == 1
    assert plan.fallback is False
    assert plan.uncovered == []
    assert plan.title == "Tratado de Prueba"


def test_a_refused_proposal_is_refined_and_the_complaints_are_fed_back(monkeypatch):
    passages = _passages()
    prompts: list[str] = []

    async def fake(provider, prompt, *, system, schema, stage, max_output_tokens=0):
        prompts.append(prompt)
        spend = Spend(stage=stage, model="m", input_tokens=1, output_tokens=1, usd=0.01)
        if stage == planning.STAGE_GENRE:
            return {"genre": "x", "confidence": 0.5}, spend
        n = sum(1 for p in prompts if "The source document's own divisions" in p)
        return (_proposal((0, 3)) if n == 1 else _proposal((0, 5), (6, 11))), spend

    monkeypatch.setattr(planning, "generate_json", fake)
    plan = asyncio.run(planning.plan(_deps(passages)))
    assert plan.attempts == 2
    assert plan.fallback is False
    assert any("Your previous outline was refused" in p for p in prompts)
    assert any("assigned to no chapter" in p for p in prompts)


def test_after_three_attempts_the_source_s_own_chapters_are_adopted(monkeypatch):
    """Not a failure. Blocking here would refuse to transform a document whose
    only fault is being ordinary — the argument `n_fallback_rules` already makes.
    """
    passages = _passages()
    calls = _stub(monkeypatch, [_proposal((0, 1))])  # never covers the document
    plan = asyncio.run(planning.plan(_deps(passages)))
    assert calls.count(planning.STAGE_PLAN) == planning.MAX_REFINE
    assert plan.fallback is True
    assert plan.uncovered == [], "the fallback covers the document by construction"
    assert plan.notes, "a fallback records why it was needed"


def test_the_fallback_falls_back_to_the_document_s_title(monkeypatch):
    passages = _passages()
    _stub(monkeypatch, [{"title": "", "chapters": [{"title": "A", "intent": "", "sources": []}]}])
    plan = asyncio.run(planning.plan(_deps(passages)))
    assert plan.fallback is True
    assert plan.title == "El Documento"


def test_a_failed_classification_does_not_stop_the_planning(monkeypatch):
    """An unknown source genre costs the proposal one piece of context; refusing
    to transform because a classification call failed is the expensive answer to
    a cheap problem."""
    passages = _passages()
    _stub(monkeypatch, [_proposal((0, 5), (6, 11))], fail_detect=True)
    plan = asyncio.run(planning.plan(_deps(passages)))
    assert plan.source_genre == ""
    assert plan.fallback is False
    assert len(plan.chapters) == 2


def test_a_refused_proposal_call_goes_straight_to_the_fallback(monkeypatch):
    passages = _passages()

    async def fake(provider, prompt, *, system, schema, stage, max_output_tokens=0):
        spend = Spend(stage=stage, model="m", input_tokens=1, output_tokens=1, usd=0.01)
        if stage == planning.STAGE_GENRE:
            return {"genre": "x", "confidence": 0.5}, spend
        raise RuntimeError("the model returned nothing usable")

    monkeypatch.setattr(planning, "generate_json", fake)
    plan = asyncio.run(planning.plan(_deps(passages)))
    assert plan.fallback is True
    assert plan.chapters


def test_the_detected_genre_is_recorded_and_reaches_no_report(monkeypatch):
    """The brief asks that the analysis be silent, not that it be unrecorded: a
    reading whose instrument is unrecorded cannot be compared with the next one.
    """
    passages = _passages()
    _stub(monkeypatch, [_proposal((0, 5), (6, 11))], genre="lecture transcript")
    plan = asyncio.run(planning.plan(_deps(passages)))
    assert plan.source_genre == "lecture transcript"
    assert plan.source_genre_confidence == pytest.approx(0.7)


def test_every_call_s_spend_is_carried_on_the_plan(monkeypatch):
    passages = _passages()
    _stub(monkeypatch, [_proposal((0, 1))])  # forces three proposals
    plan = asyncio.run(planning.plan(_deps(passages)))
    assert len(plan.spend) == 1 + planning.MAX_REFINE


def test_the_budget_comes_off_the_probe_and_the_chapter_count(monkeypatch):
    passages = _passages()
    _stub(monkeypatch, [_proposal((0, 5), (6, 11))])
    rich = asyncio.run(planning.plan(_deps(passages, supported=400)))
    poor = asyncio.run(planning.plan(_deps(passages, supported=0)))
    assert poor.budget == 0
    assert rich.budget > 0


def test_the_chapters_are_renumbered_and_measured(monkeypatch):
    passages = _passages()
    _stub(monkeypatch, [_proposal((0, 5), (6, 11))])
    plan = asyncio.run(planning.plan(_deps(passages)))
    assert [c.ordinal for c in plan.chapters] == [1, 2]
    assert all(c.chars > 0 for c in plan.chapters)
