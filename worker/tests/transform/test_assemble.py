"""The finished file: the work, its bibliography, and nothing else.

The brief is explicit that the output carries no preamble, no detected source
genre, no mode and no summary of changes. That is a property of this file and it
is asserted here, because every other place it could be checked is a place a
person would have to remember to look.
"""

from __future__ import annotations

from brainworker.transform import assemble
from brainworker.transform.genres import GENRES
from brainworker.transform.types import CitedSource, ComposedChapter

LEAK = "ZZ-PLANTED-OVERLAP-ZZ"


def _rows():
    return [
        assemble.draft_row(
            ComposedChapter(ordinal=2, title="Segundo", body="Cuerpo del segundo.")
        ),
        assemble.draft_row(
            ComposedChapter(
                ordinal=1,
                title="Primero",
                body="Cuerpo del primero.",
                cited=[
                    CitedSource(chunk_id="c", version_id="v", title="Obra",
                                locator="Obra · p. 1", claim="algo")
                ],
            )
        ),
    ]


def test_chapters_are_emitted_in_ordinal_order_and_not_file_order():
    """A book with its chapters shuffled reads as a corrupt export rather than
    as a scheduling change, and the file is rewritten whole by every chapter."""
    text = assemble.assemble(
        _rows(), genre=GENRES["essay"], language="es", work_title="La Obra",
        references=[],
    )
    assert text.index("## Primero") < text.index("## Segundo")


def test_the_file_carries_the_work_and_the_bibliography_and_nothing_else():
    text = assemble.assemble(
        _rows(), genre=GENRES["essay"], language="es", work_title="La Obra",
        references=["Smith, J. Obra. 1999."],
    )
    assert text.startswith("# La Obra")
    for forbidden in (
        "faithful", "adaptive", "source genre", "género original",
        "transformación", "chunk_id", "summary of changes",
    ):
        assert forbidden not in text
    assert "## Fuentes" in text


def test_neither_overlap_nor_embed_text_can_reach_the_file():
    """The planting test the EPUB export already pins, applied to the one file a
    person reads. `overlap` is non-empty on about four rows in five."""
    rows = _rows()
    for row in rows:
        row["overlap"] = LEAK
        row["embed_text"] = LEAK
    text = assemble.assemble(
        rows, genre=GENRES["essay"], language="es", work_title="La Obra",
        references=[],
    )
    assert LEAK not in text


def test_a_row_round_trips_through_its_own_reader():
    """`draft_row` and `chapter_of` are a pair, for the reason
    `indexing.chunk_row` and `StoredChunk.from_row` are: the file is written by
    one activity and read by two."""
    original = ComposedChapter(
        ordinal=3, title="Tercero", body="Texto.",
        cited=[CitedSource(chunk_id="c", version_id="v", title="T",
                           locator="l", claim="x")],
    )
    back = assemble.chapter_of(assemble.draft_row(original))
    assert back == original


def test_a_malformed_citation_row_is_dropped_rather_than_crashing_the_assembly():
    row = assemble.draft_row(ComposedChapter(ordinal=1, title="A", body="B"))
    row["cited"] = ["not a dict", {"chunk_id": "ok"}]
    chapter = assemble.chapter_of(row)
    assert [c.chunk_id for c in chapter.cited] == ["ok"]


def test_an_untitled_chapter_emits_no_heading():
    """A document whose chapters were never detected becomes one untitled
    chapter, because that is exactly what the index holds."""
    rows = [assemble.draft_row(ComposedChapter(ordinal=1, title="", body="Solo texto."))]
    text = assemble.assemble(
        rows, genre=GENRES["essay"], language="es", work_title="", references=[]
    )
    assert not text.startswith("#\n")
    assert text.startswith("Solo texto.")


def test_the_counts_measure_what_reaches_the_page():
    rows = _rows()
    assert assemble.characters(rows) == len("Cuerpo del segundo.") + len(
        "Cuerpo del primero."
    )
    assert assemble.cited_versions(rows) == 1
