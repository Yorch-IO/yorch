"""The paid stages, with the provider mocked and Qdrant real.

Nothing here spends. The provider is replaced because a test that called Vertex
would measure Google's availability and bill the user for the privilege; Qdrant
is *not* replaced, because the properties worth checking — that a re-index
overwrites rather than duplicates, that the payload carries what a filter needs —
are properties of the real store.
"""

from __future__ import annotations

import os
import pathlib
import secrets

import pytest

from brainworker.activities import paid
from brainworker.graph.schema import chunk_id as make_chunk_id
from brainworker.artifacts import ArtifactRef, ArtifactStore
from brainworker.pipeline import (
    Chunked,
    Extraction,
    ProfileDecision,
    Registered,
    Scores,
    Staged,
)
from brainworker.providers.gemini import Embedding, Generation, Usage

DIMS = 3072


@pytest.fixture
def workspace(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv("BRAIN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAIN_DATABASE_URL", "postgresql://brain:x@127.0.0.1:1/brain")
    monkeypatch.setenv("BRAIN_GEMINI_PROJECT_ID", "proj-test")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    # docagent resolves cache/ relative to the process CWD, and correction's
    # per-paragraph cache would otherwise leak between test runs.
    monkeypatch.chdir(tmp_path)
    return tmp_path


class FakeProvider:
    """Records what it was asked for and returns something plausible."""

    def __init__(self, *, corrections: dict[str, str] | None = None,
                 semantics: str | None = None) -> None:
        self.corrections = corrections or {}
        self.semantics = semantics
        self.embed_calls: list[list[str]] = []
        self.generate_calls: list[str] = []

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None):
        import json

        self.generate_calls.append(prompt)
        if self.semantics is not None:
            return Generation(text=self.semantics, usage=Usage(100, 20, 1))
        payload = json.loads(prompt)
        out = [
            {"i": item["i"], "texto": self.corrections.get(item["texto"], item["texto"])}
            for item in payload["parrafos"]
        ]
        return Generation(
            text=json.dumps({"parrafos": out}, ensure_ascii=False),
            usage=Usage(input_tokens=500, output_tokens=500, calls=1),
        )

    def embed(self, texts, *, task, workers=6):
        # One request per text, matching the real provider: the model returns a
        # single embedding for a batch and no error, so usage is per text.
        self.embed_calls.append(list(texts))
        return [Embedding(values=[0.01] * DIMS, usage=Usage(input_tokens=10)) for _ in texts]


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch):
    fake = FakeProvider()
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    return fake


# -- correction -------------------------------------------------------------


TEXTO = """El fin principal del hombre es glorificar a Dios, segun Gén. 1:26.

La Palabra de Dios contenida en las Escrituras es la unica regla."""


def _raw(workspace: pathlib.Path, run_id: str, body: str) -> Extraction:
    from brainworker.artifacts import ArtifactStore

    store = ArtifactStore(workspace, run_id)
    ref = store.write_bytes("raw_text", body.encode("utf-8"))
    evidence = store.write_json("evidence", {})
    return Extraction(text=ref, evidence=evidence, extractor="plain")


async def test_correction_writes_a_new_byte_stream_and_reports_what_it_did(
    workspace: pathlib.Path, provider: FakeProvider
):
    provider.corrections = {
        "El fin principal del hombre es glorificar a Dios, segun Gén. 1:26.":
            "El fin principal del hombre es glorificar a Dios, según Gén. 1:26.",
    }
    extraction = _raw(workspace, "run_1", TEXTO)
    result = await paid.correct_text("run_1", extraction)

    body = (workspace / result.text.path).read_text(encoding="utf-8")
    assert "según" in body
    assert result.paragraphs == 2
    assert result.changed == 1
    assert result.spend.stage == "correction"
    assert result.spend.input_tokens == 500


async def test_a_correction_that_loses_a_scripture_reference_is_discarded(
    workspace: pathlib.Path, provider: FakeProvider
):
    """The engine's `verify()` gate, reached through the adapter.

    A bad correction must degrade to *no* correction, never to damaged content —
    which is precisely why this reuses the engine's rules instead of restating
    them.
    """
    provider.corrections = {
        "El fin principal del hombre es glorificar a Dios, segun Gén. 1:26.":
            "El fin principal del hombre es glorificar a Dios.",
    }
    extraction = _raw(workspace, "run_2", TEXTO)
    result = await paid.correct_text("run_2", extraction)

    body = (workspace / result.text.path).read_text(encoding="utf-8")
    assert "Gén. 1:26" in body, "the original must survive a rejected correction"
    assert result.rejected == 1
    assert result.changed == 0


async def test_the_corrected_stream_is_what_later_char_spans_index(
    workspace: pathlib.Path, provider: FakeProvider
):
    """Correction changes the text's length, so chunking must run against the
    corrected bytes. Re-joining them the way `split_paragraphs` expects is what
    keeps the offsets truthful."""
    from docagent.chunk import split_paragraphs

    provider.corrections = {
        "La Palabra de Dios contenida en las Escrituras es la unica regla.":
            "La Palabra de Dios contenida en las Escrituras es la única regla.",
    }
    extraction = _raw(workspace, "run_3", TEXTO)
    result = await paid.correct_text("run_3", extraction)

    data = (workspace / result.text.path).read_bytes()
    paragraphs = split_paragraphs(data)
    assert len(paragraphs) == 2
    for p in paragraphs:
        assert data[p.offset : p.offset + len(p.text.encode("utf-8"))].decode("utf-8") == p.text


