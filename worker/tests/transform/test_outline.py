"""Coverage, the one mechanical guarantee this feature offers.

"Preserve the source's ideas as strictly as possible" is a sentence in a prompt.
*Every source chunk is assigned to at least one target chapter* is a property,
and it is the only thing here that can hold a model to the document it was given.
"""

from __future__ import annotations

import pytest

from brainworker.transform import outline, reading
from brainworker.transform.genres import GENRES
from brainworker.transform.types import (
    MAX_CHAPTER_SOURCE_CHARS,
    MAX_UNCOVERED_FRACTION,
    ChapterPlan,
    SourceSpan,
)


def _passages(n: int = 12, chars: int = 500):
    rows = [
        {"index": i, "kind": "cuerpo", "chapter": f"Cap {i // 3 + 1}", "text": "x" * chars}
        for i in range(n)
    ]
    return reading.passages_of(rows)


def _plan(*spans) -> list[ChapterPlan]:
    return [
        ChapterPlan(
            ordinal=i + 1,
            title=f"Capítulo {i + 1}",
            intent="scope and limits of the subject",
            sources=[SourceSpan(first=a, last=b)],
        )
        for i, (a, b) in enumerate(spans)
    ]


def test_a_faithful_plan_leaving_material_unassigned_is_refused():
    passages = _passages()
    complaints = outline.validate(
        _plan((0, 5)),
        genre=GENRES["essay"],
        mode="faithful",
        chunk_count=len(passages),
        max_chapters=10,
        passages=passages,
    )
    assert any("assigned to no chapter" in c for c in complaints)


def test_an_adaptive_plan_may_condense_but_not_discard():
    passages = _passages(n=20)
    under = outline.validate(
        _plan((0, 15)),
        genre=GENRES["essay"],
        mode="adaptive",
        chunk_count=len(passages),
        max_chapters=10,
        passages=passages,
    )
    assert under == [], "condensing a fifth is what adaptive mode is for"

    over = outline.validate(
        _plan((0, 9)),
        genre=GENRES["essay"],
        mode="adaptive",
        chunk_count=len(passages),
        max_chapters=10,
        passages=passages,
    )
    assert any("this mode allows" in c for c in over)


def test_the_uncovered_fraction_is_reported_exactly():
    passages = _passages(n=20)
    uncovered, fraction = outline.coverage(_plan((0, 14)), len(passages))
    assert uncovered == [15, 16, 17, 18, 19]
    assert fraction == pytest.approx(0.25)
    assert fraction <= MAX_UNCOVERED_FRACTION


def test_a_chapter_larger_than_one_call_can_write_is_refused():
    passages = _passages(n=80, chars=500)
    complaints = outline.validate(
        _plan((0, 79)),
        genre=GENRES["essay"],
        mode="faithful",
        chunk_count=len(passages),
        max_chapters=10,
        passages=passages,
    )
    assert any("more than the" in c and "single chapter" in c for c in complaints)


def test_duplicate_titles_and_empty_sources_are_both_reported_in_one_round():
    """One refine round carries every complaint. Revealing them one at a time
    would spend three paid proposals learning what one could have said."""
    passages = _passages()
    chapters = [
        ChapterPlan(ordinal=1, title="Uno", sources=[SourceSpan(0, 11)]),
        ChapterPlan(ordinal=2, title="uno", sources=[]),
    ]
    complaints = outline.validate(
        chapters,
        genre=GENRES["essay"],
        mode="faithful",
        chunk_count=len(passages),
        max_chapters=10,
        passages=passages,
    )
    assert any("Two chapters are both called" in c for c in complaints)
    assert any("names no source passages" in c for c in complaints)


def test_a_proposal_is_read_defensively_rather_than_trusted():
    """Clamped, not raised on. A span naming chunk 900 of a 600-chunk document
    is an arithmetic slip, and clamping produces a plan the validator can judge;
    raising turns it into a refine round that costs a call."""
    chapters = outline.chapters_from(
        {
            "chapters": [
                {"title": "A", "sources": [{"first": 6, "last": 900}]},
                {"title": "B", "sources": [{"first": 5, "last": 0}]},
                "not a chapter at all",
                {"title": "C", "sources": [{"first": "x", "last": 2}]},
            ]
        },
        chunk_count=12,
    )
    assert [(c.sources[0].first, c.sources[0].last) for c in chapters[:2]] == [
        (6, 11),
        (0, 5),
    ]
    assert chapters[2].title == "C" and chapters[2].sources == []


