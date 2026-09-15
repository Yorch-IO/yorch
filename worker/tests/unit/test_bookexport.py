"""Reading a run's chunks, deciding whether to ask a model, naming the file.

The half of the EPUB feature between the pure renderer and the stores. The
workspace here is a `tmp_path` with no catalog behind it — the same arrangement
`tests/activities/test_ingest.py` uses and for the same reason: artifact
recording is best-effort bookkeeping, so running without one continuously proves
these functions do not depend on one.
"""

from __future__ import annotations

import io
import pathlib
import zipfile

import pytest

from brainworker import booking, bookexport, epub
from brainworker.artifacts import ArtifactStore, ContentChanged


def chunk(**kw) -> dict:
    base = {"chapter": "", "section": "", "kind": "cuerpo", "text": "Texto.",
            "overlap": "", "context": ""}
    return {**base, **kw}


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    return tmp_path


# --- reading the source ----------------------------------------------------


def test_the_chunks_are_read_back_with_their_digest_checked(workspace):
    """Verified rather than merely read, and it matters more here than anywhere
    else this artifact is opened: the standalone path runs long after the run
    that wrote the file, and a `chunks.jsonl` rewritten since would make the
    book describe a cutting the index no longer holds — silently, because every
    row in it would still be well-formed."""
    store = ArtifactStore(workspace, "ingest-1")
    ref = store.write_jsonl("chunks", [chunk(text="Original.")])
    assert bookexport.read_rows(workspace, "ingest-1", ref)[0]["text"] == "Original."

    store.write_jsonl("chunks", [chunk(text="Reescrito.")])
    with pytest.raises(ContentChanged):
        bookexport.read_rows(workspace, "ingest-1", ref)


def test_the_book_lands_in_the_run_that_records_it_not_the_one_it_reads(workspace):
    """`run_artifact` is keyed on `(run_id, name)`, so a run may only write its
    own artifacts. On the standalone path the chunks belong to an earlier run
    and only the book is new."""
    source = ArtifactStore(workspace, "ingest-old")
    rows = bookexport.read_rows(
        workspace, "ingest-old", source.write_jsonl("chunks", [chunk()])
    )
    ref = bookexport.write_book(
        workspace, "epub-new", rows,
        version_id="ver_1", title="Libro", author=None, language="es",
    )
    assert ref.path == "runs/epub-new/book.epub"
    assert (workspace / ref.path).is_file()
    assert ref.sha256 and ref.bytes == (workspace / ref.path).stat().st_size


def test_what_is_written_is_a_readable_archive(workspace):
    store = ArtifactStore(workspace, "r")
    rows = bookexport.read_rows(
        workspace, "r", store.write_jsonl("chunks", [chunk(chapter="Uno", text="Hola.")])
    )
    ref = bookexport.write_book(
        workspace, "r", rows, version_id="ver_1", title="Libro", author="A", language="es"
    )
    data = (workspace / ref.path).read_bytes()
    assert zipfile.ZipFile(io.BytesIO(data)).testzip() is None


# --- which shape of book ---------------------------------------------------


def test_a_run_carrying_timestamps_is_recognised_without_asking_the_run_kind():
    """The rows are what gets rendered, and `start_s` is written by exactly one
    writer for exactly one reason. A run kind would be a second answer to a
    question the data already answers."""
    assert bookexport.is_timed([chunk(start_s=0.0, end_s=4.0)])
    assert not bookexport.is_timed([chunk()])
    assert bookexport.is_timed([chunk(), chunk(start_s=9.0)])


def test_a_timed_run_produces_the_transcript_shape(workspace):
    store = ArtifactStore(workspace, "video-1")
    rows = bookexport.read_rows(workspace, "video-1", store.write_jsonl("chunks", [
        chunk(chapter="Charla", text="Uno.", kind="transcripcion", start_s=0.0, end_s=3.0),
        chunk(chapter="Charla", text="Dos.", kind="transcripcion", start_s=65.0, end_s=70.0),
    ]))
    ref = bookexport.write_book(
        workspace, "video-1", rows,
        version_id="ver_v", title="Charla", author="Canal", language="es",
    )
    archive = zipfile.ZipFile(io.BytesIO((workspace / ref.path).read_bytes()))
    pages = [n for n in archive.namelist() if n.startswith("OEBPS/chapters/")]
    assert len(pages) == 1
    assert '<span class="marca">1:05</span>' in archive.read(pages[0]).decode("utf-8")


# --- the excerpt a model reads ---------------------------------------------


def test_the_excerpt_folds_the_headings_back_in():
    """A chunk row does not contain its heading — the chunker *consumes* the
    paragraph — and the title of a book is very often exactly the heading that
    was consumed. Reading only `text` hands the model the one part of the front
    matter that had the title removed from it."""
    text = bookexport.excerpt([
        chunk(chapter="El reto de Dios", text="Darío Silva-Silva"),
        chunk(chapter="El reto de Dios", section="Prólogo", text="Un libro sobre la fe."),
    ], 500)
    assert "El reto de Dios" in text
    assert "Prólogo" in text
    assert text.count("El reto de Dios") == 1


def test_the_excerpt_stops_at_the_limit_it_was_given():
    """The estimate prices exactly this number of characters, so a reader that
    ran past it would spend more than the gate quoted."""
    rows = [chunk(text="x" * 1000) for _ in range(20)]
    assert len(bookexport.excerpt(rows, 900)) == 900


def test_the_excerpt_of_an_empty_run_is_empty():
    assert bookexport.excerpt([], 100) == ""


# --- the filename ----------------------------------------------------------


def test_the_saved_name_keeps_the_accents_a_reader_expects():
    assert bookexport.filename_for("Teología y república") == "Teología y república.epub"


def test_the_header_fallback_keeps_only_what_a_header_can_carry():
    """An HTTP header is Latin-1 and «Teología» is not. The accented name rides
    in RFC 5987's `filename*`; this is what an old client reads."""
    name = bookexport.ascii_filename_for("Teología y república")
    assert name.isascii() and name.endswith(".epub")


def test_a_title_that_is_only_punctuation_still_names_a_file():
    assert bookexport.filename_for("") == "libro.epub"
    assert bookexport.ascii_filename_for("¿¡") == "libro.epub"


def test_a_title_with_separators_in_it_cannot_become_a_path():
    assert "/" not in bookexport.filename_for("a/b")
    assert "\\" not in bookexport.filename_for("a\\b")


# --- when a model is worth asking ------------------------------------------


def test_a_document_the_catalog_knows_nothing_about_is_worth_asking_about():
    assert booking.needs_metadata(None, "01_RetoDeDios", "01_RetoDeDios")


def test_a_title_somebody_chose_is_never_second_guessed():
    """The test is exact rather than heuristic on purpose: anything that is not
    *precisely* the stem the import derived is a value a person or an earlier
    call settled, and paying a model to disagree with it is the failure this
    predicate exists to prevent."""
    assert not booking.needs_metadata("Calvino", "Institución", "01_RetoDeDios")
    assert booking.needs_metadata(None, "Institución", "01_RetoDeDios")
    assert booking.needs_metadata("Calvino", "01_RetoDeDios", "01_RetoDeDios")


def test_the_stem_is_derived_the_way_the_import_derived_it():
    """`stage_source` computes the placeholder title with
    `PurePosixPath(source_key).stem`; a second spelling of that rule is how the
    two would drift and the model would be paid to re-title a named book."""
    assert booking.filename_title("libros/01_RetoDeDios.pdf") == "01_RetoDeDios"
    assert booking.filename_title("charla.corrected.txt") == "charla.corrected"
    assert booking.filename_title("") == ""
