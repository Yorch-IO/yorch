"""Replaying a run's semantics, including runs older than the fields it now writes.

The free path back from a dropped graph is the whole reason `semantics.json` is
kept, and it has to keep working for everything already indexed. A field added to
the artifact must therefore be optional on the way *in*, not only on the way out.
"""

from __future__ import annotations

import pathlib

import pytest

from brainworker.activities import rebuild
from brainworker.pipeline import Registered


@pytest.fixture
def workspace(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv("BRAIN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAIN_DATABASE_URL", "postgresql://brain:x@127.0.0.1:1/brain")
    monkeypatch.setenv("BRAIN_GEMINI_PROJECT_ID", "proj-test")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _fake_graph(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    class FakeGraph:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def ensure_schema(self): pass

    monkeypatch.setattr(rebuild, "Graph", lambda url: FakeGraph())
    monkeypatch.setattr(rebuild.proj, "project_concepts",
                        lambda g, c: captured.setdefault("concepts", c) and 0 or len(c))
    monkeypatch.setattr(rebuild.proj, "project_claims",
                        lambda g, c: captured.setdefault("claims", c) and 0 or len(c))
    monkeypatch.setattr(rebuild.proj, "project_semantic_edges",
                        lambda g, e: captured.setdefault("edges", e) and 0 or len(e))
    return captured


def _artifact(workspace: pathlib.Path, run_id: str, payload: dict):
    from brainworker.artifacts import ArtifactStore

    return ArtifactStore(workspace, run_id).write_json("semantics", payload)


async def test_an_artifact_written_before_quotes_existed_still_replays_free(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """The shape `semantics.json` had before claims carried a quote or a state.

    It must project exactly as it used to, and it must report zero verified
    claims rather than claiming credit for spans it does not have.
    """
    ref = _artifact(workspace, "run_old", {
        "extractor_model": "gemini-3.6-flash",
        "concepts": [{"name": "Providencia", "type": "doctrina"}],
        "claims": [{"text": "Una afirmación vieja.", "confidence": 0.8,
                    "source_chunk_id": "chk_" + "a" * 24}],
        "edges": [],
    })
    captured = _fake_graph(monkeypatch)

    registered = Registered(document_id="doc_" + "a" * 24,
                            version_id="ver_" + "a" * 24,
                            created=False, already_indexed=True)
    result = await rebuild.replay_semantics("run_old", registered, ref)

    assert result.claims == 1
    assert result.claims_verified == 0, "no quote means no credit for one"
    assert result.spend.usd == 0.0, "a file read and a graph write"
    assert "quote" not in captured["claims"][0]
    assert captured["concepts"][0]["name"] == "Providencia"


async def test_a_replay_reports_the_quotes_the_artifact_does_carry(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Counted from the artifact rather than assumed, so a rebuild of a run that
    did verify its quotes says so — and one that did not cannot borrow the claim."""
    ref = _artifact(workspace, "run_new", {
        "extractor_model": "gemini-3.6-flash",
        "concepts": [{"name": "Providencia", "descriptions": ["El gobierno de Dios."]}],
        "claims": [
            {"text": "Con cita.", "confidence": 0.8,
             "source_chunk_id": "chk_" + "a" * 24, "quote": "gobierno de Dios",
             "quote_char_start": 10, "quote_char_end": 26, "status": "afirma"},
            {"text": "Sin cita.", "confidence": 0.7,
             "source_chunk_id": "chk_" + "a" * 24, "status": "atribuido"},
        ],
        "edges": [],
    })
    captured = _fake_graph(monkeypatch)

    registered = Registered(document_id="doc_" + "b" * 24,
                            version_id="ver_" + "b" * 24,
                            created=False, already_indexed=True)
    result = await rebuild.replay_semantics("run_new", registered, ref)

    assert (result.claims, result.claims_verified) == (2, 1)
    # The descriptions ride along, so a rebuild restores a condensed graph's
    # inputs without re-extracting anything.
    assert captured["concepts"][0]["descriptions"] == ["El gobierno de Dios."]
