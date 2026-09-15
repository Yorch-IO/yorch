"""The book a run's chunks make, asserted on the archive rather than on a mock.

`brainworker.epub` is pure for exactly this reason: what is worth checking is
the rendering, and every one of these runs without a workspace, a catalog or a
provider. The archive is opened and read back, because the failures that matter
here are ones a returning value cannot show — a paragraph that is in the book
twice, a heading that never made it, an accent mangled on the way through two
layers of escaping.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from brainworker import epub


def rows(*specs: dict) -> list[dict]:
    """Chunk rows with the fields `indexing.chunk_row` writes for a document."""
    base = {"chapter": "", "section": "", "kind": "cuerpo", "overlap": "", "context": ""}
    return [{**base, **s} for s in specs]


def book(chapters: list[epub.Chapter], **kw) -> epub.Book:
    return epub.Book(
        title=kw.get("title", "Un libro"),
        author=kw.get("author", "Una autora"),
        language=kw.get("language", "es"),
        identifier=epub.identifier_for(kw.get("version_id", "ver_abc")),
        chapters=chapters,
    )


def opened(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def text_of(data: bytes, name: str) -> str:
    return opened(data).read(name).decode("utf-8")


def every_chapter(data: bytes) -> str:
    archive = opened(data)
    return "\n".join(
        archive.read(n).decode("utf-8")
        for n in archive.namelist()
        if n.startswith("OEBPS/chapters/")
    )


# --- the container ---------------------------------------------------------


def test_the_archive_is_a_readable_zip_whose_mimetype_comes_first_uncompressed():
    """The one check every EPUB validator makes before any other.

    A reader identifies the container by reading `mimetype` at a fixed offset
    without unzipping, which only works if it is the first entry and stored
    rather than deflated. Nothing else in this module can tell you it is wrong;
    the file simply fails to open in a reader and works perfectly in `zipfile`.
    """
    data = epub.build(book([epub.Chapter("Uno", (epub.Block("Hola."),))]))
    archive = opened(data)
    assert archive.testzip() is None
    first = archive.infolist()[0]
    assert first.filename == "mimetype"
    assert first.compress_type == zipfile.ZIP_STORED
    assert archive.read("mimetype") == b"application/epub+zip"


def test_every_chapter_is_in_the_manifest_and_the_spine():
    """A file in the archive that the spine does not name is a page no reader
    will ever turn to — it is in the book and unreachable."""
    chapters = [epub.Chapter(f"Capítulo {n}", (epub.Block("Texto."),)) for n in range(1, 4)]
    data = epub.build(book(chapters))
    opf = text_of(data, "OEBPS/content.opf")
    nav = text_of(data, "OEBPS/nav.xhtml")
    for n in range(1, 4):
        href = f"chapters/{n:04d}.xhtml"
        assert f'href="{href}"' in opf
        assert f'idref="chapter-{n:04d}"' in opf
        assert href in nav
        assert f"OEBPS/{href}" in opened(data).namelist()


def test_an_accented_title_survives_into_the_metadata_and_the_cover():
    data = epub.build(book([], title="Teología y república", author="Darío Silva-Silva"))
    opf = text_of(data, "OEBPS/content.opf")
    assert "<dc:title>Teología y república</dc:title>" in opf
    assert "<dc:creator>Darío Silva-Silva</dc:creator>" in opf
    assert "Teología y república" in text_of(data, "OEBPS/cover.xhtml")


def test_a_document_with_no_author_carries_no_creator_element():
    """An empty `dc:creator` is a claim that the book has an author called "".

    `read_metadata` returns None rather than guessing precisely so this can be
    absent, and an element written anyway would undo that.
    """
    data = epub.build(book([], author=None))
    assert "dc:creator" not in text_of(data, "OEBPS/content.opf")


def test_two_builds_of_the_same_version_are_byte_identical():
    """The sha256 is recorded in the catalog and verified on every download, so
    a build that embedded the clock would make "regenerate" look like a
    different book every time."""
    chapters = epub.chapters_from_rows(rows({"chapter": "Uno", "text": "Hola."}))
    assert epub.build(book(chapters)) == epub.build(book(chapters))


def test_the_identifier_is_derived_from_the_version_and_not_from_chance():
    assert epub.identifier_for("ver_a") == epub.identifier_for("ver_a")
    assert epub.identifier_for("ver_a") != epub.identifier_for("ver_b")
    assert epub.identifier_for("ver_a").startswith("urn:uuid:")


# --- the outline -----------------------------------------------------------


def test_a_heading_is_emitted_when_it_changes_and_not_once_per_chunk():
    """The outline is reconstructed from the rows because the chunker consumed
    it. Emitting `section` on every row would repeat the heading above every
    paragraph of the section."""
    data = epub.build(book(epub.chapters_from_rows(rows(
        {"chapter": "1. Providencia", "section": "1.1 Creación", "text": "Primero."},
        {"chapter": "1. Providencia", "section": "1.1 Creación", "text": "Segundo."},
        {"chapter": "1. Providencia", "section": "1.2 Gobierno", "text": "Tercero."},
    ))))
    body = every_chapter(data)
    assert body.count("<h2>1.1 Creación</h2>") == 1
    assert body.count("<h2>1.2 Gobierno</h2>") == 1
    assert body.count("<h1>1. Providencia</h1>") == 1


def test_a_deeper_section_path_nests_rather_than_flattening():
    chapters = epub.chapters_from_rows(rows(
        {"chapter": "Uno", "section": "A > B", "text": "Texto."},
    ))
    body = every_chapter(epub.build(book(chapters)))
    assert "<h2>A</h2>" in body
    assert "<h3>B</h3>" in body


def test_a_document_with_no_chapters_at_all_becomes_one_chapter():
    """Measured on real runs: a book with no numbered headings produces 500 rows
    and zero chapters. Zero chapters is an empty spine, which is a file no
    reader will open — and the document is not empty, it is unstructured."""
    chapters = epub.chapters_from_rows(rows({"text": "Sin estructura."}))
    assert len(chapters) == 1
    assert chapters[0].title == epub.UNTITLED_CHAPTER
    assert "Sin estructura." in every_chapter(epub.build(book(chapters)))


def test_a_book_with_no_chapters_still_has_a_spine():
    data = epub.build(book([]))
    assert "OEBPS/chapters/0001.xhtml" in opened(data).namelist()
    assert 'idref="chapter-0001"' in text_of(data, "OEBPS/content.opf")


def test_a_chapter_that_returns_later_opens_a_second_file():
    """Chapters are grouped by *transition*, not collected by name. Merging two
    runs of the same title would reorder the document, which is worse than
    printing the title twice."""
    chapters = epub.chapters_from_rows(rows(
        {"chapter": "A", "text": "Uno."},
        {"chapter": "B", "text": "Dos."},
        {"chapter": "A", "text": "Tres."},
    ))
    assert [c.title for c in chapters] == ["A", "B", "A"]


def test_an_empty_row_reaches_no_page():
    chapters = epub.chapters_from_rows(rows(
        {"chapter": "A", "text": "   "}, {"chapter": "A", "text": "Real."}
    ))
    assert sum(len(c.blocks) for c in chapters) == 1


# --- what must never be rendered -------------------------------------------


def test_the_overlap_never_reaches_the_page():
    """`overlap` is the *previous* chunk's tail, carried so retrieval can show a
    fragment in context, and it is non-empty on about four rows in five. A
    renderer that emitted it would duplicate a paragraph on nearly every page —
    which reads as a corrupt book rather than as a bug.
    """
    data = epub.build(book(epub.chapters_from_rows(rows(
        {"chapter": "A", "text": "Propio.", "overlap": "COLA DEL ANTERIOR"},
    ))))
    assert b"COLA DEL ANTERIOR" not in data


def test_neither_the_embed_text_nor_the_context_reaches_the_page():
    """`embed_text` is the breadcrumb and the overlap concatenated ahead of the
    text — the worst of the three to render, and the field most likely to be
    reached for by someone who wants "the chunk's text"."""
    data = epub.build(book(epub.chapters_from_rows(rows({
        "chapter": "A",
        "text": "Propio.",
        "embed_text": "A > B\n\nCOLA\n\nPropio.",
        "context": "CONTEXTO",
    }))))
    assert b"COLA" not in data and b"CONTEXTO" not in data


def test_markup_in_the_document_is_escaped_rather_than_rendered():
    """The text is a book's own prose and may legitimately contain `<` — a
    theology corpus is full of `<<` quotations — and XHTML that does not parse
    is a chapter a reader sees as an error page."""
    data = epub.build(book(epub.chapters_from_rows(rows(
        {"chapter": "A", "text": "Si a < b & b < c, <em>entonces</em>."},
    ))))
    body = every_chapter(data)
    assert "&lt;em&gt;entonces&lt;/em&gt;" in body
    assert "a &lt; b &amp; b &lt; c" in body


# --- paragraphs and kinds --------------------------------------------------


def test_a_chunk_splits_into_one_paragraph_per_blank_line():
    chapters = epub.chapters_from_rows(rows(
        {"chapter": "A", "text": "Uno.\n\nDos.\n\nTres."},
    ))
    body = every_chapter(epub.build(book(chapters)))
    assert body.count("<p ") == 3


def test_a_single_newline_inside_a_paragraph_is_kept_as_a_break():
    """Verse and numbered lists are single-newline-separated inside one
    paragraph, and reflowing a psalm into prose is a different text."""
    chapters = epub.chapters_from_rows(rows({"chapter": "A", "text": "Verso uno\nVerso dos"}))
    body = every_chapter(epub.build(book(chapters)))
    assert "Verso uno<br/>Verso dos" in body


@pytest.mark.parametrize(
    "kind,css",
    [("cuerpo", "cuerpo"), ("nota", "nota"), ("preguntas", "preguntas"),
     ("tabla_fila", "tabla"), ("diapositiva", "diapositiva")],
)
def test_a_chunk_kind_becomes_a_class_a_stylesheet_can_reach(kind, css):
    chapters = epub.chapters_from_rows(rows({"chapter": "A", "text": "X.", "kind": kind}))
    assert f'class="{css}"' in every_chapter(epub.build(book(chapters)))


def test_a_kind_this_module_has_not_been_taught_styles_as_prose():
    """The safe default. A new chunk kind must not produce markup with no rule
    behind it, and prose is what a body paragraph looks like anyway."""
    chapters = epub.chapters_from_rows(rows({"chapter": "A", "text": "X.", "kind": "algo_nuevo"}))
    assert 'class="cuerpo"' in every_chapter(epub.build(book(chapters)))


# --- the video shape -------------------------------------------------------


def test_a_transcript_becomes_one_chapter_with_a_marker_on_each_fragment():
    """An hour of speech cut into 69 fragments must not produce 69 entries in
    the table of contents: the timestamp goes beside the text, not into the
    outline."""
    chapters = epub.chapters_from_rows(
        rows(
            {"chapter": "Charla", "text": "Primero.", "kind": "transcripcion", "start_s": 0.0},
            {"chapter": "Charla", "text": "Segundo.", "kind": "transcripcion", "start_s": 3725.0},
        ),
        timed=True,
    )
    assert len(chapters) == 1
    body = every_chapter(epub.build(book(chapters)))
    assert '<span class="marca">0:00</span>' in body
    assert '<span class="marca">1:02:05</span>' in body


def test_a_timed_document_ignores_the_section_field_entirely():
    """A transcript has no sections, and a stray one would put a heading in the
    middle of continuous speech."""
    chapters = epub.chapters_from_rows(
        rows({"chapter": "Charla", "section": "ruido", "text": "Hola.", "start_s": 1.0}),
        timed=True,
    )
    assert "ruido" not in every_chapter(epub.build(book(chapters)))


@pytest.mark.parametrize(
    "seconds,shown",
    [(0, "0:00"), (9, "0:09"), (61, "1:01"), (599, "9:59"), (3600, "1:00:00"),
     (3725, "1:02:05"), (-4, "0:00")],
)
def test_a_timestamp_reads_the_way_a_player_shows_it(seconds, shown):
    assert epub.timestamp(seconds) == shown


def test_a_marker_opens_the_first_paragraph_of_its_fragment_and_no_other():
    chapters = epub.chapters_from_rows(
        rows({"chapter": "C", "text": "Uno.\n\nDos.", "start_s": 5.0}), timed=True
    )
    body = every_chapter(epub.build(book(chapters)))
    assert body.count('class="marca"') == 1
