"""The order removal writes in, proved by failing half way through it.

The claim this module exists to check is not "removal works" — the live-store
tests in `tests/catalog` and `tests/graph` do that. It is that a removal
interrupted between two stores leaves the *catalog* intact, because the catalog
is the only thing that can find the leftovers again and retry.

Fakes rather than live services on purpose: the interesting run is the one where
a store fails, and that is not something a healthy stack will do on request.
"""

from __future__ import annotations

import pytest

from brainworker import config, removal
from brainworker.catalog import Document


class FakeQdrant:
    def __init__(self) -> None:
        self.deleted: list[dict[str, str]] = []
        self.repointed: list[tuple[dict[str, str], dict[str, str]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exists(self) -> bool:
        return True

    def delete_by_filter(self, filters):
        self.deleted.append(filters)
        return 7

    def set_payload(self, filters, payload):
        self.repointed.append((filters, payload))
        return 7


class FakeCatalog:
    """Reads answer; writes are recorded so the test can assert none happened."""

    def __init__(self, *, holders: dict[str, list[str]], removed: list[str]) -> None:
        self._holders = holders
        self.removed = removed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def document(self, document_id, *, library_id=None):
        if document_id not in {h for hs in self._holders.values() for h in hs}:
            return None
        return Document(
            id=document_id, library_id=library_id or "lib_1", folder_id=None,
            source_key="libros/a.pdf", title="A", author=None, format="pdf",
            present=True, absent_since=None, tags=[], created_at=None,
            updated_at=None, source_path="/workspace/inbox/libros/a.pdf",
        )

    def versions_of(self, document_id):
        class V:
            def __init__(self, vid):
                self.id = vid

        return [V(v) for v, hs in self._holders.items() if document_id in hs]

    def documents_holding(self, version_id):
        return list(self._holders.get(version_id, []))

    def remove_document(self, document_id, *, library_id=None):
        self.removed.append(document_id)
        return {"documents": 1, "versions": 1}

    def remove_version(self, version_id):
        self.removed.append(version_id)
        return {"versions": 1, "links": 1, "active_pointers_cleared": 0}


class ExplodingGraph:
    def __init__(self, *a, **k):
        raise RuntimeError("memgraph went away mid-removal")


class FakeGraph:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def settings(tmp_path) -> config.Settings:
    return config.Settings(
        workspace=tmp_path, temporal_target="", temporal_namespace="default",
        task_queue="q", qdrant_url="http://127.0.0.1:1", qdrant_collection="brain_test",
        memgraph_url="bolt://127.0.0.1:1", database_url="postgresql://x/y",
        log_level="INFO", secrets_file=tmp_path / "s.env",
    )


def _wire(monkeypatch, *, holders, removed, graph):
    fake_q = FakeQdrant()
    monkeypatch.setattr(removal, "_qdrant", lambda _s: fake_q)
    monkeypatch.setattr(removal, "Graph", graph)
    monkeypatch.setattr(
        removal, "Catalog",
        lambda *a, **k: FakeCatalog(holders=holders, removed=removed),
    )
    return fake_q


def test_a_failure_after_qdrant_leaves_the_catalog_naming_the_document(
    monkeypatch, settings
):
    """The whole reason for the order. If the catalog row went first, these
    points would now be unreachable: nothing references them and nothing knows
    to come back for them."""
    removed: list[str] = []
    fake_q = _wire(
        monkeypatch, holders={"ver_1": ["doc_1"]}, removed=removed,
        graph=ExplodingGraph,
    )

    with pytest.raises(RuntimeError, match="memgraph went away"):
        removal.remove_document(settings, library_id="lib_1", document_id="doc_1")

    assert fake_q.deleted == [{"library_id": "lib_1", "version_id": "ver_1"}]
    assert removed == [], "the catalog must still name the document for a retry"


def test_the_retry_completes(monkeypatch, settings):
    """Removal is idempotent by construction: the second pass finds the points
    already gone and finishes the job."""
    removed: list[str] = []
    _wire(monkeypatch, holders={"ver_1": ["doc_1"]}, removed=removed, graph=FakeGraph)
    monkeypatch.setattr(
        removal.proj, "remove_document",
        lambda _g, _d: removal.proj.Removed(documents=1, versions=1),
    )

    result = removal.remove_document(settings, library_id="lib_1", document_id="doc_1")

    assert removed == ["doc_1"]
    assert result.versions_removed == ["ver_1"]
    assert result.catalog == {"documents": 1, "versions": 1}


def test_a_shared_version_is_repointed_rather_than_deleted(monkeypatch, settings):
    """Two documents, one version. The points stay; their `document_id` payload
    must stop naming the document being removed."""
    removed: list[str] = []
    fake_q = _wire(
        monkeypatch, holders={"ver_1": ["doc_1", "doc_2"]}, removed=removed,
        graph=FakeGraph,
    )
    monkeypatch.setattr(
        removal.proj, "remove_document",
        lambda _g, _d: removal.proj.Removed(documents=1),
    )

    result = removal.remove_document(settings, library_id="lib_1", document_id="doc_1")

    assert fake_q.deleted == [], "the other document still holds these bytes"
    assert fake_q.repointed == [
        (
            {"library_id": "lib_1", "version_id": "ver_1", "document_id": "doc_1"},
            {"document_id": "doc_2"},
        )
    ]
    assert result.versions_kept == ["ver_1"] and result.versions_removed == []


def test_an_unknown_document_is_refused_before_anything_is_deleted(
    monkeypatch, settings
):
    removed: list[str] = []
    fake_q = _wire(monkeypatch, holders={}, removed=removed, graph=ExplodingGraph)

    with pytest.raises(removal.RemovalError) as e:
        removal.remove_document(settings, library_id="lib_1", document_id="doc_x")

    assert e.value.kind == "document_not_found"
    assert fake_q.deleted == [] and removed == []
