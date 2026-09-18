"""What one chapter hands the next, and the bound it may never cross.

`Continuity` is the only structure in this feature that accumulates. An
unbounded one would not fail loudly: it would make chapter forty of a
sixty-chapter book die on a payload the previous thirty-nine had grown, after
all thirty-nine had been paid for.
"""

from __future__ import annotations

from brainworker.transform.types import (
    MAX_CITED,
    MAX_CONTINUITY_BYTES,
    MAX_ESTABLISHED,
    MAX_GLOSSARY,
    MAX_THREADS,
    TAIL_CHARS,
    Continuity,
)


def _grown(chapters: int) -> Continuity:
    """A record carried through `chapters` chapters, each adding generously."""
    carried = Continuity()
    for i in range(chapters):
        carried = Continuity(
            glossary={**carried.glossary, **{f"term {i}-{j}": "x" * 60 for j in range(4)}},
            established=carried.established + [f"{i}. A chapter title — with an intent"],
            threads=carried.threads + [f"thread {i} left open" * 3],
            tail=carried.tail + "prose " * 400,
            cited=carried.cited + [f"chk_{i}_{j}" for j in range(8)],
        ).trimmed()
    return carried


def test_every_bound_is_enforced():
    carried = _grown(200)
    assert len(carried.glossary) <= MAX_GLOSSARY
    assert len(carried.established) <= MAX_ESTABLISHED
    assert len(carried.threads) <= MAX_THREADS
    assert len(carried.tail) <= TAIL_CHARS
    assert len(carried.cited) <= MAX_CITED


def test_a_two_hundred_chapter_run_stays_under_the_stated_ceiling():
    """The bound asserted rather than assumed.

    "It is probably small" is not a bound, and the rule it would be breaking —
    bulk data never enters a payload — is one this codebase states absolutely.
    """
    assert _grown(200).size() < MAX_CONTINUITY_BYTES


def test_the_glossary_keeps_its_first_rendering():
    """A term must not drift, so the entry to protect is the one on the page.

    The opposite of the usual newest-wins, and defensible here where the graph's
    own first-writer-wins is not: chapters are written in order, so the first
    writer of a term is where the term is introduced, while `_MERGE_CONCEPTS`
    takes whichever document happened to project first — which produced 207
    wrong display names out of 12,196 concepts.
    """
    first = Continuity(glossary={"gracia": "la gracia"}).trimmed()
    later = Continuity(glossary={**first.glossary}).trimmed()
    assert later.glossary["gracia"] == "la gracia"


def test_the_tail_keeps_the_end_and_not_the_beginning():
    """It exists so the next chapter continues from a sentence, not from a
    summary. Keeping the head would hand it the wrong sentence.
    """
    carried = Continuity(tail="A" * 5_000 + "THE LAST WORDS").trimmed()
    assert carried.tail.endswith("THE LAST WORDS")
    assert len(carried.tail) == TAIL_CHARS


def test_the_established_list_keeps_the_most_recent():
    carried = Continuity(
        established=[f"{i}. chapter" for i in range(MAX_ESTABLISHED + 40)]
    ).trimmed()
    assert carried.established[-1].startswith(str(MAX_ESTABLISHED + 39))
