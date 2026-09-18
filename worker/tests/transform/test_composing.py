"""The composition graph: what it keeps, what it drops, and what it records.

The two properties that matter are the ones a reader of the finished work
cannot check for themselves: that a paragraph resting only on a citation which
did not verify does **not** reach the page, and that it is not lost either.
"""

from __future__ import annotations

import asyncio

import pytest

from brainworker.answering.types import Evidence
from brainworker.pipeline import Spend
from brainworker.transform import composing
from brainworker.transform import research as research_mod
from brainworker.transform.genres import GENRES
from brainworker.transform.types import (
    ChapterPlan,
    Continuity,
    SourceSpan,
    TransformPlan,
)

EVIDENCE = [
    Evidence(
        chunk_id="chk_real",
        version_id="v_other",
        document_id="d_other",
        title="Otra Obra",
        breadcrumb="Cap 3",
        text="Lo que dice la otra obra.",
        kind="cuerpo",
        score=0.8,
        locator="Otra Obra · cap. 3",
    )
]


def _deps(**over):
    plan = TransformPlan(
        genre="essay",
        mode="faithful",
        chapters=[
            ChapterPlan(ordinal=1, title="Capítulo Uno", intent="abre",
                        sources=[SourceSpan(0, 3)])
        ],
    )
    base = dict(
        settings=object(),
        provider=object(),
        genre=GENRES["essay"],
        mode="faithful",
        plan=plan,
        chapter=plan.chapters[0],
        source_text="Texto fuente del capítulo.",
        incoming=Continuity(),
        purposes=["context"],
        references=[],
        allowance=3,
        library_id="lib",
        tenant_id="t",
        version_id="v_self",
    )
    base.update(over)
    return composing.ChapterDeps(**base)


def _stub_research(monkeypatch, evidence=EVIDENCE, queries=2):
    monkeypatch.setattr(
        research_mod,
        "gather",
        lambda settings, provider, **kw: research_mod.Research(
            evidence=list(evidence), queries_made=queries, supported=9
        ),
    )


def _stub_drafts(monkeypatch, drafts: list[dict]):
    """Serve one prepared envelope per call, then repeat the last."""
    seen: list[str] = []

    async def fake(provider, prompt, *, system, schema, stage, max_output_tokens=0):
        seen.append(prompt)
        raw = drafts[min(len(seen) - 1, len(drafts) - 1)]
        return raw, Spend(stage=stage, model="m", input_tokens=10, output_tokens=5, usd=0.02)

    monkeypatch.setattr(composing, "generate_json", fake)
    return seen


def _draft(paragraphs, citations):
    return {
        "title": "Capítulo Uno",
        "paragraphs": paragraphs,
        "terms": [{"term": "gracia", "form": "la gracia"}],
        "threads": ["queda abierta la cuestión"],
        "citations": citations,
    }


def test_a_paragraph_whose_every_citation_failed_does_not_reach_the_page(monkeypatch):
    _stub_research(monkeypatch)
    _stub_drafts(monkeypatch, [
        _draft(
            [
                {"text": "Del documento fuente.", "chunk_ids": []},
                {"text": "Apoyado en la biblioteca.", "chunk_ids": ["chk_real"]},
                {"text": "INVENTADO.", "chunk_ids": ["chk_nunca_visto"]},
            ],
            [
                {"chunk_id": "chk_real", "supports": "lo que dice la otra obra"},
                {"chunk_id": "chk_nunca_visto", "supports": "nada"},
            ],
        )
    ])
    out = asyncio.run(composing.compose(_deps()))
    assert "INVENTADO." not in out.chapter.body
    assert "Del documento fuente." in out.chapter.body
    assert "Apoyado en la biblioteca." in out.chapter.body


def test_a_dropped_paragraph_is_recorded_rather_than_lost(monkeypatch):
    """Removed, because unlike a synthesis a novel has nowhere to demote a claim
    to; kept, because a dropped claim that leaves the work quietly shorter is its
    own failure and a reader of the report must be able to see what the model
    wanted to say and could not support."""
    _stub_research(monkeypatch)
    _stub_drafts(monkeypatch, [
        _draft(
            [{"text": "INVENTADO.", "chunk_ids": ["chk_nunca"]}],
            [{"chunk_id": "chk_nunca", "supports": "nada"}],
        )
    ])
    out = asyncio.run(composing.compose(_deps()))
    assert "INVENTADO." in out.removed
    assert out.invented >= 1


def test_a_citation_without_a_locator_does_not_survive(monkeypatch):
    """`answer._verify`'s rule, reused rather than reimplemented: a citation the
    user cannot open is not a citation."""
    no_locator = [
        Evidence(chunk_id="chk_real", version_id="v_other", document_id="d",
                 title="Obra", breadcrumb="", text="t", kind="cuerpo", score=0.8,
                 locator="")
    ]
    _stub_research(monkeypatch, evidence=no_locator)
    _stub_drafts(monkeypatch, [
        _draft(
            [{"text": "Apoyado.", "chunk_ids": ["chk_real"]}],
            [{"chunk_id": "chk_real", "supports": "algo"}],
        )
    ])
    out = asyncio.run(composing.compose(_deps()))
    assert out.chapter.cited == []
    assert "Apoyado." in out.removed


def test_a_heavily_unsupported_draft_is_written_again_once(monkeypatch):
    _stub_research(monkeypatch)
    bad = _draft(
        [{"text": f"Inventado {i}.", "chunk_ids": ["chk_nunca"]} for i in range(4)]
        + [{"text": "Bueno.", "chunk_ids": []}],
        [{"chunk_id": "chk_nunca", "supports": "nada"}],
    )
    good = _draft(
        [{"text": "Del documento.", "chunk_ids": []},
         {"text": "De la biblioteca.", "chunk_ids": ["chk_real"]}],
        [{"chunk_id": "chk_real", "supports": "algo"}],
    )
    seen = _stub_drafts(monkeypatch, [bad, good])
    out = asyncio.run(composing.compose(_deps()))
    assert len(seen) == 2, "one revision, and only one"
    assert out.revisions == 1
    assert "Inventado 0." not in out.chapter.body


