"""What a run reports about the claims it produced.

The counts are read from the run's own `semantics.json`, not from a catalog
column, and that is the property under test: a run written before claims carried
a verified quote must report zero rather than having a number invented for it by
a migration.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("fastapi.testclient")

from brainworker.api import main  # noqa: E402
from brainworker.artifacts import ArtifactStore  # noqa: E402

RUN = "ingest-1787000000000-abcdef01"


def _write(workspace: pathlib.Path, claims: list[dict], concepts: list[dict] | None = None):
    """Write a real artifact and hand back the catalog rows describing it."""
    ref = ArtifactStore(workspace, RUN).write_json(
        "semantics",
        {"extractor_model": "m", "concepts": concepts or [], "claims": claims,
         "edges": []},
    )
    return [{"name": ref.kind, "rel_path": ref.path, "sha256": ref.sha256,
             "size_bytes": ref.bytes}]


def test_a_run_reports_how_many_of_its_claims_can_be_checked(tmp_path: pathlib.Path):
    artifacts = _write(tmp_path, [
        {"text": "con cita", "confidence": 0.9, "source_chunk_id": "chk_1",
         "quote": "las palabras del documento"},
        {"text": "sin cita", "confidence": 0.8, "source_chunk_id": "chk_1"},
    ], concepts=[{"name": "Providencia"}])

    assert main._semantics_counts(tmp_path, RUN, artifacts) == {
        "concepts": 1, "claims": 2, "claims_verified": 1,
    }


def test_a_run_written_before_quotes_existed_reports_zero_honestly(
    tmp_path: pathlib.Path,
):
    """No migration can invent this number, and zero is the true one: none of
    those claims carries a quote anybody can check."""
    artifacts = _write(tmp_path, [
        {"text": "un claim viejo", "confidence": 0.8, "source_chunk_id": "chk_1"},
    ])
    counts = main._semantics_counts(tmp_path, RUN, artifacts)
    assert counts == {"concepts": 0, "claims": 1, "claims_verified": 0}


def test_a_structure_only_run_reports_nothing_rather_than_zero(
    tmp_path: pathlib.Path,
):
    """"This run extracted no semantics" and "0 of 0 claims are verifiable" are
    different statements. Only the first is true of a structure-only run."""
    assert main._semantics_counts(tmp_path, RUN, [{"name": "chunks"}]) is None


def test_a_pruned_workspace_still_renders_a_status(tmp_path: pathlib.Path):
    """`infra/backup.sh` found 79 artifacts whose files were gone. A status page
    that raised on one of them would be unopenable exactly when it is needed."""
    missing = [{"name": "semantics", "rel_path": f"runs/{RUN}/semantics.json",
                "sha256": "0" * 64, "size_bytes": 10}]
    assert main._semantics_counts(tmp_path, RUN, missing) is None


def test_a_tampered_artifact_is_refused_rather_than_counted(tmp_path: pathlib.Path):
    """The read verifies the sha256 the catalog recorded. A file that no longer
    matches it is not a source of counts."""
    artifacts = _write(tmp_path, [{"text": "x", "confidence": 0.5,
                                   "source_chunk_id": "chk_1", "quote": "x"}])
    path = tmp_path / artifacts[0]["rel_path"]
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")

    assert main._semantics_counts(tmp_path, RUN, artifacts) is None


# -- whether a rebuild could actually read what it needs --------------------


class FakeCatalog:
    """Answers with the artifact rows a run recorded, and nothing else."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def artifacts(self, run_id: str) -> list[dict]:
        return self.rows


def test_a_rebuild_is_offered_only_when_the_file_is_where_the_row_says(
    tmp_path: pathlib.Path,
):
    """The catalog row proves a run *recorded* chunks, not that they are still
    readable. This endpoint exists to keep a button that fails when pressed from
    being offered, and it used to trust the row."""
    rel = "runs/r1/chunks.jsonl"
    (tmp_path / "runs" / "r1").mkdir(parents=True)
    (tmp_path / rel).write_text("{}\n", encoding="utf-8")
    catalog = FakeCatalog([{"name": "chunks", "rel_path": rel}])

    assert main._replayable(tmp_path, catalog, "r1") is True


def test_a_row_whose_file_is_gone_does_not_offer_a_rebuild(tmp_path: pathlib.Path):
    """Two ways the row outlives the file, both seen: a pruned run directory —
    `infra/backup.sh` found 79 — and a catalog holding rows for runs written
    under a different workspace, which is what a real rebuild hit on
    2026-08-21."""
    catalog = FakeCatalog([{"name": "chunks", "rel_path": "runs/r1/chunks.jsonl"}])
    assert main._replayable(tmp_path, catalog, "r1") is False


def test_a_run_that_recorded_no_chunks_offers_no_rebuild(tmp_path: pathlib.Path):
    catalog = FakeCatalog([{"name": "semantics", "rel_path": "runs/r1/semantics.json"}])
    assert main._replayable(tmp_path, catalog, "r1") is False
    assert main._replayable(tmp_path, catalog, None) is False


def test_the_missing_artifact_error_says_which_workspace_it_looked_in(
    tmp_path: pathlib.Path,
):
    """The interesting failure is not "pruned" but "it is under the *other*
    workspace". A message carrying only the relative path sends the reader
    looking for a deleted file that is sitting right there."""
    from brainworker.artifacts import ArtifactError, ArtifactRef, ArtifactStore

    store = ArtifactStore(tmp_path, "r1")
    ref = ArtifactRef(kind="chunks", path="runs/r1/chunks.jsonl",
                      sha256="0" * 64, bytes=1)
    with pytest.raises(ArtifactError) as caught:
        store.resolve(ref)
    assert str(tmp_path) in str(caught.value)
