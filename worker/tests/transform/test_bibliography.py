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


@pytest.mark.parametrize("name", GENRE_NAMES)
def test_every_genre_names_the_work_it_is_a_recasting_of(name: str):
    """Reported after reading a finished essay: it named every source except the
    one it was made from.

    The document is not "consulted" and it is not a reference the original
    carries — it is the substance. A reader who cannot tell what a recasting
    recasts has been handed an orphan, so it gets its own heading, first, in
    every genre including the ones whose bodies carry no citation marks.
    """
    text = bibliography.render(
        style=GENRES[name].bibliography,
        language="es",
        references=["Smith, J. Una obra. 1999."],
        sources=SOURCES,
        source_title="4.-Doctrina-de-la-Regeneración",
        source_author="Darío Silva-Silva",
    )
    source_at = text.index("Obra de origen")
    original_at = text.index("Referencias de la obra original")
    library_at = text.index("Obras de esta biblioteca consultadas")
    assert source_at < original_at < library_at
    assert "4.-Doctrina-de-la-Regeneración — Darío Silva-Silva" in text


def test_a_source_with_no_author_is_named_without_one():
    text = bibliography.render(
        style=GENRES["essay"].bibliography, language="es", references=[],
        sources=[], source_title="Un Documento", source_author="",
    )
    assert "- Un Documento\n" in text
    assert "—" not in text.split("Referencias de la obra original")[0].split("###")[1]


def test_a_source_the_catalog_could_not_name_says_so_rather_than_inventing():
    """The heading stays and the sentence says what happened, which is the
    `/project-summary` `available` rule: an absent section and "there was none"
    are different facts."""
    text = bibliography.render(
        style=GENRES["essay"].bibliography, language="es", references=[],
        sources=[], source_title="", source_author="",
    )
    assert "Obra de origen" in text
    assert "No consta de qué obra procede este texto." in text


def test_the_source_is_named_in_english_too():
    text = bibliography.render(
        style=GENRES["study"].bibliography, language="en", references=[],
        sources=[], source_title="A Document", source_author="R. Writer",
    )
    assert "The work this is a recasting of" in text
    assert "A Document — R. Writer" in text
