"""The typed boundary between this engine and a host that is not the CLI.

`brainworker` already imports `extract`, `chunk`, `rules`, `profiles`, `correct`,
`bm25` and `qdrant` directly, and reimplements the engine's node order as
Temporal activities. What it never had is the engine's tail — `build_evalset`,
`evaluate`, the tune loop, `persist` — which is why the product can index 74
documents and say nothing about how well any of them can be found.

This module is that tail, plus the indexing step, behind protocols the host
implements. It is deliberately **not** `graph.build_graph`:

- `IndexState` holds live dataclasses and the document's bytes, so it is not a
  Temporal payload;
- its checkpointer is one shared SQLite file that stores that state at every
  superstep — 565 MB on this machine;
- the tune loop re-embeds a whole book inside one invocation, which is hours
  against a per-minute embedding quota;
- every node is synchronous with no cancellation hook, and reports progress by
  `print()`.

Temporal already provides per-stage retries, a durable history and an approval
gate. What it needed from here was the stages, not the orchestration.

## Two things the host owns, and why

**Identity.** `PointWriter` receives chunks and vectors and derives the point id
and payload itself. This engine has no concept of tenancy — `doc_id_for()`
hashes a filename — and it must not acquire one: a point written without a
`tenant_id` into the product's collection is unreachable by either plane, and
the money for its embedding is spent with no error anywhere.

**The embedding model.** `Embedder` carries it, and this module names no model
constant of its own. The model is a property of the *collection*, not of the
engine: `brain` holds 5,335 points embedded with `gemini-embedding-2` and
`docagent_v2` holds 4,064 embedded with `gemini-embedding-001`. Both are 3,072
wide, so Qdrant accepts either without an error and the cosine between them means
nothing — a healthy log, a collection that accepts, and a corrupt ranking.
`index_chunks` refuses the mismatch rather than trusting a caller to notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from . import evaluate as ev
from .bm25 import SparseVector, avg_doc_len, doc_sparse_vector, tokenize
from .ledger import Ledger
from .profiles import EvalItem, RetrievalParams, Scores

TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"


class VectorSpaceMismatch(RuntimeError):
    """The embedder and the collection do not agree on the vector space.

    Raised before a single request is paid for, because the failure it prevents
    is silent: Qdrant stores any vector of the right width, and the ranking it
    then produces is meaningless rather than wrong-looking.
    """


class UnscopedEvaluation(RuntimeError):
    """A measurement was asked for without saying what to measure.

    An empty scope searches the whole collection, so a document is scored against
    every other document's chunks and reports a recall that is a fact about the
    corpus rather than about this document. The CLI scopes by ``doc_id``; the
    product scopes by ``tenant_id`` and ``version_id``.
    """


# --- what the host supplies --------------------------------------------------


class Chunkish(Protocol):
    """What `index_chunks` itself reads off a chunk — nothing more.

    `docagent.chunk.Chunk` satisfies it. So does a host's own view over a chunk
    it persisted: the worker writes chunks to JSONL because they cannot cross a
    Temporal payload, and reads them back with `embed_text` as a *stored* value
    rather than recomputing it. The stored one is the authority — it is what the
    preview showed and what the estimate was priced from.
    """

    text: str

    def embed_text(self) -> str: ...


@runtime_checkable
class Vector(Protocol):
    """One embedding, as both hosts already return it."""

    values: list[float]
    tokens: int


class Embedder(Protocol):
    """Embeds text in the collection's own vector space.

    ``model`` and ``dimensions`` are read, not decoration: they are what
    `index_chunks` checks against the writer.
    """

    model: str
    dimensions: int

    def embed_many(
        self,
        texts: Sequence[str],
        *,
        task_type: str,
        on_done: Callable[[int, int], None] | None = None,
    ) -> Sequence[Vector]: ...


class PointWriter(Protocol):
    """Turns chunks and vectors into points, and owns their identity."""

    model: str
    dimensions: int

    def upsert(self, rows: Sequence["IndexRow"]) -> int: ...

    def prune_tail(self, keep: int) -> int:
        """Delete points this document left behind above ``keep``.

        Point ids are deterministic in the chunk index on both sides, so a
        re-index that produces *fewer* chunks than the last one leaves the tail
        of the previous chunking alive — carrying that chunking's `char_span`,
        which now indexes a byte stream nothing holds. Observed on
        `07-LlavesDelPoder-INT.pdf` after a rejected 625-chunk tuning candidate,
        and reachable on any re-index whose profile changed.
        """


class Searcher(Protocol):
    """The read side of a collection. `docagent.qdrant.Qdrant` satisfies it.

    `topicality_gate` is part of it because `evaluate.measure` asks before it
    searches: an off-topic query scored 5.58 on BM25 against `Gén. 2:15` at 3.82,
    so no lexical threshold separates them and the gate has to run on the dense
    leg (invariant #9).
    """

    def search(self, vector: list[float], opts: Any) -> list[Any]: ...

    def topicality_gate(self, vector: list[float], opts: Any) -> bool: ...


# --- what crosses the boundary -----------------------------------------------


@dataclass(frozen=True)
class IndexRow:
    """One chunk, ready to be written, minus its identity."""

    chunk: Chunkish
    dense: list[float]
    sparse: SparseVector
    #: What the API billed for this chunk's embedding. The engine's own payload
    #: records it; the product's does not. Carried either way so the writer
    #: decides rather than the engine.
    tokens: int


@dataclass
class IndexOutcome:
    points: int = 0
    pruned: int = 0
    dimensions: int = 0
    input_tokens: int = 0


@dataclass
class EvalOutcome:
    scores: Scores = field(default_factory=Scores)
    #: The hybrid run itself, which is what a tuning decision is judged against.
    #: Not a Temporal payload — the host keeps `scores` and discards this.
    baseline: Any = None
    leakage: str = ""
    #: Bootstrap noise margin on the objective, so a caller can say whether a
    #: difference it is looking at is one it could have detected.
    margin: float = 0.0


# --- the stages --------------------------------------------------------------


def index_chunks(
    chunks: Sequence[Chunkish],
    *,
    embedder: Embedder,
    writer: PointWriter,
    ledger: Ledger | None = None,
    on_done: Callable[[int, int], None] | None = None,
) -> IndexOutcome:
    """Embed every chunk and hand the vectors to the writer.

    One API request per chunk: concurrency, not batching. That is the embedder's
    business, and both hosts already enforce it — `gemini-embedding-2` returns a
    single embedding for a request carrying four, with no error.

    The dense vector comes from `Chunk.embed_text()` (breadcrumb, context,
    overlap and text) while the sparse vector and the stored text come from
    `chunk.text` alone. That asymmetry is deliberate and predates this module:
    retrieval matches on the enriched text, a reader is shown the chunk.
    """
    if embedder.model != writer.model or embedder.dimensions != writer.dimensions:
        raise VectorSpaceMismatch(
            f"embedder writes {embedder.model} at {embedder.dimensions} dims; "
            f"the collection holds {writer.model} at {writer.dimensions}. "
            "Two models of equal width are interchangeable to Qdrant and not to "
            "the cosine, so this would corrupt the ranking without failing."
        )
    if not chunks:
        return IndexOutcome(dimensions=writer.dimensions)

    vectors = embedder.embed_many(
        [c.embed_text() for c in chunks], task_type=TASK_DOCUMENT, on_done=on_done
    )
    if len(vectors) != len(chunks):
        # `gemini-embedding-2` aggregates a batch silently rather than failing,
        # which is how this becomes a count mismatch instead of an error.
        raise VectorSpaceMismatch(
            f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
        )

    docs = [tokenize(c.text) for c in chunks]
    avgdl = avg_doc_len(docs)
    rows = [
        IndexRow(
            chunk=c,
            dense=v.values,
            sparse=doc_sparse_vector(docs[i], avgdl),
            tokens=v.tokens,
        )
        for i, (c, v) in enumerate(zip(chunks, vectors))
    ]

    written = writer.upsert(rows)
    # Always, not only after a tuning revert: any re-index whose chunk count
    # shrank leaves a tail, and nothing else in either host removes it.
    pruned = writer.prune_tail(len(rows))

    tokens = sum(v.tokens for v in vectors)
    if ledger is not None:
        ledger.record("embed", embedder.model, input_tokens=tokens, calls=len(chunks))
    return IndexOutcome(
        points=written or len(rows),
        pruned=pruned,
        dimensions=writer.dimensions,
        input_tokens=tokens,
    )


class _QueryEmbedder:
    """Duck-types `Vertex.embed` for `evaluate.measure` and `noise_floor`.

    Both take a `Vertex` and call one method on it. Rather than widen their
    signatures — they are also the CLI's path — this presents that one method
    over an `Embedder`, so a host that never had a `Vertex` can still measure.
    """

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    def embed(self, text: str, task_type: str = TASK_QUERY, stage: str = "eval_query"):
        return self._embedder.embed_many([text], task_type=task_type)[0]


def evaluate(
    embedder: Embedder,
    searcher: Searcher,
    evalset: Sequence[EvalItem],
    *,
    scope: dict[str, str],
    params: RetrievalParams | None = None,
    chunks: int = 0,
) -> EvalOutcome:
    """Measure the index that was just written, hybrid and dense-only.

    Both legs, always: the synthetic questions are written *from* the chunks they
    must find, so they leak vocabulary to the lexical leg. The gap between the two
    modes is the leakage measurement, and a hybrid figure quoted alone is not
    interpretable.

    ``scope`` is required and must not be empty — see `UnscopedEvaluation`.
    """
    if not scope:
        raise UnscopedEvaluation(
            "evaluate() needs a payload filter naming what to measure; an empty "
            "one scores this document against the whole collection"
        )
    if not evalset:
        return EvalOutcome(scores=Scores(chunks=chunks))

    params = params or RetrievalParams()
    shim = _QueryEmbedder(embedder)

    # Embed each question once and share the vectors across both configurations.
    # Two passes would pay twice and would also muddy the comparison with
    # embedding nondeterminism.
    questions = [item.question for item in evalset]
    vectors = {
        q: v.values
        for q, v in zip(
            questions, embedder.embed_many(questions, task_type=TASK_QUERY)
        )
    }

    common = dict(
        filters=scope, query_vectors=vectors, per_section=params.per_section
    )
    hybrid = ev.measure(
        shim, searcher, list(evalset), min_score=params.min_score,
        dense_only=False, **common,
    )
    dense = ev.measure(
        shim, searcher, list(evalset), min_score=params.min_score,
        dense_only=True, **common,
    )
    floor = ev.noise_floor(shim, searcher, filters=scope)

    return EvalOutcome(
        scores=ev.to_scores(hybrid, dense, floor, chunks),
        baseline=hybrid,
        leakage=ev.leakage_note(hybrid, dense),
        margin=ev.noise_margin(hybrid),
    )


# --- tuning ------------------------------------------------------------------


@dataclass
class TuneDecision:
    """What one tuning round concluded, and what it would cost to act on it.

    Two kinds, and the difference is the whole reason tuning is bounded.
    ``retrieval`` changes nothing in the index — it is a different way of
    querying the same points — so every candidate is tried and the best is
    adopted for free. ``chunking`` re-cuts the document, which means embedding
    every chunk again: at the measured quota that is a second pass of ~100
    minutes for a 600-chunk book, so exactly one candidate is ever proposed and
    the host decides whether to spend it.
    """

    kind: str = "none"
    label: str = ""
    #: Adopted already, for `kind == "retrieval"`. Free.
    retrieval: RetrievalParams | None = None
    #: The rules to try, for `kind == "chunking"`. Not adopted — proposed.
    chunk_rules: Any = None
    #: What a candidate has to beat, and by how much.
    baseline_objective: float = 0.0
    margin: float = 0.0
    history: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def tune_once(
    embedder: Embedder,
    searcher: Searcher,
    evalset: Sequence[EvalItem],
    *,
    scope: dict[str, str],
    profile: Any,
    baseline: Any,
    history: Sequence[dict] = (),
) -> TuneDecision:
    """One round: the free knobs exhaustively, then at most one paid candidate.

    **Nothing is adopted for having been tried.** A candidate must beat a
    bootstrap noise margin computed from the baseline's own per-question
    reciprocal ranks, and the objective is MRR@10 rather than recall@5 — which
    was measured to be blind here, saturated and binary at k=5, so an overlap of
    300 scored identically to an overlap of 0.

    That margin is also why the sample size matters more than it looks: with
    σ ≈ 0.358 it takes roughly 80 questions to resolve a +0.040 MRR effect, so a
    round run on 40 will honestly refuse nearly everything. Refusing an effect
    indistinguishable from noise is the design working; paying for a round that
    *cannot* resolve it is not, and the caller is the one that can tell the user
    which of the two it is buying.
    """
    if not scope:
        raise UnscopedEvaluation("tune_once needs the same scope evaluate() does")

    params = getattr(profile, "retrieval", None) or RetrievalParams()
    margin = ev.noise_margin(baseline)
    rounds = list(history)
    notes = [
        f"baseline MRR@10={ev.objective(baseline):.3f} "
        f"(recall@5={baseline.recall_at_5:.3f}), margin ±{margin:.3f}"
    ]

    shim = _QueryEmbedder(embedder)
    questions = [item.question for item in evalset]
    vectors = {
        q: v.values
        for q, v in zip(questions, embedder.embed_many(questions, task_type=TASK_QUERY))
    }

    best_label, best_run, best_params = None, baseline, params
    for cand in ev.RETRIEVAL_CANDIDATES:
        trial = replace_params(params, cand)
        run = ev.measure(
            shim, searcher, list(evalset),
            min_score=trial.min_score, per_section=trial.per_section,
            dense_only=trial.dense_only, filters=scope, query_vectors=vectors,
        )
        better, why = ev.is_real_improvement(baseline, run, margin)
        notes.append(f"{cand.label}: MRR@10={ev.objective(run):.3f} — {why}")
        rounds.append(
            {
                "candidate": cand.label,
                "mrr_at_10": round(ev.objective(run), 4),
                "recall_at_5": round(run.recall_at_5, 4),
                "accepted": better,
                "why": why,
            }
        )
        if better and ev.objective(run) > ev.objective(best_run):
            best_label, best_run, best_params = cand.label, run, trial

    if best_label is not None:
        notes.append(
            f"adopting {best_label}: MRR@10 {ev.objective(baseline):.3f} -> "
            f"{ev.objective(best_run):.3f} — costs nothing, the index is unchanged"
        )
        return TuneDecision(
            kind="retrieval", label=best_label, retrieval=best_params,
            baseline_objective=ev.objective(baseline), margin=margin,
            history=rounds, notes=notes,
        )

    cand = ev.next_chunk_candidate(profile, rounds)
    if cand is None:
        notes.append("no candidate left to try; keeping the current parameters")
        return TuneDecision(
            kind="none", baseline_objective=ev.objective(baseline), margin=margin,
            history=rounds, notes=notes,
        )

    from dataclasses import replace as _replace

    overrides = {
        k: v
        for k, v in (
            ("target_chars", cand.target_chars),
            ("overlap_chars", cand.overlap_chars),
        )
        if v is not None
    }
    notes.append(
        f"the free knobs are exhausted; {cand.label} is the next candidate, and it "
        "re-chunks and re-embeds — a full second indexing pass"
    )
    rounds.append(
        {
            "candidate": cand.label, "mrr_at_10": None, "recall_at_5": None,
            "accepted": None, "why": "pending reindex",
        }
    )
    return TuneDecision(
        kind="chunking", label=cand.label,
        chunk_rules=_replace(profile.chunk_rules, **overrides),
        baseline_objective=ev.objective(baseline), margin=margin,
        history=rounds, notes=notes,
    )


def replace_params(params: RetrievalParams, candidate: Any) -> RetrievalParams:
    """`params` with whatever the candidate overrides, and nothing else."""
    from dataclasses import replace as _replace

    return _replace(
        params,
        **{
            k: v
            for k, v in (
                ("min_score", candidate.min_score),
                ("per_section", candidate.per_section),
                ("dense_only", candidate.dense_only),
            )
            if v is not None
        },
    )


def judge_candidate(decision: TuneDecision, measured: Any) -> tuple[bool, str]:
    """Whether a chunking candidate earned the pass it just cost.

    Judged against the margin computed *before* it ran, not against a fresh one:
    a margin derived from the candidate's own run would move with it, and the
    comparison would be between two things that both changed.
    """
    now = ev.objective(measured)
    delta = now - decision.baseline_objective
    keep = delta > decision.margin
    detail = (
        f"MRR@10 {decision.baseline_objective:.3f} -> {now:.3f} ({delta:+.3f}) "
        f"vs margin ±{decision.margin:.3f}"
    )
    return keep, f"{detail} — {'keep' if keep else 'revert'}"
