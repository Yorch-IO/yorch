"""The closing chapter, which no model touches.

The property worth asserting is negative: nothing in this path can fabricate,
because nothing that could fabricate is involved. Every library entry is a
locator a verified citation carried and every original reference is a string
found in the source's own bytes.
"""

from __future__ import annotations

import pytest

from brainworker.transform import bibliography
from brainworker.transform.genres import GENRE_NAMES, GENRES
from brainworker.transform.types import CitedSource

SOURCES = [
    CitedSource(chunk_id="c1", version_id="v1", document_id="d1",
                title="Teología Integral", locator="Teología Integral · cap. 2",
                claim="la fe precede a la obra"),
    CitedSource(chunk_id="c2", version_id="v1", document_id="d1",
                title="Teología Integral", locator="Teología Integral · cap. 5",
                claim="la obra sigue a la fe"),
    CitedSource(chunk_id="c3", version_id="v2", document_id="d2",
                title="Otro Libro", locator="Otro Libro · p. 9"),
]


@pytest.mark.parametrize("name", GENRE_NAMES)
def test_every_genre_renders_both_lists_and_never_merges_them(name: str):
    """Two lists, never one alphabetical merge.

    A work the source cited (which this work has never read) and a work this
    work actually quoted are different things. `synthesis.py`'s split applied to
    provenance: merging would put the text a reader must treat sceptically
    beside the one they may rely on, under one heading.
    """
    text = bibliography.render(
        style=GENRES[name].bibliography,
        language="es",
        references=["Smith, J. Una obra. 1999."],
        sources=SOURCES,
    )
    original = text.index("Referencias de la obra original")
    library = text.index("Obras de esta biblioteca consultadas")
    assert original < library
    assert "Smith, J. Una obra. 1999." in text
    assert "Teología Integral" in text


def test_an_empty_list_renders_its_heading_and_a_sentence():
    """An absent section and "there were none" are different facts — the
    `/project-summary` `available` rule, in prose. A reader who sees no library
    heading cannot tell whether it was consulted and gave nothing, or was never
    consulted at all.
    """
    text = bibliography.render(
        style=GENRES["novel"].bibliography, language="es", references=[], sources=[]
    )
    assert "Referencias de la obra original" in text
    assert "La obra original no recoge referencias." in text
    assert "Obras de esta biblioteca consultadas" in text
    assert "No se consultó ninguna otra obra" in text


def test_one_work_cited_twice_is_one_entry_with_both_locators():
    text = bibliography.render(
        style=GENRES["treatise"].bibliography, language="es",
        references=[], sources=SOURCES,
    )
    assert text.count("Teología Integral —") == 1
    assert "cap. 2" in text and "cap. 5" in text


def test_grouping_is_on_the_version_and_not_on_the_title():
    """A title is a display string two versions can share — this corpus has
    measured books whose running header survived into the index as a chapter
    title — while a version id is what the catalog actually distinguishes."""
    same_title = [
        CitedSource(chunk_id="a", version_id="v1", title="Obra", locator="p. 1"),
        CitedSource(chunk_id="b", version_id="v2", title="Obra", locator="p. 2"),
    ]
    assert len(bibliography.group(same_title)) == 2


def test_a_numbered_genre_numbers_and_a_plain_one_does_not():
    numbered = bibliography.render(
        style=GENRES["treatise"].bibliography, language="es",
        references=["A", "B"], sources=[],
    )
    plain = bibliography.render(
        style=GENRES["essay"].bibliography, language="es",
        references=["A", "B"], sources=[],
    )
    assert "1. A" in numbered and "2. B" in numbered
    assert "- A" in plain and "- B" in plain


def test_annotation_is_per_genre():
    annotated = bibliography.render(
        style=GENRES["treatise"].bibliography, language="es",
        references=[], sources=SOURCES,
    )
    bare = bibliography.render(
        style=GENRES["counsel"].bibliography, language="es",
        references=[], sources=SOURCES,
    )
    assert "la fe precede a la obra" in annotated
    assert "la fe precede a la obra" not in bare


def test_an_unknown_language_falls_back_visibly_to_english():
    """Visible rather than silent: a Portuguese book with an English heading is
    obviously wrong, where a mistranslated one would not be."""
    assert bibliography.language_of("pt") == "en"
    assert bibliography.language_of("") == "en"
    assert bibliography.language_of("ES") == "es"
    text = bibliography.render(
        style=GENRES["study"].bibliography, language="pt", references=[], sources=[]
    )
    assert "References in the original work" in text


def test_an_untitled_work_says_so_rather_than_printing_nothing():
    text = bibliography.render(
        style=GENRES["study"].bibliography, language="es", references=[],
        sources=[CitedSource(chunk_id="a", version_id="v", title="", locator="p. 1")],
    )
    assert "(sin título)" in text


def test_a_reference_the_sweep_could_not_parse_survives_verbatim():
    ugly = "vid. supra, n. 14; cf. tambien el apendice (sin paginar)"
    text = bibliography.render(
        style=GENRES["history"].bibliography, language="es",
        references=[ugly], sources=[],
    )
    assert ugly in text


def test_the_chapter_title_is_the_genre_s_register():
    assert bibliography.chapter_title(GENRES["treatise"].bibliography, "es") == "Bibliografía"
    assert bibliography.chapter_title(GENRES["novel"].bibliography, "es") == "Nota sobre las fuentes"
    assert bibliography.chapter_title(GENRES["novel"].bibliography, "en") == "A note on sources"


def test_first_appearance_order_is_kept():
    """The order the work cited them in, which is the order a reader met them."""
    rows = bibliography.group(list(reversed(SOURCES)))
    assert [title for title, _, _ in rows] == ["Otro Libro", "Teología Integral"]