def test_the_revision_is_capped_at_one_however_bad_the_draft_is(monkeypatch):
    _stub_research(monkeypatch)
    bad = _draft(
        [{"text": f"Inventado {i}.", "chunk_ids": ["chk_nunca"]} for i in range(5)],
        [{"chunk_id": "chk_nunca", "supports": "nada"}],
    )
    seen = _stub_drafts(monkeypatch, [bad])
    out = asyncio.run(composing.compose(_deps()))
    assert len(seen) == 2
    assert out.revisions == composing.MAX_REVISIONS


def test_the_invention_count_accumulates_across_drafts(monkeypatch):
    """A chapter whose first draft invented three citations and whose second did
    not still had a model invent three. `_last` here would report a corrected
    run as a clean one."""
    _stub_research(monkeypatch)
    bad = _draft(
        [{"text": f"Inventado {i}.", "chunk_ids": [f"chk_z{i}"]} for i in range(3)]
        + [{"text": "Bueno.", "chunk_ids": []}],
        [{"chunk_id": f"chk_z{i}", "supports": "nada"} for i in range(3)],
    )
    good = _draft([{"text": "Bueno.", "chunk_ids": []}], [])
    _stub_drafts(monkeypatch, [bad, good])
    out = asyncio.run(composing.compose(_deps()))
    assert out.invented == 3
    assert len(out.removed) == 3
    assert out.chapter.body == "Bueno."


def test_every_call_s_spend_is_kept_and_not_only_the_last(monkeypatch):
    """The reducer, asserted. Merging the graph's `updates` deltas by hand would
    leave `spend` holding one node's rows — a ledger that under-reports with
    nothing failing anywhere."""
    _stub_research(monkeypatch)
    bad = _draft(
        [{"text": f"Inventado {i}.", "chunk_ids": ["chk_nunca"]} for i in range(4)],
        [{"chunk_id": "chk_nunca", "supports": "nada"}],
    )
    good = _draft([{"text": "Bueno.", "chunk_ids": []}], [])
    _stub_drafts(monkeypatch, [bad, good])
    out = asyncio.run(composing.compose(_deps()))
    assert len([s for s in out.spend if s.stage == composing.STAGE_COMPOSE]) == 2


def test_no_allowance_composes_from_the_source_alone(monkeypatch):
    """The correct behaviour, not a degraded one."""
    called: list[int] = []
    monkeypatch.setattr(
        research_mod,
        "gather",
        lambda *a, **k: called.append(1) or research_mod.Research(),
    )
    _stub_drafts(monkeypatch, [_draft([{"text": "Solo la fuente.", "chunk_ids": []}], [])])
    out = asyncio.run(composing.compose(_deps(allowance=0)))
    assert called == []
    assert out.queries_made == 0
    assert out.chapter.body == "Solo la fuente."


def test_the_continuity_carries_the_tail_the_terms_and_the_citations(monkeypatch):
    _stub_research(monkeypatch)
    _stub_drafts(monkeypatch, [
        _draft(
            [{"text": "Primero." * 200, "chunk_ids": ["chk_real"]}],
            [{"chunk_id": "chk_real", "supports": "algo"}],
        )
    ])
    out = asyncio.run(composing.compose(_deps()))
    assert out.continuity.tail.endswith("Primero.")
    assert out.continuity.glossary == {"gracia": "la gracia"}
    assert out.continuity.threads == ["queda abierta la cuestión"]
    assert out.continuity.cited == ["chk_real"]
    assert out.continuity.established[-1].startswith("1. Capítulo Uno")


def test_a_chunk_id_written_into_the_prose_is_stripped(monkeypatch):
    """`answer._clean`, reused. The prompt forbids it and mostly complies, and
    "mostly" is not a property a reader-facing string can rest on."""
    _stub_research(monkeypatch)
    _stub_drafts(monkeypatch, [
        _draft(
            [{"text": "Una frase [chk_2bf13bd98fd6091aeb6e9ce2].", "chunk_ids": []}],
            [],
        )
    ])
    out = asyncio.run(composing.compose(_deps()))
    assert "chk_" not in out.chapter.body
    assert out.chapter.body.startswith("Una frase")


def test_only_citations_a_surviving_paragraph_rests_on_reach_the_bibliography(monkeypatch):
    """A citation listed for a paragraph that was then removed describes nothing
    in the finished work, and printing it would put a source in the closing
    chapter the work never uses."""
    two = EVIDENCE + [
        Evidence(chunk_id="chk_second", version_id="v_other", document_id="d",
                 title="Tercera Obra", breadcrumb="", text="t", kind="cuerpo",
                 score=0.7, locator="Tercera Obra · p. 4")
    ]
    _stub_research(monkeypatch, evidence=two)
    _stub_drafts(monkeypatch, [
        _draft(
            [
                {"text": "Se queda.", "chunk_ids": ["chk_real"]},
                {"text": "Se va.", "chunk_ids": ["chk_inventado"]},
            ],
            [
                {"chunk_id": "chk_real", "supports": "a"},
                {"chunk_id": "chk_second", "supports": "b"},
                {"chunk_id": "chk_inventado", "supports": "c"},
            ],
        )
    ])
    out = asyncio.run(composing.compose(_deps()))
    assert [c.chunk_id for c in out.chapter.cited] == ["chk_real"]
