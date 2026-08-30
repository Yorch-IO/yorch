"""A scripture reference must be a term, not just the name of its book.

`bm25.tokenize` splits on non-alphanumerics and drops tokens under
MIN_TOKEN_LEN, so "Juan 10:6" reduced to ``juan``: the chapter and verse were
gone, and every citation of John in a book was the same term.

Measured on `libros/02-PuertasEternas_INT.pdf`, which carries 49 distinct
references: **42 of them lost their chapter:verse entirely.** Simulating BM25
over that book's own 240 indexed chunks, looking up a reference's exact text
found its own chunk at rank 1 in 12 of 49 cases before this rule and 45 of 49
after, with the median rank going from 5 to 1. The same simulation over the
book's 40 synthetic eval questions is unchanged to three decimals — the rule is
additive, which is what these tests pin from both ends.
"""

from __future__ import annotations

from docagent.bm25 import STOPWORDS, query_sparse_vector, tokenize


def test_a_reference_keeps_its_chapter_and_verse():
    assert tokenize("Juan 10:6") == ["juan", "juan10v6"]


def test_two_verses_of_one_chapter_are_different_terms():
    """The defect, stated so the fix has something to beat: before this rule
    both sides of the assertion were ``['juan']``."""
    assert tokenize("Juan 10:6") != tokenize("Juan 10:12")


def test_the_book_name_survives_beside_the_compound():
    """A query naming only the book must keep working, so the plain token is
    still emitted."""
    assert "juan" in tokenize("Juan 10:6")


def test_a_numbered_book_carries_its_number():
    assert "1sam10v24" in tokenize("1 Sam. 10:24")


def test_an_abbreviation_folds_its_accent_like_every_other_token():
    assert "gen2v15" in tokenize("Gén. 2:15")
    assert "exo20v4" in tokenize("Éxo. 20:4-6")


def test_a_clock_time_is_not_a_scripture_reference():
    """The guard is that the book token may not be a stopword. Without it
    "a las 10:30" mints ``las10v30``."""
    assert "las" in STOPWORDS
    assert tokenize("a las 10:30") == []


def test_the_query_leg_mints_the_same_term_as_the_document_leg():
    """Both legs go through `tokenize`, so nothing has to be configured twice —
    but a future refactor could split them, and then the compound would be
    indexed and never queried."""
    from docagent.bm25 import term_id

    assert term_id("juan10v6") in query_sparse_vector("¿Qué dice Juan 10:6?").indices
