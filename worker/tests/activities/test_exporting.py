"""The packaging stage, against a real workspace and no catalog.

The database is pointed at a closed port on purpose, the same arrangement
`test_ingest.py` explains: artifact recording is best-effort bookkeeping, so
running with the catalog down both keeps these fast and continuously proves the
stage produces its file without one.
"""

from __future__ import annotations

import io
import pathlib
import zipfile

import pytest

from brainworker.activities import exporting
from brainworker.artifacts import ArtifactStore
from brainworker.pipeline import Registered


def chunk(**kw) -> dict:
    base = {"chapter": "", "section": "", "kind": "cuerpo", "text": "Texto.",
            "overlap": "", "context": ""}
    return {**base, **kw}


@pytest.fixture
def workspace(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv("BRAIN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAIN_DATABASE_URL", "postgresql://brain:x@127.0.0.1:1/brain")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def registered() -> Registered:
    return Registered(
        document_id="doc_1", version_id="ver_1", created=True, already_indexed=False
    )


def chunks_for(workspace: pathlib.Path, run_id: str, rows: list[dict]):
    return ArtifactStore(workspace, run_id).write_jsonl("chunks", rows)


async def test_the_book_lands_in_the_workspace_with_the_catalog_down(workspace):
    """The catalog here refuses every connection. A stage whose artifact
    depended on bookkeeping would produce nothing at all."""
    chunks = chunks_for(workspace, "run_1", [chunk(chapter="Uno", text="Hola.")])
    ref = await exporting.build_epub("run_1", "lib_x", registered(), chunks)

    assert ref.kind == "epub"
    assert (workspace / ref.path).is_file()
    assert zipfile.ZipFile(io.BytesIO((workspace / ref.path).read_bytes())).testzip() is None


async def test_a_document_with_no_catalog_row_still_gets_a_book(workspace):
    """The title is the one thing the catalog holds that this stage wants, and
    losing it must cost the book its name rather than its existence."""
    chunks = chunks_for(workspace, "run_2", [chunk(text="Sin catálogo.")])
    ref = await exporting.build_epub("run_2", "lib_x", registered(), chunks)
    assert (workspace / ref.path).is_file()


async def test_the_reference_describes_the_bytes_that_were_written(workspace):
    """`read_bytes` verifies the digest on every download, so a reference that
    disagreed with the file would make the book unfetchable the moment it was
    built."""
    chunks = chunks_for(workspace, "run_3", [chunk(text="Uno.")])
    ref = await exporting.build_epub("run_3", "lib_x", registered(), chunks)
    store = ArtifactStore(workspace, "run_3")
    assert store.read_bytes(ref)  # raises ContentChanged if it disagrees
    assert ref.bytes == (workspace / ref.path).stat().st_size


async def test_building_twice_converges_rather_than_appending(workspace):
    """A Temporal retry runs the whole activity again. `write_bytes` replaces,
    and the archive is deterministic, so the second attempt produces the file
    the first one did rather than a second book."""
    chunks = chunks_for(workspace, "run_4", [chunk(chapter="A", text="Uno.")])
    first = await exporting.build_epub("run_4", "lib_x", registered(), chunks)
    second = await exporting.build_epub("run_4", "lib_x", registered(), chunks)
    assert first == second


async def test_nothing_is_asked_of_a_provider_that_is_not_configured(workspace):
    """A worker with no Vertex project must still package a book. The metadata
    call is the only thing that needs one, and it declines rather than failing
    the stage."""
    chunks = chunks_for(workspace, "run_5", [chunk(text="Uno.")])
    assert await exporting.resolve_book_metadata("run_5", registered(), chunks) is None


async def test_a_catalog_that_is_merely_down_does_not_stall_the_stage(workspace):
    """Measured, because the failure is invisible otherwise: with a *pooled*
    connection these same five tests took 50 seconds instead of half of one.

    A pool retries a refused connection in the background, so a catalog that is
    down turns one immediate error into a full ten-second timeout — per read, on
    a stage that has a perfectly good answer without it. The bound is generous
    on purpose: what it catches is a pool, not a slow machine.
    """
    import time

    chunks = chunks_for(workspace, "run_6", [chunk(text="Uno.")])
    started = time.monotonic()
    await exporting.build_epub("run_6", "lib_x", registered(), chunks)
    assert time.monotonic() - started < 5.0
