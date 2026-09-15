"""Handing a book over, and correcting what the catalog calls a document.

The download is the first route on this plane that serves bytes, and the two
properties worth asserting are refusals: what may be fetched at all, and what
happens when the file no longer matches the row describing it. This plane has no
authentication — it is safe only because nothing off the machine can route to it
— so the allowlist is the whole of that decision.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from brainworker import epub  # noqa: E402
from brainworker.api import main  # noqa: E402
from brainworker.artifacts import ArtifactStore  # noqa: E402
from brainworker.catalog.repo import Document, RunSummary  # noqa: E402

from datetime import datetime, timezone  # noqa: E402

RUN = "epub-1787000000000-abcdef01"
T0 = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    class _Settings:
        database_url = "postgresql://nowhere/none"
        workspace = tmp_path

    monkeypatch.setattr(main, "settings", lambda: _Settings())
    return _Settings


def _document(**kw) -> Document:
    base = dict(
        id="doc_1", library_id="lib_x", folder_id=None, source_key="libro.pdf",
        title="01_RetoDeDios", author=None, format="pdf", present=True,
        absent_since=None, tags=[], created_at=T0, updated_at=T0,
        source_path="/workspace/inbox/libro.pdf",
    )
    base.update(kw)
    return Document(**base)


def _run(title="Institución") -> RunSummary:
    return RunSummary(
        id=RUN, workflow_id=RUN, kind="epub", state="succeeded", stage="done",
        started_at=T0, finished_at=T0, error_kind=None, error_detail=None,
        title=title, library_id="lib_x", label=None, document_id="doc_1",
        version_id="ver_1", usd_so_far=0.0,
    )


class _FakeCatalog:
    def __init__(self, artifacts=(), run=None, document=None):
        self._artifacts, self._run, self._document = list(artifacts), run, document
        self.written: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def artifacts(self, run_id):
        return self._artifacts

    def run(self, run_id, *, tenant_id):
        return self._run

    def document(self, document_id, *, library_id=None, tenant_id=None):
        return self._document

    def set_document_metadata(self, document_id, *, title=None, author=None, tenant_id):
        self.written.append({"title": title, "author": author, "tenant": tenant_id})
        if self._document is not None:
            from dataclasses import replace

            self._document = replace(
                self._document,
                title=title if title is not None else self._document.title,
                author=author if author is not None else self._document.author,
            )
        return True


def _catalog(monkeypatch, catalog):
    monkeypatch.setattr(main, "Catalog", lambda *_, **__: catalog)


def _book(workspace: pathlib.Path, run_id: str = RUN):
    data = epub.build(
        epub.Book("Institución", "Calvino", "es", epub.identifier_for("ver_1"), [])
    )
    ref = ArtifactStore(workspace, run_id).write_bytes("epub", data)
    return ref, [{"name": "epub", "rel_path": ref.path, "sha256": ref.sha256,
                  "size_bytes": ref.bytes}]


# -- the download -----------------------------------------------------------


def test_the_book_comes_back_as_an_epub_a_reader_can_open(client, workspace, monkeypatch):
    ref, rows = _book(workspace)
    _catalog(monkeypatch, _FakeCatalog(artifacts=rows, run=_run()))

    r = client.get(f"/runs/{RUN}/artifacts/epub")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/epub+zip"
    assert r.content == (workspace / ref.path).read_bytes()


def test_it_is_offered_as_a_download_named_after_the_document(client, workspace, monkeypatch):
    _, rows = _book(workspace)
    _catalog(monkeypatch, _FakeCatalog(artifacts=rows, run=_run("Teología y república")))

    disposition = client.get(f"/runs/{RUN}/artifacts/epub").headers["content-disposition"]
    assert disposition.startswith("attachment;")
    # Both forms: an HTTP header is Latin-1 and «Teología» is not, so the
    # accented name rides in RFC 5987's `filename*` and the ASCII one is what an
    # older client reads. Sending only the first renames the book.
    assert "filename=" in disposition
    assert "filename*=UTF-8''" in disposition
    assert disposition.isascii()


def test_a_run_whose_document_is_gone_still_names_the_file_something(
    client, workspace, monkeypatch
):
    """`run.document_id` is ON DELETE SET NULL because cost history outlives the
    document. The artifact outlives it too."""
    _, rows = _book(workspace)
    _catalog(monkeypatch, _FakeCatalog(artifacts=rows, run=_run(title=None)))

    disposition = client.get(f"/runs/{RUN}/artifacts/epub").headers["content-disposition"]
    assert RUN in disposition


def test_no_artifact_but_the_book_can_be_fetched(client, workspace, monkeypatch):
    """The allowlist is the whole of this plane's protection for the workspace.

    Every other kind is a working file — the text streams, `semantics.json`, an
    eval set with the corpus's own questions in it — and this plane has no
    authentication. A generic `/artifacts/{name}` would make a misconfigured
    port the only thing between a stranger and the corpus.
    """
    _catalog(monkeypatch, _FakeCatalog(artifacts=[
        {"name": "semantics", "rel_path": f"runs/{RUN}/semantics.json",
         "sha256": "a" * 64, "size_bytes": 10},
    ], run=_run()))

    r = client.get(f"/runs/{RUN}/artifacts/semantics")
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "artifact_not_found"


def test_a_run_that_produced_no_book_says_so_rather_than_500(client, monkeypatch):
    _catalog(monkeypatch, _FakeCatalog(artifacts=[], run=_run()))
    r = client.get(f"/runs/{RUN}/artifacts/epub")
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "artifact_not_found"


def test_a_file_that_no_longer_matches_its_row_is_refused(client, workspace, monkeypatch):
    """The one thing that distinguishes this from a static mount. `run_artifact`
    records what the run wrote, and a file that no longer hashes to it is not the
    book the catalog is describing — serving it anyway is the quiet kind of
    wrong: a well-formed answer about the wrong thing.
    """
    ref, rows = _book(workspace)
    (workspace / ref.path).write_bytes(b"PK\x03\x04 otra cosa")
    _catalog(monkeypatch, _FakeCatalog(artifacts=rows, run=_run()))

    r = client.get(f"/runs/{RUN}/artifacts/epub")
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "artifact_changed"


def test_a_row_whose_file_was_pruned_is_refused_the_same_way(
    client, workspace, monkeypatch
):
    ref, rows = _book(workspace)
    (workspace / ref.path).unlink()
    _catalog(monkeypatch, _FakeCatalog(artifacts=rows, run=_run()))

    r = client.get(f"/runs/{RUN}/artifacts/epub")
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "artifact_changed"


# -- correcting the metadata ------------------------------------------------


def test_a_person_can_name_a_document_the_import_could_only_guess_at(client, monkeypatch):
    catalog = _FakeCatalog(document=_document())
    _catalog(monkeypatch, catalog)

    r = client.patch(
        "/libraries/lib_x/documents/doc_1",
        json={"title": "El reto de Dios", "author": "Darío Silva-Silva"},
    )
    assert r.status_code == 200
    assert r.json() == {
        "id": "doc_1", "title": "El reto de Dios", "author": "Darío Silva-Silva",
    }
    assert catalog.written[0]["tenant"] == main.LEGACY_TENANT_ID


def test_one_field_may_be_sent_without_clearing_the_other(client, monkeypatch):
    """`None` means "leave this alone", which is what lets a screen send the
    field somebody edited rather than the whole row."""
    catalog = _FakeCatalog(document=_document(title="Institución", author="Calvino"))
    _catalog(monkeypatch, catalog)

    r = client.patch("/libraries/lib_x/documents/doc_1", json={"author": "Juan Calvino"})
    assert r.status_code == 200
    assert catalog.written[0] == {
        "title": None, "author": "Juan Calvino", "tenant": main.LEGACY_TENANT_ID,
    }


def test_an_oversized_title_is_refused_by_the_frameworks_own_validation(
    client, monkeypatch
):
    """A field constraint rather than a hand-raised kind, so that both planes
    answer an oversized body with the same 422-and-a-list. The paid plane's
    filter reproduces that shape for `class-validator` on purpose; a hand-rolled
    kind here would make the two answer one bad request two different ways."""
    _catalog(monkeypatch, _FakeCatalog(document=_document()))
    r = client.patch(
        "/libraries/lib_x/documents/doc_1",
        json={"title": "x" * (main.MAX_TITLE_CHARS + 1)},
    )
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


def test_a_document_in_another_library_is_not_found_rather_than_forbidden(
    client, monkeypatch
):
    _catalog(monkeypatch, _FakeCatalog(document=None))
    r = client.patch("/libraries/lib_otra/documents/doc_1", json={"title": "X"})
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "document_not_found"
