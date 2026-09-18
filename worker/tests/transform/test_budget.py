"""The research budget: derived from relevance, and spent without overrunning.

The brief asks for a query limit calculated dynamically from relevance and
forbids a second relevance mechanism. There is no need for one: `supported` — the
count of chunks clearing `retrieve.MIN_SCORE` — is the number, and it is the same
signal `effort.effective_style_level` already uses to tell a narrow question from
a broad one.
"""

from __future__ import annotations

import pytest

from brainworker.transform import budget, reading
from brainworker.transform.types import PURPOSES

ALL = list(PURPOSES)


def _passages(n: int = 60):
    rows = [
        {"index": i, "kind": "cuerpo", "chapter": f"Cap {i // 5 + 1}", "text": f"Texto {i}. " * 60}
        for i in range(n)
    ]
    return reading.passages_of(rows)


def test_a_library_with_nothing_to_say_earns_no_queries():
    """Zero is a real answer and is not rounded up to a courtesy query.

    `OffCorpus` read at corpus scale: asking anyway would spend money to be told
    so once per chapter.
    """
    assert budget.budget_for(0, ALL, 10) == 0


def test_no_purposes_means_no_research_whatever_the_library_holds():
    assert budget.budget_for(5_000, [], 10) == 0


def test_the_budget_rises_with_what_the_library_supports():
    thin = budget.budget_for(16, ALL, 40)
    rich = budget.budget_for(400, ALL, 40)
    assert thin < rich


def test_the_cap_is_against_the_work_being_written_not_the_source():
    """The second defect the first real run exposed, and the same shape.

    `QUERIES_PER_CHAPTER` caps the budget against "the work's own size", and the
    work is the one being *written*. A 400,000-character book whose headings
    were never detected has one source chapter and seventeen target ones, and
    feeding the source count in gave it 3 queries where it had earned 51 — a
    17x under-budget on precisely the documents already worst served by the
    probe. On this corpus, 27 of 52 documents have exactly one detected chapter.
    """
    from brainworker.transform.estimate import projected_chapters
    from brainworker.transform.genres import GENRES

    target = projected_chapters(1, 400_000, GENRES["essay"])
    assert target > 10, "a 400k book is not a one-chapter work"
    assert budget.budget_for(400, ALL, target) > budget.budget_for(400, ALL, 1)


def test_the_budget_is_capped_three_ways():
    huge = budget.budget_for(100_000, ALL, 400)
    assert huge <= budget.MAX_QUERIES
    # And by the work's own size, so a short document against a large library
    # cannot buy a large research bill for a small work.
    assert budget.budget_for(100_000, ALL, 2) <= budget.QUERIES_PER_CHAPTER * 2
    # And per purpose, so one purpose cannot consume a whole run.
    assert budget.budget_for(100_000, ["context"], 400) <= budget.MAX_PER_PURPOSE


def test_a_supported_library_always_earns_the_floor():
    assert budget.budget_for(1, ["context"], 10) == budget.MIN_QUERIES


@pytest.mark.parametrize("total", [4, 7, 12, 40])
def test_the_allowance_never_exceeds_what_is_left(total: int):
    remaining = 20
    for left in range(total, 0, -1):
        allowance = budget.allowance_for(remaining, left)
        assert 0 <= allowance <= remaining
        remaining -= allowance
    assert remaining >= 0


def test_the_whole_budget_is_spendable_and_the_last_chapter_is_not_starved():
    """The defect the slack term produced, pinned.

    An even share plus two left the last two chapters of six with **nothing**,
    because the slack compounds: each chapter takes its share plus two out of a
    pool the previous chapter has already taken its slack from.
    """
    remaining = 20
    spent: list[int] = []
    for left in range(6, 0, -1):
        allowance = budget.allowance_for(remaining, left)
        spent.append(allowance)
        remaining -= allowance
    assert all(a > 0 for a in spent), spent
    assert sum(spent) == 20


def test_an_underspending_chapter_returns_its_allowance_to_the_pool():
    """Self-balancing, which is why there is no slack term: the workflow
    subtracts what a chapter *spent*, never what it was allowed."""
    remaining = 20
    allowances: list[int] = []
    for left in range(6, 0, -1):
        allowance = budget.allowance_for(remaining, left)
        allowances.append(allowance)
        remaining -= allowance // 4  # each chapter spends a quarter of its share
    assert allowances[-1] > allowances[0], allowances


def test_the_probe_samples_evenly_and_includes_both_ends():
    passages = _passages(n=60)
    texts = budget.probe_passages(passages, sample=4)
    assert len(texts) == 4
    assert all(0 < len(t) <= budget.PROBE_CHARS for t in texts)
    assert texts[0].startswith("Texto 0.")
    assert "Texto 59." in texts[-1]


def test_a_document_with_one_detected_chapter_is_still_sampled_across():
    """The defect the first real run found, measured on the real corpus.

    The probe sampled *chapter* openings, which is a good spread over eight of
    them and a terrible one over one — and **27 of this corpus's 52 documents
    have exactly one detected chapter**, because `build_chunks` consumes a
    heading paragraph and this corpus has recorded heading defects. The worst
    case was a 500-passage book probed **once**, on its first 600 characters,
    with the whole run's research budget derived from that sample.
    """
    passages = _passages(n=500)
    assert len(reading.chapters_of(passages)) >= 1
    one_chapter = reading.passages_of(
        [
            {"index": i, "kind": "cuerpo", "chapter": "Sin capítulos detectados",
             "text": f"Texto {i}. " * 60}
            for i in range(500)
        ]
    )
    assert len(reading.chapters_of(one_chapter)) == 1
    texts = budget.probe_passages(one_chapter)
    assert len(texts) == budget.PROBE_SAMPLE
    # And they are spread, not eight readings of the opening.
    assert texts[0] != texts[-1]
    assert "Texto 0." in texts[0]
    assert "Texto 499." in texts[-1]


def test_the_probe_reads_the_whole_document_and_not_only_its_front():
    """The front matter of a book is its least characteristic part — a preface
    is about the author and an index is about nothing — so a probe that read
    only the opening would measure the library's coverage of prefaces."""
    assert budget._spread(100, 8) == [0, 14, 28, 42, 57, 71, 85, 99]
    assert budget._spread(3, 8) == [0, 1, 2]
    assert budget._spread(1, 8) == [0]


def test_an_empty_document_probes_nothing_rather_than_raising():
    assert budget.probe_passages([], sample=4) == []
    assert budget.probe_passages(_passages(n=3), sample=0) == []