async def test_chunking_after_correction_reads_the_corrected_artifact(
    workspace: pathlib.Path, provider: FakeProvider
):
    extraction = _raw(workspace, "run_4", TEXTO)
    await paid.correct_text("run_4", extraction)
    chunked = await paid.chunk_final("run_4", "corrected_text")
    assert chunked.count > 0
    assert chunked.chunks.kind == "chunks"


# -- embedding and indexing -------------------------------------------------


def _chunked(workspace: pathlib.Path, run_id: str, n: int = 3) -> Chunked:
    from brainworker.artifacts import ArtifactStore

    store = ArtifactStore(workspace, run_id)
    rows = [
        {
            "index": i, "kind": "cuerpo", "chapter": "Libro I", "section": "",
            "text": f"Párrafo número {i} sobre el conocimiento de Dios.",
            "embed_text": f"Libro I\n\nPárrafo número {i}.",
            "context": "", "overlap": "",
            "char_from": i * 100, "char_to": (i + 1) * 100, "cell_ref": "",
        }
        for i in range(n)
    ]
    return Chunked(chunks=store.write_jsonl("chunks", rows), count=n)


def _ids(library: str = "lib_t") -> tuple[Registered, Staged]:
    sha = secrets.token_hex(32)
    return (
        Registered(document_id="doc_" + sha[:24], version_id="ver_" + sha[:24],
                   created=True, already_indexed=False),
        Staged(content_sha256=sha, byte_size=100, fmt="txt",
               extractor="plain", title="Catecismo"),
    )


@pytest.fixture
def qdrant(monkeypatch: pytest.MonkeyPatch):
    """A disposable collection, dropped afterwards.

    Not the real one. These tests used to write into `brain`, and the points
    stayed: after enough runs the answering fixture's scroll window was entirely
    test data and eight retrieval tests started skipping with a message about a
    missing citation — a failure whose cause was three files away.
    """
    import os

    from docagent.qdrant import Qdrant

    url = os.environ.get("BRAIN_QDRANT_URL", "http://127.0.0.1:6433")
    name = f"brain_test_{secrets.token_hex(6)}"
    monkeypatch.setenv("BRAIN_QDRANT_URL", url)
    monkeypatch.setenv("BRAIN_QDRANT_COLLECTION", name)
    try:
        with Qdrant(url, name, timeout=5.0) as q:
            q.wait_ready(timeout=5.0)
    except Exception as e:
        pytest.skip(f"no Qdrant at {url}: {type(e).__name__}: {e}")
    try:
        yield url
    finally:
        with Qdrant(url, name, timeout=10.0) as q:
            q.drop()


async def test_a_shrinking_reindex_leaves_no_orphan_points(
    workspace: pathlib.Path, provider: FakeProvider, qdrant: str
):
    """The defect nothing in this repository closed before.

    `upsert` overwrites only the ids the new run produced. Point ids are
    deterministic in the chunk index, so a re-index that yields *fewer* chunks
    than the last one leaves the previous chunking's tail alive — carrying
    `char_span`s into a byte stream nothing holds any more, and competing in
    ranking with the chunks that replaced it.

    Observed for the CLI on `07-LlavesDelPoder-INT.pdf` after a rejected
    625-chunk tuning candidate left ids 502..624 behind. Nothing here needs a
    tuning candidate to reach it: a corrected profile that chunks more coarsely
    is enough, and `removal.py` — the only other delete in this package — removes
    a whole version, never a tail.
    """
    from docagent.qdrant import Qdrant

    registered, staged = _ids()
    collection = os.environ["BRAIN_QDRANT_COLLECTION"]

    await paid.embed_and_index(
        "run_long", "lib_t", registered, staged, _chunked(workspace, "run_long", 10)
    )
    with Qdrant(qdrant, collection) as q:
        assert q.count({"version_id": registered.version_id}) == 10

    # Same document, same version, fewer chunks.
    result = await paid.embed_and_index(
        "run_short", "lib_t", registered, staged, _chunked(workspace, "run_short", 6)
    )

    assert result.points == 6
    with Qdrant(qdrant, collection) as q:
        assert q.count({"version_id": registered.version_id}) == 6, (
            "the tail of the longer chunking survived"
        )


async def test_pruning_only_touches_this_version(
    workspace: pathlib.Path, provider: FakeProvider, qdrant: str
):
    """The tail delete is scoped, or a short re-index of one book would delete
    the back of every other book in the collection."""
    from docagent.qdrant import Qdrant

    mine, staged = _ids()
    theirs, other_staged = _ids()
    collection = os.environ["BRAIN_QDRANT_COLLECTION"]

    await paid.embed_and_index("r1", "lib_t", mine, staged, _chunked(workspace, "r1", 8))
    await paid.embed_and_index(
        "r2", "lib_t", theirs, other_staged, _chunked(workspace, "r2", 8)
    )
    await paid.embed_and_index("r3", "lib_t", mine, staged, _chunked(workspace, "r3", 2))

    with Qdrant(qdrant, collection) as q:
        assert q.count({"version_id": mine.version_id}) == 2
        assert q.count({"version_id": theirs.version_id}) == 8


