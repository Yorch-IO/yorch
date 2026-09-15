"""The seam the Temporal worker drives the engine through.

Everything here is about a property the host cannot check for itself, because
each failure it guards against is silent: a vector space nobody notices changed,
a stale tail nothing deletes, a measurement with no scope.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from docagent import embedcache, runner
from docagent.bm25 import SparseVector
from docagent.chunk import Chunk
from docagent.profiles import EvalItem, RetrievalParams
from docagent.qdrant import Hit, SearchOpts

DIMS = 8


class FakeVector:
    def __init__(self, values, tokens=3):
        self.values = values
        self.tokens = tokens


class FakeEmbedder:
    def __init__(self, model="model-a", dimensions=DIMS):
        self.model = model
        self.dimensions = dimensions
        self.seen: list[list[str]] = []

    def embed_many(self, texts, *, task_type, on_done=None):
        self.seen.append(list(texts))
        return [FakeVector([float(len(t) % 7)] * self.dimensions) for t in texts]


class FakeWriter:
    def __init__(self, model="model-a", dimensions=DIMS):
        self.model = model
        self.dimensions = dimensions
        self.rows: list[runner.IndexRow] = []
        self.pruned_to: int | None = None

    def upsert(self, rows):
        self.rows = list(rows)
        return len(self.rows)

    def prune_tail(self, keep):
        self.pruned_to = keep
        return 0


def chunk(index: int, text: str = "un párrafo cualquiera del libro") -> Chunk:
    return Chunk(
        index=index,
        kind="cuerpo",
        chapter="1. Capítulo",
        section="",
        text=text,
        char_from=index * 100,
        char_to=index * 100 + len(text.encode("utf-8")),
        para_from=index,
        para_to=index,
    )


# --- the vector space --------------------------------------------------------


def test_the_engine_never_chooses_the_embedding_model():
    """The model belongs to the collection, not to this package.

    `brain` holds 5,335 points embedded with `gemini-embedding-2`; `docagent_v2`
    holds 4,064 embedded with `gemini-embedding-001`. Both are 3,072 wide, so
    Qdrant stores either without complaint and the cosine between them means
    nothing — a healthy log over a corrupt ranking. A run died on that once
    already, after paying $1.27 for correction, when `vertex.EMBED_MODEL` named
    a model the project does not serve.

    So `runner` must not be a third place a model id can come from.
    """
    tree = ast.parse(pathlib.Path(runner.__file__).read_text(encoding="utf-8"))
    named: list[str] = []
    for node in ast.walk(tree):
        # A model id can only reach a caller as a value: a constant, a default,
        # or a keyword. Prose about one is what the module is *for*.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "gemini" in node.value and not _is_docstring(tree, node):
                named.append(node.value)
    assert named == [], f"runner names a model: {named}"


def _is_docstring(tree, node) -> bool:
    for parent in ast.walk(tree):
        if isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(parent, "body", [])
            if body and isinstance(body[0], ast.Expr) and body[0].value is node:
                return True
    return False


def test_a_mismatched_vector_space_is_refused_before_anything_is_paid_for():
    embedder = FakeEmbedder(model="gemini-embedding-001")
    writer = FakeWriter(model="gemini-embedding-2")

    with pytest.raises(runner.VectorSpaceMismatch):
        runner.index_chunks([chunk(0)], embedder=embedder, writer=writer)

    assert embedder.seen == [], "it embedded before checking"


def test_a_mismatched_width_is_refused_too():
    with pytest.raises(runner.VectorSpaceMismatch):
        runner.index_chunks(
            [chunk(0)],
            embedder=FakeEmbedder(dimensions=768),
            writer=FakeWriter(dimensions=3072),
        )


# --- the write ---------------------------------------------------------------


def test_the_dense_vector_is_the_enriched_text_and_the_sparse_one_is_not():
    """Retrieval matches on breadcrumb + context + overlap + text; a reader is
    shown the chunk. Collapsing the two would change what ranks."""
    c = chunk(0)
    embedder, writer = FakeEmbedder(), FakeWriter()

    runner.index_chunks([c], embedder=embedder, writer=writer)

    assert embedder.seen[0] == [c.embed_text()]
    assert c.embed_text() != c.text, "the fixture no longer exercises the asymmetry"
    assert writer.rows[0].chunk.text == c.text


def test_indexing_always_prunes_the_tail_not_only_after_a_tuning_revert():
    """Any re-index whose chunk count shrank leaves the previous chunking's tail
    alive, carrying spans into a byte stream nothing holds. Nothing else in
    either host deletes it."""
    writer = FakeWriter()
    runner.index_chunks(
        [chunk(i) for i in range(6)], embedder=FakeEmbedder(), writer=writer
    )
    assert writer.pruned_to == 6


def test_an_empty_document_writes_nothing_and_does_not_embed():
    embedder, writer = FakeEmbedder(), FakeWriter()
    out = runner.index_chunks([], embedder=embedder, writer=writer)
    assert out.points == 0 and embedder.seen == []


def test_the_ledger_records_one_call_per_chunk():
    from docagent.ledger import Ledger

    ledger = Ledger()
    runner.index_chunks(
        [chunk(i) for i in range(4)],
        embedder=FakeEmbedder(),
        writer=FakeWriter(),
        ledger=ledger,
    )
    assert ledger.entries["embed"].calls == 4


# --- the measurement ---------------------------------------------------------


class FakeSearcher:
    """Returns the chunk whose index the question names, plus a decoy."""

    def __init__(self, chunks):
        self.by_index = {c.index: c for c in chunks}
        self.queries: list[SearchOpts] = []

    def topicality_gate(self, vector, opts):
        return True

    def search(self, vector, opts):
        self.queries.append(opts)
        wanted = int(round(vector[0]))
        out = []
        for idx in (wanted, (wanted + 1) % len(self.by_index)):
            c = self.by_index.get(idx)
            if c is None:
                continue
            out.append(
                Hit(
                    score=0.9 if idx == wanted else 0.4,
                    payload={
                        "chunk_index": c.index,
                        "char_span": [c.char_from, c.char_to],
                        "breadcrumb": c.chapter,
                        "cell_ref": "",
                    },
                )
            )
        return out


class IndexEmbedder(FakeEmbedder):
    """Embeds a question to the index it names, so retrieval is deterministic."""

    def embed_many(self, texts, *, task_type, on_done=None):
        self.seen.append(list(texts))
        out = []
        for t in texts:
            digits = "".join(ch for ch in t if ch.isdigit())
            out.append(FakeVector([float(digits or -1)] * self.dimensions))
        return out


def test_a_measurement_without_a_scope_is_refused():
    """An empty filter searches the whole collection, so the figure it returns is
    a fact about the corpus rather than about this document."""
    with pytest.raises(runner.UnscopedEvaluation):
        runner.evaluate(
            IndexEmbedder(),
            FakeSearcher([chunk(0)]),
            [EvalItem(question="pregunta 0", chunk_index=0)],
            scope={},
        )


def test_the_scope_reaches_every_search_including_the_noise_floor():
    chunks = [chunk(i) for i in range(3)]
    searcher = FakeSearcher(chunks)
    scope = {"tenant_id": "tnt_0", "version_id": "ver_1"}

    runner.evaluate(
        IndexEmbedder(),
        searcher,
        [EvalItem(question=f"pregunta {i}", chunk_index=i, char_mid=-1) for i in range(3)],
        scope=scope,
        chunks=len(chunks),
    )

    assert searcher.queries, "nothing was searched"
    assert all(o.filters == scope for o in searcher.queries)


def test_both_legs_are_measured_so_the_lexical_leakage_stays_visible():
    """The questions are written from the chunks they must find, so the hybrid
    figure alone overstates. The gap between the two modes is the measurement."""
    chunks = [chunk(i) for i in range(3)]
    searcher = FakeSearcher(chunks)

    out = runner.evaluate(
        IndexEmbedder(),
        searcher,
        [EvalItem(question=f"pregunta {i}", chunk_index=i, char_mid=-1) for i in range(3)],
        scope={"version_id": "ver_1"},
        params=RetrievalParams(),
        chunks=len(chunks),
    )

    assert {o.dense_only for o in searcher.queries} == {True, False}
    assert out.scores.eval_questions == 3
    assert out.leakage


def test_each_question_is_embedded_once_and_shared_across_both_legs():
    """Two passes would pay twice and muddy the comparison with embedding
    nondeterminism."""
    embedder = IndexEmbedder()
    items = [EvalItem(question=f"pregunta {i}", chunk_index=i, char_mid=-1) for i in range(3)]

    runner.evaluate(
        embedder, FakeSearcher([chunk(i) for i in range(3)]), items,
        scope={"version_id": "ver_1"},
    )

    batched = [b for b in embedder.seen if len(b) == 3]
    assert len(batched) == 1, "the questions were embedded more than once"


# --- the shared embedding cache ---------------------------------------------


def test_a_retry_does_not_re_embed_what_it_already_paid_for(tmp_path):
    """The gap that made the quota wall unrecoverable, now shared with the worker.

    `online_prediction_requests_per_base_model` is metered `1/min/{project}/{base_model}`
    at ~6 embeddings a minute, so re-spending 586 units to arrive at the same wall
    is how a run never converges.
    """
    root = tmp_path / "embed"
    assert embedcache.read(root, "m", DIMS, "RETRIEVAL_DOCUMENT", "hola") is None

    embedcache.write(root, "m", DIMS, "RETRIEVAL_DOCUMENT", "hola", [0.25] * DIMS, 7)

    assert embedcache.read(root, "m", DIMS, "RETRIEVAL_DOCUMENT", "hola") == (
        [0.25] * DIMS,
        7,
    )


def test_the_cache_key_separates_two_models_of_the_same_width():
    """Two 3,072-wide models are interchangeable to Qdrant and not to the cosine,
    so an entry must never survive a model change."""
    a = embedcache.key("gemini-embedding-001", 3072, "RETRIEVAL_DOCUMENT", "x")
    b = embedcache.key("gemini-embedding-2", 3072, "RETRIEVAL_DOCUMENT", "x")
    assert a != b


def test_a_query_vector_is_not_served_to_a_document_lookup():
    assert embedcache.key("m", DIMS, "RETRIEVAL_QUERY", "x") != embedcache.key(
        "m", DIMS, "RETRIEVAL_DOCUMENT", "x"
    )


def test_a_truncated_file_reads_as_a_miss_rather_than_a_short_vector(tmp_path):
    """A short vector would reach Qdrant and be accepted."""
    root = tmp_path / "embed"
    root.mkdir()
    key = embedcache.key("m", DIMS, "RETRIEVAL_DOCUMENT", "hola")
    (root / f"{key}.f32").write_bytes(b"\x00\x00\x00\x00" + b"\x00" * 4)

    assert embedcache.read(root, "m", DIMS, "RETRIEVAL_DOCUMENT", "hola") is None


# --- tuning ------------------------------------------------------------------


def _profile_with(target=1200, overlap=150):
    from docagent.chunk import ChunkRules
    from docagent.profiles import Profile

    return Profile(
        fingerprint="fp", slug="s", extractor="plain",
        chunk_rules=ChunkRules(target_chars=target, overlap_chars=overlap),
    )


def _run(rrs):
    from docagent.evaluate import EvalRun

    return EvalRun(
        hits_at_1=sum(1 for r in rrs if r == 1.0),
        hits_at_5=sum(1 for r in rrs if r >= 0.2),
        reciprocal_ranks=list(rrs),
        questions=len(rrs),
    )


def test_a_candidate_is_never_adopted_merely_for_having_been_tried():
    """The whole point of the margin. With σ ≈ 0.358 it takes about 80 questions
    to resolve a +0.040 MRR effect; below that the honest answer is no."""
    chunks = [chunk(i) for i in range(3)]
    items = [EvalItem(question=f"pregunta {i}", chunk_index=i, char_mid=-1) for i in range(3)]
    baseline = _run([1.0, 0.5, 0.5])

    out = runner.tune_once(
        IndexEmbedder(), FakeSearcher(chunks), items,
        scope={"version_id": "ver_1"}, profile=_profile_with(), baseline=baseline,
    )

    # Every retrieval knob was tried — they cost nothing, the index is unchanged.
    assert len(out.history) >= len(__import__("docagent.evaluate", fromlist=["x"]).RETRIEVAL_CANDIDATES)
    assert out.margin > 0


def test_a_chunking_candidate_is_proposed_not_adopted():
    """It costs a full re-embed — ~100 minutes for a 600-chunk book against the
    measured quota — so the host decides whether to spend it."""
    chunks = [chunk(i) for i in range(3)]
    items = [EvalItem(question=f"pregunta {i}", chunk_index=i, char_mid=-1) for i in range(3)]

    out = runner.tune_once(
        IndexEmbedder(), FakeSearcher(chunks), items,
        scope={"version_id": "ver_1"}, profile=_profile_with(),
        baseline=_run([1.0, 1.0, 1.0]),
    )

    assert out.kind == "chunking"
    assert out.chunk_rules is not None
    assert "re-embeds" in " ".join(out.notes)


def test_a_candidate_that_matches_the_current_rules_is_not_worth_a_reindex():
    """It would spend a full pass to measure something already known."""
    chunks = [chunk(i) for i in range(3)]
    items = [EvalItem(question=f"pregunta {i}", chunk_index=i, char_mid=-1) for i in range(3)]

    out = runner.tune_once(
        IndexEmbedder(), FakeSearcher(chunks), items,
        scope={"version_id": "ver_1"},
        # Every candidate in the grid already matches, so none is worth trying.
        profile=_profile_with(target=900, overlap=300),
        baseline=_run([1.0, 1.0, 1.0]),
        history=[{"candidate": "target=1600"}, {"candidate": "overlap=0"}],
    )

    assert out.kind == "none"


def test_a_candidate_is_judged_against_the_margin_it_was_set_against():
    """A margin derived from the candidate's own run would move with it, and the
    comparison would be between two things that both changed."""
    decision = runner.TuneDecision(
        kind="chunking", label="overlap=300", baseline_objective=0.700, margin=0.077
    )

    # MRR 0.875 against a 0.700 baseline: +0.175, comfortably past ±0.077.
    keep, why = runner.judge_candidate(decision, _run([1.0, 1.0, 1.0, 0.5]))
    assert keep and "keep" in why

    # MRR 0.750: +0.050, which is *inside* the margin. Refusing it is the
    # design working, not a missed win.
    inside, why = runner.judge_candidate(decision, _run([1.0, 1.0, 0.5, 0.5]))
    assert not inside and "revert" in why

    reject, why = runner.judge_candidate(decision, _run([0.5, 0.5, 0.5, 1.0]))
    assert not reject and "revert" in why


def test_tuning_refuses_to_run_without_a_scope():
    with pytest.raises(runner.UnscopedEvaluation):
        runner.tune_once(
            IndexEmbedder(), FakeSearcher([chunk(0)]), [], scope={},
            profile=_profile_with(), baseline=_run([1.0]),
        )