def test_chapters_from_survives_rubbish():
    assert outline.chapters_from(None, 10) == []
    assert outline.chapters_from({"chapters": "no"}, 10) == []
    assert outline.chapters_from({}, 10) == []


def test_the_fallback_never_leaves_a_chapter_blank():
    """The defect the first production run showed, at the moment it mattered.

    The source was a sermon transcript; `chapters_of` gave it one chapter with
    `title=""`, which is the honest reading of a document whose headings were
    never detected and the state 27 of this corpus's 52 documents are in. The
    fallback returned three chapters called `""`, so the **second gate** — whose
    entire job is to show the outline before anybody pays — displayed three
    blank bullets. The work itself read fine, because each chapter is titled as
    it is *written*, which is after the decision.
    """
    passages = reading.passages_of(
        [{"index": i, "kind": "cuerpo", "chapter": "", "text": "x" * 900}
         for i in range(90)]
    )
    chapters = reading.chapters_of(passages)
    assert [c.title for c in chapters] == [""], "the source really names nothing"

    for language, word in (("es", "Parte"), ("en", "Part")):
        plan = outline.fallback(chapters, passages, max_chapters=10,
                                language=language)
        assert len(plan) > 1
        assert all(c.title for c in plan), "no chapter may reach a gate unnamed"
        assert all(c.title.startswith(word) for c in plan)
        assert len({c.title for c in plan}) == len(plan), "and they are distinct"


def test_an_unknown_language_numbers_the_parts_in_english():
    passages = reading.passages_of(
        [{"index": i, "kind": "cuerpo", "chapter": "", "text": "x" * 900}
         for i in range(60)]
    )
    plan = outline.fallback(reading.chapters_of(passages), passages,
                            max_chapters=10, language="pt")
    assert all(c.title.startswith("Part ") for c in plan)


def test_the_fallback_covers_the_document_by_construction():
    """Not a failure. Rule learning falls back to built-in rules after three
    attempts rather than refusing, because "blocking there would refuse to
    publish documents whose only fault is being ordinary"."""
    passages = _passages(n=12)
    chapters = outline.fallback(
        reading.chapters_of(passages), passages, max_chapters=10
    )
    uncovered, fraction = outline.coverage(chapters, len(passages))
    assert uncovered == []
    assert fraction == 0.0
    assert [c.ordinal for c in chapters] == list(range(1, len(chapters) + 1))


def test_the_fallback_splits_a_chapter_one_call_cannot_write():
    passages = _passages(n=120, chars=600)  # one chapter every 3 -> 1800 chars
    big = reading.chapters_of(passages)
    # Re-group them all into a single enormous source chapter.
    rows = [
        {"index": p.index, "kind": "cuerpo", "chapter": "Uno", "text": p.text}
        for p in passages
    ]
    passages = reading.passages_of(rows)
    chapters = outline.fallback(
        reading.chapters_of(passages), passages, max_chapters=60
    )
    assert len(chapters) > 1
    assert all(c.chars <= MAX_CHAPTER_SOURCE_CHARS for c in chapters)
    assert outline.coverage(chapters, len(passages))[0] == []
    assert big  # the original grouping is untouched by the regroup above


def test_the_fallback_merges_down_to_the_cap_without_breaking_the_order():
    passages = _passages(n=30)
    chapters = outline.fallback(
        reading.chapters_of(passages), passages, max_chapters=3
    )
    assert len(chapters) <= 3
    firsts = [min(s.first for s in c.sources) for c in chapters]
    assert firsts == sorted(firsts)
    assert outline.coverage(chapters, len(passages))[0] == []


def test_a_genre_s_own_checks_run_even_when_the_structure_already_failed():
    passages = _passages()
    complaints = outline.validate(
        [ChapterPlan(ordinal=1, title="", sources=[])],
        genre=GENRES["commentary"],
        mode="faithful",
        chunk_count=len(passages),
        max_chapters=10,
        passages=passages,
    )
    assert any("has no title" in c for c in complaints)
    assert any("comments on nothing" in c for c in complaints)


def test_a_commentary_may_not_run_backwards_through_its_subject():
    passages = _passages(n=12)
    chapters = _plan((6, 11), (0, 5))
    complaints = GENRES["commentary"].validate(chapters)
    assert any("subject's own order" in c for c in complaints)