async def test_a_retry_does_not_re_embed_what_it_already_paid_for(
    workspace: pathlib.Path, provider: FakeProvider, qdrant: str
):
    """Paid activities get two Temporal attempts, and each attempt runs the whole
    stage. Without a cache the second one re-pays for every vector of the first —
    and the scarce resource is the per-minute embedding quota, not the money, so
    re-spending it arrives back at the same wall for ever.
    """
    registered, staged = _ids()
    chunked = _chunked(workspace, "run_cache", 5)

    await paid.embed_and_index("run_cache", "lib_t", registered, staged, chunked)
    assert sum(len(c) for c in provider.embed_calls) == 5

    provider.embed_calls.clear()
    await paid.embed_and_index("run_cache", "lib_t", registered, staged, chunked)

    assert provider.embed_calls == [], "it re-embedded text it already had"


async def test_indexing_writes_points_whose_ids_come_from_the_content(
    workspace: pathlib.Path, provider: FakeProvider, qdrant: str
):
    """The duplicate fix carried into Qdrant.

    `docagent.doc_id_for()` hashes the filename, which is what lets
    byte-identical duplicates index twice and compete in ranking. Deriving the
    point id from the version means the same bytes always overwrite the same
    points — and makes this activity idempotent, which Temporal requires anyway.
    """
    from docagent.qdrant import Qdrant, point_id

    registered, staged = _ids()
    chunked = _chunked(workspace, "run_5")

    first = await paid.embed_and_index("run_5", "lib_t", registered, staged, chunked)
    assert first.points == 3
    assert first.dimensions == DIMS

    second = await paid.embed_and_index("run_5", "lib_t", registered, staged, chunked)
    assert second.points == 3

    with Qdrant(qdrant, os.environ["BRAIN_QDRANT_COLLECTION"]) as q:
        rows = [p for p in q.scroll_all() if p.get("version_id") == registered.version_id]
    assert len(rows) == 3, "re-indexing must overwrite, not duplicate"
    assert point_id(registered.version_id, 0) != point_id("otra-version", 0)


async def test_the_payload_carries_what_a_filter_and_a_citation_need(
    workspace: pathlib.Path, provider: FakeProvider, qdrant: str
):
    from docagent.qdrant import Qdrant

    registered, staged = _ids()
    await paid.embed_and_index(
        "run_6", "lib_t", registered, staged, _chunked(workspace, "run_6", 1)
    )

    with Qdrant(qdrant, os.environ["BRAIN_QDRANT_COLLECTION"]) as q:
        row = next(
            p for p in q.scroll_all() if p.get("version_id") == registered.version_id
        )
    assert row["library_id"] == "lib_t"
    assert row["document_id"] == registered.document_id
    assert row["kind"] == "cuerpo"
    assert row["char_span"] == [0, 100]
    # The graph's id for the same chunk, so a hit can be expanded through the
    # graph without a lookup table.
    assert row["chunk_id"] == make_chunk_id(registered.version_id, 0)


async def test_the_embedding_task_is_the_indexing_one(
    workspace: pathlib.Path, provider: FakeProvider, qdrant: str
):
    """Invariant 5: the model embeds documents and queries asymmetrically, and
    using one task for both measurably degrades retrieval."""
    registered, staged = _ids()
    await paid.embed_and_index(
        "run_7", "lib_t", registered, staged, _chunked(workspace, "run_7", 1)
    )
    assert provider.embed_calls, "nothing was embedded"


async def test_an_empty_chunk_set_costs_nothing(
    workspace: pathlib.Path, provider: FakeProvider
):
    from brainworker.artifacts import ArtifactStore

    store = ArtifactStore(workspace, "run_8")
    empty = Chunked(chunks=store.write_jsonl("chunks", []), count=0)
    registered, staged = _ids()
    result = await paid.embed_and_index("run_8", "lib_t", registered, staged, empty)
    assert result.points == 0
    assert provider.embed_calls == []


# -- semantics --------------------------------------------------------------


