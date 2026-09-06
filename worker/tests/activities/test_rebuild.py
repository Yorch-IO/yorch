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
        # Graph-shaped rather than stubbed away: `prune_semantics` runs for
        # real against this and takes its early return, so an arity or a name
        # it got wrong still fails here. Stubbing the function out instead is
        # how `_condense_descriptions` kept four green tests over a call that
        # would have raised the first time it ran.
        def write(self, *a, **k): return []
        def write_many(self, *a, **k): return None

    monkeypatch.setattr(rebuild, "Graph", lambda url: FakeGraph())
    monkeypatch.setattr(rebuild.proj, "project_concepts",
                        lambda g, c, **k: captured.setdefault("concepts", c) and 0 or len(c))
    monkeypatch.setattr(rebuild.proj, "project_claims",
                        lambda g, c, **k: captured.setdefault("claims", c) and 0 or len(c))
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



# -- whose document a rebuild replays ----------------------------------------

ACME = "tnt_" + "9" * 24


class _CatalogWithOneDocument:
    """Just enough catalog for `load_rebuild_inputs`.

    A double rather than Postgres because what is under test is the
    `IngestRequest` this activity *builds*, not any query it runs — and the
    fields that matter come straight off the row it is handed.
    """

    def __init__(self, tenant: str):
        self.tenant = tenant
        self.started: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def document(self, document_id, *, library_id, tenant_id):
        assert tenant_id == self.tenant, "ownership is resolved before anything else"
        from types import SimpleNamespace

        return SimpleNamespace(
            id=document_id, library_id=library_id, tenant_id=self.tenant,
            source_path="/workspace/x.pdf", source_key="libros/x.pdf",
            title="Un libro", author=None, format="pdf", folder_id=None,
        )

    def active_version(self, _document_id):
        return "ver_x"

    def versions_of(self, _document_id):
        from types import SimpleNamespace

        return [SimpleNamespace(id="ver_x", content_sha256="a" * 64, byte_size=10)]

    def latest_run_with_artifact(self, *_a, **_k):
        return "run_src"

    def start_run(self, **kw):
        self.started = kw


async def test_a_rebuild_replays_into_the_organisation_the_document_belongs_to(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """The `IngestRequest` a rebuild reassembles is what `project_structure`
    reads the tenant off. It left the field out, so a paying organisation's
    document, sections, chunks and citations were re-projected into the legacy
    graph under an unsalted version id — and because retrieval still returned
    the right text, only with no locator and no claim, it looked exactly like a
    graph that had not been built yet rather than like a leak.
    """
    from brainworker.artifacts import ArtifactStore

    store = ArtifactStore(workspace, "run_src")
    chunks = store.write_jsonl(
        "chunks", [{"index": 0, "kind": "cuerpo", "text": "Uno.",
                    "char_from": 0, "char_to": 4}]
    )
    catalog = _CatalogWithOneDocument(ACME)
    monkeypatch.setattr(rebuild, "Catalog", lambda *_a, **_k: catalog)
    row = {"name": rebuild.REQUIRED_ARTIFACT, "rel_path": chunks.path,
           "sha256": chunks.sha256, "size_bytes": chunks.bytes}
    monkeypatch.setattr(
        rebuild, "_artifact_of",
        lambda _c, _r, name: row if name == rebuild.REQUIRED_ARTIFACT else None,
    )


    inputs = await rebuild.load_rebuild_inputs(
        "lib_1", "doc_1", "run_new", "rebuild-1", ACME
    )

    assert inputs.request.tenant_id == ACME
    assert inputs.registered.tenant_id == ACME
    # And the run is filed against the same organisation, not whoever pressed
    # the button.
    assert catalog.started["tenant_id"] == ACME


# -- the ledger's account of a free stage ------------------------------------


async def test_the_replay_books_a_row_saying_the_stage_cost_nothing(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """A stage that ran for nothing and a stage that did not run are different
    facts, and until now they rendered identically.

    `replay_semantics` built a zero-token `Spend` under a comment claiming it
    "keeps the stage visible in the run's ledger" and never wrote it, so a
    rebuild's audit view showed `replaying semantics` with an empty cost cell —
    which reads as "not measured", not as "free". It really is free: a file read
    and a graph write, no provider call and nothing to price. Saying so is the
    fix, and it is the same rule the cached query embedding already books a
    zero row for.
    """
    _fake_graph(monkeypatch)
    charged: list = []
    monkeypatch.setattr(rebuild, "_charge", lambda run_id, spend: charged.append((run_id, spend)) or spend)

    ref = _artifact(workspace, "run_free", {
        "extractor_model": "gemini-3.6-flash",
        "concepts": [{"name": "Gracia"}],
        "claims": [],
        "edges": [],
    })
    await rebuild.replay_semantics(
        "run_free",
        Registered(document_id="doc_1", version_id="ver_1", created=True,
                   already_indexed=False),
        ref,
    )
    assert [run for run, _ in charged] == ["run_free"]
    spend = charged[0][1]
    assert spend.stage == "semantics-replay"
    assert spend.usd == 0.0 and spend.input_tokens == 0 and spend.output_tokens == 0