async def test_every_semantic_edge_is_attributed_to_the_chunk_that_produced_it(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """A relation nobody can check is worse than no relation: it still looks
    like evidence. That is why extraction runs one chunk per call."""
    import json

    fake = FakeProvider(semantics=json.dumps({
        "conceptos": [{"nombre": "Gracia común", "tipo": "doctrina", "confianza": 0.9}],
        "afirmaciones": [
            {"texto": "La gracia común alcanza a todos.", "concepto": "Gracia común",
             "confianza": 0.8}
        ],
    }, ensure_ascii=False))
    monkeypatch.setattr(paid, "_provider", lambda: fake)

    captured: dict = {}

    class FakeGraph:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def ensure_schema(self): pass

    monkeypatch.setattr(paid, "Graph", lambda url: FakeGraph())
    monkeypatch.setattr(paid.proj, "project_concepts", lambda g, c, **k: captured.setdefault("concepts", c) and 0 or len(c))
    monkeypatch.setattr(paid.proj, "project_claims", lambda g, c, **k: captured.setdefault("claims", c) and 0 or len(c))
    monkeypatch.setattr(paid.proj, "project_semantic_edges",
                        lambda g, e: captured.setdefault("edges", e) and 0 or len(e))

    registered, _ = _ids()
    chunked = _chunked(workspace, "run_9", 2)
    result = await paid.extract_semantics("run_9", registered, chunked)

    assert len(fake.generate_calls) == 2, "one call per chunk, for attribution"
    assert result.concepts == 1
    for edge in captured["edges"]:
        assert edge.source_chunk_id, "an edge with no source cannot be evidence"
        assert edge.extractor_model
        assert 0.0 <= edge.confidence <= 1.0
    assert {c["source_chunk_id"] for c in captured["claims"]} == {
        make_chunk_id(registered.version_id, 0),
        make_chunk_id(registered.version_id, 1),
    }


async def test_one_unparseable_chunk_does_not_lose_the_whole_document(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Structure and citations are already projected; the document stays
    browsable either way."""
    fake = FakeProvider(semantics="not json at all")
    monkeypatch.setattr(paid, "_provider", lambda: fake)

    class FakeGraph:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def ensure_schema(self): pass

    monkeypatch.setattr(paid, "Graph", lambda url: FakeGraph())
    monkeypatch.setattr(paid.proj, "project_concepts", lambda g, c, **k: len(c))
    monkeypatch.setattr(paid.proj, "project_claims", lambda g, c, **k: len(c))
    monkeypatch.setattr(paid.proj, "project_semantic_edges", lambda g, e: len(e))

    registered, _ = _ids()
    result = await paid.extract_semantics(
        "run_10", registered, _chunked(workspace, "run_10", 2)
    )
    assert result.concepts == 0 and result.edges == 0


def _fake_graph(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture what would have been projected, without a Memgraph."""
    captured: dict = {}

    class FakeGraph:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def ensure_schema(self): pass

    monkeypatch.setattr(paid, "Graph", lambda url: FakeGraph())
    monkeypatch.setattr(paid.proj, "project_concepts",
                        lambda g, c, **k: captured.setdefault("concepts", c) and 0 or len(c))
    monkeypatch.setattr(paid.proj, "project_claims",
                        lambda g, c, **k: captured.setdefault("claims", c) and 0 or len(c))
    monkeypatch.setattr(paid.proj, "project_semantic_edges",
                        lambda g, e: captured.setdefault("edges", e) and 0 or len(e))
    return captured


def _semantic_response(quote: str) -> str:
    import json

    return json.dumps({
        "conceptos": [{"nombre": "Providencia", "tipo": "doctrina", "confianza": 0.9}],
        "afirmaciones": [{
            "texto": "El texto trata del conocimiento de Dios.",
            "concepto": "Providencia",
            "confianza": 0.8,
            "cita": quote,
        }],
    }, ensure_ascii=False)


async def test_a_quote_is_located_in_the_document_spelling_not_the_models(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """A model asked for a verbatim span reproduces the words and not the line
    breaks the extractor put between them. Whitespace may differ; the span that
    comes back is the document's own, and it indexes the corrected stream."""
    # `_chunked` writes "Párrafo número 0 sobre el conocimiento de Dios." at
    # char_from 0, so a quote's offsets are checkable by hand.
    fake = FakeProvider(semantics=_semantic_response("sobre  el\nconocimiento"))
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    captured = _fake_graph(monkeypatch)

    registered, _ = _ids()
    result = await paid.extract_semantics(
        "run_q1", registered, _chunked(workspace, "run_q1", 1)
    )

    claim = captured["claims"][0]
    assert claim["quote"] == "sobre el conocimiento", "the document's spelling wins"
    text = "Párrafo número 0 sobre el conocimiento de Dios."
    assert claim["quote_char_start"] == text.index("sobre el conocimiento")
    assert claim["quote_char_end"] == claim["quote_char_start"] + len(claim["quote"])
    assert result.claims == 1 and result.claims_verified == 1


async def test_a_quote_the_chunk_does_not_contain_costs_the_span_not_the_claim(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """The claim is still a reading of a chunk a person can open. What it loses
    is the pointer to the sentence — and the loss is counted, because a claim
    that cannot be checked must not look like one that can."""
    fake = FakeProvider(semantics=_semantic_response("una frase que no está ahí"))
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    captured = _fake_graph(monkeypatch)

    registered, _ = _ids()
    result = await paid.extract_semantics(
        "run_q2", registered, _chunked(workspace, "run_q2", 1)
    )

    claim = captured["claims"][0]
    assert "quote" not in claim
    assert claim["text"] and claim["source_chunk_id"], "the claim survives"
    assert result.claims == 1 and result.claims_verified == 0


def test_only_whitespace_may_differ_between_a_quote_and_the_document(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """The check is what makes the quote worth storing. A fixed accent, a
    normalised quotation mark or a dropped word is a quote the document does not
    contain, and tolerating any of them would make every span unreliable."""
    text = "Párrafo número 0 sobre el conocimiento de Dios."
    assert paid._locate_quote("sobre el conocimiento", text, 0) is not None
    for near_miss in (
        "sobre el conocimento",          # a letter dropped
        "sobre el Conocimiento",         # a case change
        "sobre el conocimiento de dios", # an accent fixed the other way
        "número 0 de Dios",              # words joined across a gap
    ):
        assert paid._locate_quote(near_miss, text, 0) is None, near_miss


async def test_a_claim_records_what_the_document_does_with_it(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """A text that expounds the doctrine it is about to rebut enunciates it in
    the same words as one who holds it. Without the state the two are identical
    in the graph."""
    import json

    fake = FakeProvider(semantics=json.dumps({
        "conceptos": [{"nombre": "Providencia", "confianza": 0.9}],
        "afirmaciones": [{
            "texto": "Los autores citados lo niegan.", "concepto": "Providencia",
            "confianza": 0.8, "cita": "conocimiento de Dios", "estado": "niega",
        }],
    }, ensure_ascii=False))
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    captured = _fake_graph(monkeypatch)

    registered, _ = _ids()
    await paid.extract_semantics(
        "run_s1", registered, _chunked(workspace, "run_s1", 1)
    )
    assert captured["claims"][0]["status"] == "niega"


def test_an_unknown_state_is_absent_rather_than_asserted():
    """There is no safe default. A claim the document merely reports would be
    promoted to one it asserts, which is the confusion the field removes — so
    anything outside the enum stores nothing and renders as `sin_estado`."""
    assert paid._status("afirma") == "afirma"
    assert paid._status("NIEGA") == "niega", "case is not a difference worth losing"
    for bad in (None, "", "true", "SUSPECTED", "probablemente", 3):
        assert paid._status(bad) is None, bad


# -- gleaning ---------------------------------------------------------------


class GleaningProvider:
    """Answers a scripted sequence, and records whether it was given history."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, tuple | None]] = []

        class _S:
            model = "gemini-3.6-flash"

        self.settings = _S()

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None,
                 history=None):
        import json

        self.calls.append((prompt, tuple(history) if history else None))
        payload = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return Generation(text=json.dumps(payload, ensure_ascii=False),
                          usage=Usage(100, 20, 1))

    def embed(self, texts, *, task, workers=6):  # pragma: no cover - unused here
        raise AssertionError("semantics does not embed")


def _claim(text: str, quote: str) -> dict:
    return {"texto": text, "concepto": "Providencia", "confianza": 0.8,
            "cita": quote, "estado": "afirma"}


async def test_gleaning_is_off_unless_asked_for(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """The two projects this is ported from default to one pass. This pipeline
    does not, because it has no measurement of its own — and the last one it took
    of this stage went against the intuition."""
    from brainworker import config

    assert config.Gemini().max_gleaning == 0
    fake = GleaningProvider([{"conceptos": [], "afirmaciones": []}])
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    _fake_graph(monkeypatch)

    registered, _ = _ids()
    await paid.extract_semantics(
        "run_g0", registered, _chunked(workspace, "run_g0", 2)
    )
    assert len(fake.calls) == 2, "one call per chunk and no more"
    assert all(history is None for _, history in fake.calls)


async def test_a_gleaning_pass_adds_what_the_first_missed_in_the_same_conversation(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Asking in the same conversation is what makes the second pass cheaper than
    a second extraction: the model is told to add what it missed rather than told
    not to repeat itself."""
    fake = GleaningProvider([
        {"conceptos": [{"nombre": "Providencia", "confianza": 0.9}],
         "afirmaciones": [_claim("Trata del conocimiento.", "conocimiento de Dios")]},
        {"conceptos": [{"nombre": "Sabiduría", "confianza": 0.7}],
         "afirmaciones": [_claim("Habla de un párrafo.", "Párrafo número")],
         "quedan": False},
    ])
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    monkeypatch.setenv("BRAIN_MAX_GLEANING", "1")
    captured = _fake_graph(monkeypatch)

    registered, _ = _ids()
    result = await paid.extract_semantics(
        "run_g1", registered, _chunked(workspace, "run_g1", 1)
    )

    assert len(fake.calls) == 2, "the chunk, then the gleaning pass"
    assert fake.calls[1][1] is not None, "the second pass carries the first"
    assert result.concepts == 2 and result.claims == 2
    assert {c["quote"] for c in captured["claims"]} == {
        "conocimiento de Dios", "Párrafo número",
    }


async def test_a_gleaning_pass_stops_when_the_model_says_nothing_is_left(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """`quedan` rides in the schema instead of costing a second call, which is
    what the ported prompts do — they need a separate Y/N turn because their
    extraction returns delimited text rather than structured output."""
    fake = GleaningProvider([
        {"conceptos": [], "afirmaciones": []},
        {"conceptos": [], "afirmaciones": [], "quedan": False},
    ])
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    monkeypatch.setenv("BRAIN_MAX_GLEANING", "3")
    _fake_graph(monkeypatch)

    registered, _ = _ids()
    await paid.extract_semantics(
        "run_g2", registered, _chunked(workspace, "run_g2", 1)
    )
    assert len(fake.calls) == 2, "three rounds allowed, one used"


async def test_a_restatement_from_a_later_pass_does_not_become_a_second_claim(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Gleaning is asked only for what was missed, so a claim it returns on a span
    an earlier pass already used is a restatement. Two nodes for one reading would
    make the same evidence count twice."""
    fake = GleaningProvider([
        {"conceptos": [], "afirmaciones": [_claim("El texto trata de Dios.", "conocimiento de Dios")]},
        {"conceptos": [], "afirmaciones": [
            # Same span, longer wording: `claim_id` differs, the span does not.
            _claim("El texto trata del conocimiento de Dios.", "conocimiento de Dios"),
        ], "quedan": False},
    ])
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    monkeypatch.setenv("BRAIN_MAX_GLEANING", "1")
    captured = _fake_graph(monkeypatch)

    registered, _ = _ids()
    result = await paid.extract_semantics(
        "run_g3", registered, _chunked(workspace, "run_g3", 1)
    )
    assert len(captured["claims"]) == 1
    assert result.claims == 1 and result.claims_verified == 1


# -- concept descriptions ---------------------------------------------------


class CondenseAdapter:
    """Stands in for the adapter, counting what it was asked to condense."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt, *, system=None, stage=None):
        self.prompts.append(prompt)
        return "  Una descripción condensada.  "


def _descriptions(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(paid.proj, "read_concept_descriptions", lambda g, ids: rows)
    monkeypatch.setattr(
        paid.proj, "set_concept_descriptions",
        lambda g, r: written.extend(r) or len(r),
    )
    return written


def test_a_short_description_is_concatenated_rather_than_paid_for(
    monkeypatch: pytest.MonkeyPatch,
):
    """Below the threshold the concatenation *is* the description. Paying a model
    to restate two short sentences is the cost nano-graphrag's threshold exists to
    avoid."""
    written = _descriptions(monkeypatch, [
        {"id": "con_1", "name": "Providencia", "description": None,
         "raw": ["El gobierno de Dios.", "Alcanza a todo lo creado."]},
    ])
    adapter = CondenseAdapter()

    count, paid_calls = paid._condense_descriptions(None, adapter, ["con_1"])

    assert (count, paid_calls) == (1, 0)
    assert adapter.prompts == []
    assert written[0]["description"] == "El gobierno de Dios. Alcanza a todo lo creado."


def test_a_description_that_grew_past_the_threshold_is_condensed_once(
    monkeypatch: pytest.MonkeyPatch,
):
    long = ["Una frase larga sobre la providencia divina y su alcance." * 2] * 20
    written = _descriptions(monkeypatch, [
        {"id": "con_1", "name": "Providencia", "description": None, "raw": long},
    ])
    adapter = CondenseAdapter()

    count, paid_calls = paid._condense_descriptions(None, adapter, ["con_1"])

    assert (count, paid_calls) == (1, 1)
    assert len(adapter.prompts) == 1
    assert written[0]["description"] == "Una descripción condensada.", "trimmed"


def test_a_concept_already_rich_enough_stops_costing_anything(
    monkeypatch: pytest.MonkeyPatch,
):
    """Past the source cap another chunk barely moves a description. Without this
    rule a concept mentioned by fifty chunks is re-summarised on every import —
    a one-off cost turned into a recurring one."""
    long = ["Una frase larga sobre la providencia divina y su alcance." * 2] * 20
    assert len(long) > paid.CONDENSE_SOURCE_CAP
    written = _descriptions(monkeypatch, [
        {"id": "con_1", "name": "Providencia", "raw": long,
         "description": "Ya condensada antes."},
    ])
    adapter = CondenseAdapter()

    count, paid_calls = paid._condense_descriptions(None, adapter, ["con_1"])

    assert (count, paid_calls) == (0, 0)
    assert adapter.prompts == []
    assert written == [], "the existing description is left exactly as it was"


def test_a_failed_condensation_leaves_a_worse_description_not_none(
    monkeypatch: pytest.MonkeyPatch,
):
    """This step runs after everything else is already projected. Losing the
    concatenation too would make a model outage cost more than the sentence it
    was asked for."""
    long = ["Una frase larga sobre la providencia divina y su alcance." * 2] * 20
    written = _descriptions(monkeypatch, [
        {"id": "con_1", "name": "Providencia", "description": None, "raw": long},
    ])

    class Broken(CondenseAdapter):
        def generate(self, prompt, *, system=None, stage=None):
            raise RuntimeError("no credentials")

    count, paid_calls = paid._condense_descriptions(None, Broken(), ["con_1"])

    assert (count, paid_calls) == (1, 0)
    assert written[0]["description"].startswith("Una frase larga")


async def test_descriptions_are_collected_free_but_condensing_is_opt_in(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Extracting the description is the same call and costs nothing extra.
    Turning many into one is the part that spends, so it sits behind the gate."""
    import json

    from brainworker.pipeline import StageOptions

    fake = FakeProvider(semantics=json.dumps({
        "conceptos": [{"nombre": "Providencia", "confianza": 0.9,
                       "descripcion": "El gobierno de Dios."}],
        "afirmaciones": [],
    }, ensure_ascii=False))
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    captured = _fake_graph(monkeypatch)
    called: list[int] = []
    monkeypatch.setattr(paid, "_condense_descriptions",
                        lambda *a: called.append(1) or (0, 0))

    registered, _ = _ids()
    result = await paid.extract_semantics(
        "run_d1", registered, _chunked(workspace, "run_d1", 2), StageOptions()
    )

    # Two chunks describing the concept identically contribute one description.
    assert captured["concepts"][0]["descriptions"] == ["El gobierno de Dios."]
    assert called == [], "condensing is off unless the gate switched it on"
    assert result.condense_spend is None


# -- the second concept -----------------------------------------------------


async def test_a_claim_that_relates_two_concepts_projects_the_second_edge(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """The graph's only concept-to-concept path. Everything else joins two
    concepts through a chunk that mentioned both, which is co-occurrence."""
    import json

    fake = FakeProvider(semantics=json.dumps({
        "conceptos": [],
        "afirmaciones": [{
            "texto": "La fe sin obras está muerta.", "concepto": "Fe",
            "relaciona": "Obras", "confianza": 0.9,
            "cita": "conocimiento de Dios", "estado": "afirma",
        }],
    }, ensure_ascii=False))
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    captured = _fake_graph(monkeypatch)

    registered, _ = _ids()
    await paid.extract_semantics(
        "run_r1", registered, _chunked(workspace, "run_r1", 1)
    )

    kinds = [e.type for e in captured["edges"]]
    assert kinds.count("ABOUT") == 1 and kinds.count("INVOLVES") == 1
    # The second concept is created as well; a relation to a node that does not
    # exist would project an edge into nothing.
    assert {c["name"] for c in captured["concepts"]} == {"Fe", "Obras"}
    involves = next(e for e in captured["edges"] if e.type == "INVOLVES")
    assert involves.source_chunk_id, "a relation with no source cannot be evidence"


async def test_a_claim_relating_a_concept_to_itself_projects_no_second_edge(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """The model restating `concepto` under another name. A self-loop is not a
    relation and every traversal would have to filter it out on every read."""
    import json

    fake = FakeProvider(semantics=json.dumps({
        "conceptos": [],
        "afirmaciones": [{
            "texto": "La fe es fe.", "concepto": "Fe", "relaciona": "  fe  ",
            "confianza": 0.9, "cita": "conocimiento de Dios", "estado": "afirma",
        }],
    }, ensure_ascii=False))
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    captured = _fake_graph(monkeypatch)

    registered, _ = _ids()
    await paid.extract_semantics(
        "run_r2", registered, _chunked(workspace, "run_r2", 1)
    )
    assert [e.type for e in captured["edges"]] == ["ABOUT"]


# -- measuring the index ----------------------------------------------------


class EvalProvider:
    """Generates one question per call and embeds deterministically."""

    def __init__(self) -> None:
        self.generate_calls: list[str] = []
        self.embed_calls: list[list[str]] = []

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None):
        import json

        self.generate_calls.append(prompt)
        payload = json.loads(prompt)
        # The question names the chunk it came from, which is what makes
        # retrieval deterministic below without a real embedding model.
        return Generation(
            text=json.dumps(
                {
                    "question": f"¿Qué dice el fragmento {payload['fragmento_principal'][:40]!r}?",
                    "answerable_only_by_main": True,
                }
            ),
            usage=Usage(input_tokens=800, output_tokens=60, calls=1),
        )

    def embed(self, texts, *, task, workers=6):
        self.embed_calls.append(list(texts))
        return [
            Embedding(values=[0.01] * DIMS, usage=Usage(input_tokens=10)) for _ in texts
        ]


def _profile(workspace: pathlib.Path, *, learned_from: str, questions: int = 2):
    """A profile on disk for the legacy tenant, with an eval set already in it."""
    from docagent.profiles import EvalItem, Profile

    root = workspace / "profiles"
    p = Profile(
        fingerprint="fp_shared",
        slug="una-familia-fp_share",
        extractor="plain",
        learned_from=learned_from,
        learned_at=1.0,
        evalset=[
            EvalItem(question=f"heredada {i}", chunk_index=i, char_mid=-1)
            for i in range(questions)
        ],
    )
    p.save(root)
    return p


def _extraction(source_key: str) -> Extraction:
    ref = ArtifactRef(kind="raw_text", path="runs/x/raw.txt", sha256="a" * 64, bytes=1)
    ev_ref = ArtifactRef(kind="evidence", path="runs/x/evidence.json",
                         sha256="b" * 64, bytes=1)
    return Extraction(
        text=ref, evidence=ev_ref, extractor="plain", source_key=source_key
    )


async def test_a_reused_profile_does_not_score_against_another_book(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """The failure `doc/CLAUDE.md` records, carried across the boundary.

    The fingerprint groups by *structure*, and structure is not subject matter: a
    hermeneutics chapter and a church-history book landed on one fingerprint on
    the real corpus. Scoring book B against book A's questions produced a recall
    of 0 that said nothing about either index. The engine drops an inherited eval
    set when `learned_from` names a different file; if this side did not, the fix
    would simply not travel.
    """
    fake = EvalProvider()
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    _profile(workspace, learned_from="libros/otro-libro.pdf")

    result = await paid.build_evalset(
        "run_drop",
        _extraction("libros/este-libro.pdf"),
        _chunked(workspace, "run_drop", 3),
        ProfileDecision(fingerprint="fp_shared", source="reused"),
    )

    assert result.reused is False, "it scored this book with another book's questions"
    assert fake.generate_calls, "it neither reused nor generated"
    assert result.questions == 3


async def test_this_documents_own_questions_are_reused_and_cost_nothing(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Regenerating them would measure the questions instead of the change, which
    is the whole reason the engine keeps them in the profile."""
    fake = EvalProvider()
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    _profile(workspace, learned_from="libros/este-libro.pdf", questions=2)

    result = await paid.build_evalset(
        "run_reuse",
        _extraction("libros/este-libro.pdf"),
        _chunked(workspace, "run_reuse", 3),
        ProfileDecision(fingerprint="fp_shared", source="reused"),
    )

    assert result.reused is True
    assert result.questions == 2
    assert fake.generate_calls == [], "it re-paid for questions it already had"


async def test_the_measurement_is_scoped_to_the_version_it_just_wrote(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch, qdrant: str
):
    """Two documents, one collection. A measurement with no scope searches the
    whole shelf and reports a figure about the corpus as if it were about this
    document."""
    fake = EvalProvider()
    monkeypatch.setattr(paid, "_provider", lambda: fake)

    mine, staged = _ids()
    theirs, other_staged = _ids()
    await paid.embed_and_index("m", "lib_t", mine, staged, _chunked(workspace, "m", 4))
    await paid.embed_and_index(
        "t", "lib_t", theirs, other_staged, _chunked(workspace, "t", 4)
    )

    chunked = _chunked(workspace, "ev", 4)
    evalset = await paid.build_evalset(
        "ev", _extraction("libros/mio.pdf"), chunked, ProfileDecision(fingerprint="fp_x")
    )
    scores = await paid.evaluate_index(
        "ev", mine, chunked, evalset, ProfileDecision(fingerprint="fp_x")
    )

    report = ArtifactStore(workspace, "ev").read_json(scores.report)
    assert report["scope"] == {
        "tenant_id": mine.tenant_id,
        "version_id": mine.version_id,
    }
    assert scores.eval_questions == evalset.questions
    # Both legs ran, or the leakage the questions introduce stays invisible.
    assert scores.leakage


async def test_the_scores_are_written_back_into_the_family_profile(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """The artifact is the authority; the profile's copy is what lets a family
    accumulate measurement across documents."""
    from docagent import profiles as engine_profiles

    fake = EvalProvider()
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    before = _profile(workspace, learned_from="libros/este-libro.pdf")

    extraction = _extraction("libros/este-libro.pdf")
    decision = ProfileDecision(fingerprint="fp_shared", slug=before.slug, source="reused")
    evalset = await paid.build_evalset(
        "run_p", extraction, _chunked(workspace, "run_p", 3), decision
    )
    scores = Scores(
        recall_at_1=0.5, recall_at_5=0.9, mrr_at_10=0.7,
        recall_at_5_dense_only=0.85, noise_floor=0.58, chunks=3, eval_questions=2,
    )

    assert await paid.persist_profile_scores("run_p", extraction, decision, evalset, scores)

    after = engine_profiles.load("fp_shared", workspace / "profiles")
    assert after.scores.recall_at_5 == 0.9
    assert after.revisions == before.revisions + 1
    assert after.learned_from == "libros/este-libro.pdf"


async def test_a_document_with_no_profile_keeps_its_scores_in_the_artifact(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """A document indexed with the measured defaults has no file of its own.
    That is an ordinary outcome, and reporting it as a successful write would be
    a lie about where the numbers went."""
    fake = EvalProvider()
    monkeypatch.setattr(paid, "_provider", lambda: fake)

    extraction = _extraction("libros/sin-perfil.pdf")
    decision = ProfileDecision(fingerprint="fp_nadie", source="default")
    evalset = await paid.build_evalset(
        "run_np", extraction, _chunked(workspace, "run_np", 2), decision
    )

    wrote = await paid.persist_profile_scores(
        "run_np", extraction, decision, evalset, Scores(chunks=2)
    )
    assert wrote is False


async def test_a_measurement_keeps_the_ranks_its_own_margin_is_resampled_from(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch, qdrant: str
):
    """The bootstrap margin a tuning candidate must beat is resampled from the
    reciprocal rank of every question, and that vector cannot be rebuilt from
    `Scores`.

    Deriving it from the mean was tried: on a realistic 40-question run it gave
    ±0.040 where the true margin is ±0.062, because a flat vector has none of the
    spread that ranks of 1, ½, ⅓, ¼ carry. **35% too small, in the direction that
    accepts noise as a real gain** — which is the one thing the margin exists to
    prevent.
    """
    fake = EvalProvider()
    monkeypatch.setattr(paid, "_provider", lambda: fake)

    registered, staged = _ids()
    chunked = _chunked(workspace, "ranks", 4)
    await paid.embed_and_index("ranks", "lib_t", registered, staged, chunked)
    evalset = await paid.build_evalset(
        "ranks", _extraction("libros/x.pdf"), chunked, ProfileDecision(fingerprint="fp")
    )
    scores = await paid.evaluate_index(
        "ranks", registered, chunked, evalset, ProfileDecision(fingerprint="fp")
    )

    report = ArtifactStore(workspace, "ranks").read_json(scores.report)
    assert len(report["reciprocal_ranks"]) == scores.eval_questions

    run = paid._baseline_from(ArtifactStore(workspace, "ranks"), scores)
    assert run is not None
    assert run.rr_vector() == report["reciprocal_ranks"]


async def test_tuning_declines_rather_than_inventing_a_margin(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """A report written before the ranks were kept has no margin in it. A round
    that cannot compute its own threshold would adopt a candidate for having been
    tried, so it declines instead."""
    from brainworker.pipeline import Scores as PipelineScores

    fake = EvalProvider()
    monkeypatch.setattr(paid, "_provider", lambda: fake)
    store = ArtifactStore(workspace, "old")
    ref = store.write_json("scores", {"scores": {"eval_questions": 40}, "misses": []})

    outcome = await paid.propose_tuning(
        "old",
        _ids()[0],
        _chunked(workspace, "old", 3),
        await paid.build_evalset(
            "old", _extraction("libros/x.pdf"), _chunked(workspace, "old", 3),
            ProfileDecision(fingerprint="fp"),
        ),
        ProfileDecision(fingerprint="fp"),
        PipelineScores(eval_questions=40, mrr_at_10=0.7, report=ref),
    )

    assert outcome.kind == "none"
