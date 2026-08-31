"""Synthetic evaluation and parameter tuning.

Self-improvement needs a number to improve. There is no labelled ground truth for
these documents, so it is generated: for a sampled chunk, ask Gemini for a
question that only that passage answers, then measure whether retrieval brings
that chunk back. That gives recall@k and MRR without human labelling, for cents.

**The bias, stated plainly.** A question generated from a chunk inherits that
chunk's vocabulary, which flatters lexical (BM25) retrieval — the eval would
reward hybrid search partly for a similarity the eval itself created. Three
mitigations, and the third is the one that matters:

1. The prompt demands paraphrase and forbids reusing the passage's distinctive
   terms.
2. Neighbouring chunks are named as distractors the question must not also fit.
3. **Every report gives dense-only and hybrid metrics side by side.** If hybrid's
   advantage here is much larger than in the structural diagnostics, that gap
   *is* the leakage, made visible instead of hidden.

Tuning accepts a parameter change only when it clears a bootstrap noise margin,
because a recall difference of one or two questions out of forty is noise.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Callable

from .chunk import KIND_TABLE_ROW, Chunk
from .profiles import EvalItem, Scores
from .qdrant import Qdrant, SearchOpts, diversify
from .vertex import TASK_QUERY, Vertex

DEFAULT_SAMPLE = 40
BOOTSTRAP_ROUNDS = 400
# A change must beat the noise margin by this factor to count as real.
ACCEPT_MARGIN = 1.0

# Off-topic and nonsense queries establish what "no real match" scores. Reused
# from the Go diagnostics so the two suites remain comparable.
NOISE_QUERIES = (
    "recetas de cocina italiana con berenjena",
    "cómo cambiar el aceite de un motor diésel",
    "xkcd qwerty zzzz plugh",
    "el mantenimiento de bicicletas de montaña",
)

EVALSET_SYSTEM = """Eres un evaluador que construye un conjunto de prueba para un buscador semántico.

Recibirás un fragmento de un documento y, como contexto, los fragmentos vecinos.

Escribe UNA pregunta en español que se responda con el fragmento principal y NO con los vecinos.

Reglas estrictas:
- PARAFRASEA. No reutilices las palabras más distintivas del fragmento; si el fragmento dice "soberanía de las esferas", pregunta por la idea sin copiar el término exacto siempre que sea posible.
- La pregunta debe ser específica: si podría responderse con cualquier parte del documento, no sirve.
- La pregunta NO debe poder responderse con los fragmentos vecinos que se te dan.
- Una sola pregunta, sin numeración, sin comentarios.
- Devuelve únicamente el JSON pedido."""

EVALSET_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "answerable_only_by_main": {"type": "boolean"},
    },
    "required": ["question", "answerable_only_by_main"],
}


# --- building the eval set ---------------------------------------------------


def build_evalset(
    vertex: Vertex,
    chunks: list[Chunk],
    sample: int = DEFAULT_SAMPLE,
    seed: int = 20260726,
    progress: "Callable[[int, int], None] | None" = None,
) -> list[EvalItem]:
    """Generate questions from a stratified sample of chunks.

    Stratified by ``kind`` so a table-heavy or footnote-heavy document is not
    evaluated purely on its prose, and seeded so tuning rounds compare on the same
    questions — comparing on a fresh sample each round would measure the sample,
    not the change.
    """
    if not chunks:
        return []

    picks = _stratified_sample(chunks, sample, seed)
    by_index = {c.index: c for c in chunks}
    items: list[EvalItem] = []

    for n, c in enumerate(picks, start=1):
        if progress:
            progress(n, len(picks))
        neighbours = [
            by_index[i].text[:300]
            for i in (c.index - 1, c.index + 1)
            if i in by_index and i != c.index
        ]
        payload = {
            "fragmento_principal": c.text[:2000],
            "ruta": c.breadcrumb(),
            "fragmentos_vecinos_que_no_debe_responder": neighbours,
        }
        try:
            raw = vertex.generate(
                json.dumps(payload, ensure_ascii=False),
                system=EVALSET_SYSTEM,
                stage="evalset",
                json_schema=EVALSET_SCHEMA,
                temperature=0.4,
            )
            d = json.loads(raw)
        except Exception:
            # A failed question is a smaller eval set, not a failed run.
            continue
        q = (d.get("question") or "").strip()
        # Reject questions the model itself flags as ambiguous: an item whose
        # answer also sits in a neighbour would score retrieval as wrong when it
        # was right.
        if q and d.get("answerable_only_by_main", True):
            items.append(
                EvalItem(
                    question=q,
                    chunk_index=c.index,
                    # Byte midpoint of the source passage: survives re-chunking,
                    # unlike the index. -1 for structured sources with no span.
                    char_mid=(
                        (c.char_from + c.char_to) // 2 if c.char_to > c.char_from else -1
                    ),
                    cell_ref=c.cell_ref,
                    breadcrumb=c.breadcrumb(),
                )
            )

    return items


def _stratified_sample(chunks: list[Chunk], sample: int, seed: int) -> list[Chunk]:
    rng = random.Random(seed)
    by_kind: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_kind.setdefault(c.kind, []).append(c)

    per_kind = max(1, sample // max(1, len(by_kind)))
    picks: list[Chunk] = []
    for kind, group in sorted(by_kind.items()):
        # Table rows are near-identical to each other by construction; sampling
        # many of them measures the same thing repeatedly.
        want = min(len(group), per_kind if kind != KIND_TABLE_ROW else max(1, per_kind // 2))
        picks.extend(rng.sample(group, want))

    if len(picks) < sample:
        rest = [c for c in chunks if c not in picks]
        picks.extend(rng.sample(rest, min(sample - len(picks), len(rest))))
    return picks[:sample]


# --- measuring ---------------------------------------------------------------


@dataclass
class EvalRun:
    """One measurement of a configuration, with enough detail to explain itself."""

    hits_at_1: int = 0
    hits_at_5: int = 0
    reciprocal_ranks: list[float] = field(default_factory=list)
    questions: int = 0
    misses: list[tuple[str, int, int]] = field(default_factory=list)  # q, want, got_rank

    @property
    def recall_at_1(self) -> float:
        return self.hits_at_1 / self.questions if self.questions else 0.0

    @property
    def recall_at_5(self) -> float:
        return self.hits_at_5 / self.questions if self.questions else 0.0

    @property
    def mrr(self) -> float:
        return sum(self.reciprocal_ranks) / self.questions if self.questions else 0.0

    def rr_vector(self) -> list[float]:
        """Reciprocal rank per question, with 0.0 for misses.

        ``reciprocal_ranks`` only records hits, so bootstrapping needs the misses
        made explicit.
        """
        misses = self.questions - len(self.reciprocal_ranks)
        return list(self.reciprocal_ranks) + [0.0] * max(0, misses)


def measure(
    vertex: Vertex,
    qdrant: Qdrant,
    evalset: list[EvalItem],
    *,
    min_score: float,
    per_section: int,
    dense_only: bool,
    doc_id: str | None = None,
    filters: dict[str, str] | None = None,
    top_k: int = 10,
    query_vectors: dict[str, list[float]] | None = None,
) -> EvalRun:
    """Run the eval set against one retrieval configuration.

    ``query_vectors`` lets the caller embed each question once and reuse it across
    configurations — otherwise a four-way comparison pays for the same embeddings
    four times, and the comparison would also be muddied by embedding nondeterminism.

    ``doc_id`` is the CLI's way of scoping to one document in a collection this
    engine owns. ``filters`` is the general form, and it exists because the
    product's collection is scoped by ``tenant_id`` and ``version_id`` instead —
    the engine has no concept of either and must not acquire one. Passing both
    merges them, with ``filters`` losing to nothing: the caller that supplies
    them is the one that knows what the collection is keyed on.
    """
    run = EvalRun(questions=len(evalset))
    filters = _scope(doc_id, filters)

    for item in evalset:
        vec = (query_vectors or {}).get(item.question)
        if vec is None:
            vec = vertex.embed(item.question, TASK_QUERY, stage="eval_query").values

        opts = SearchOpts(
            limit=top_k * (5 if per_section > 0 else 1),
            min_score=min_score,
            dense_only=dense_only,
            query_text=item.question,
            filters=filters,
        )
        if not dense_only and not qdrant.topicality_gate(vec, opts):
            run.misses.append((item.question, item.chunk_index, -1))
            continue

        hits = diversify(qdrant.search(vec, opts), per_section, top_k)
        # Matched by byte offset, not by index: see EvalItem.matches.
        rank = next(
            (i for i, h in enumerate(hits, start=1) if item.matches(h.payload)), 0
        )
        if rank:
            run.reciprocal_ranks.append(1.0 / rank)
            if rank == 1:
                run.hits_at_1 += 1
            if rank <= 5:
                run.hits_at_5 += 1
            else:
                run.misses.append((item.question, item.chunk_index, rank))
        else:
            run.misses.append((item.question, item.chunk_index, -1))
    return run


def _scope(
    doc_id: str | None, filters: "dict[str, str] | None"
) -> dict[str, str]:
    """The payload filter a measurement runs under.

    Both forms are accepted because the two collections this engine writes are
    keyed differently: `docagent_*` on `doc_id`, the product's `brain` on
    `tenant_id` + `version_id`. A measurement that forgot the scope would search
    the whole collection and score a document against another one's chunks.
    """
    scope = dict(filters or {})
    if doc_id:
        scope["doc_id"] = doc_id
    return scope


def noise_floor(
    vertex: Vertex,
    qdrant: Qdrant,
    doc_id: str | None = None,
    filters: "dict[str, str] | None" = None,
) -> float:
    """Highest dense score any off-topic query achieves — what "no match" looks
    like. Measured on the dense leg because RRF scores are not comparable to a
    cosine (invariant #8)."""
    filters = _scope(doc_id, filters)
    best = 0.0
    for q in NOISE_QUERIES:
        vec = vertex.embed(q, TASK_QUERY, stage="eval_noise").values
        hits = qdrant.search(
            vec,
            SearchOpts(limit=1, min_score=0.0, dense_only=True, query_text=q, filters=filters),
        )
        if hits:
            best = max(best, hits[0].score)
    return best


def to_scores(hybrid: EvalRun, dense: EvalRun, floor: float, chunks: int) -> Scores:
    return Scores(
        recall_at_1=round(hybrid.recall_at_1, 4),
        recall_at_5=round(hybrid.recall_at_5, 4),
        mrr_at_10=round(hybrid.mrr, 4),
        recall_at_5_dense_only=round(dense.recall_at_5, 4),
        noise_floor=round(floor, 4),
        chunks=chunks,
        eval_questions=hybrid.questions,
    )


def leakage_note(hybrid: EvalRun, dense: EvalRun) -> str:
    """Say out loud how much of hybrid's advantage may be the eval's own doing."""
    gap = hybrid.recall_at_5 - dense.recall_at_5
    if hybrid.questions == 0:
        return "no eval questions were generated"
    if gap <= 0.02:
        return (
            f"hybrid and dense-only are within {gap:+.3f} recall@5 — no sign the "
            "synthetic questions are leaking vocabulary to the lexical leg"
        )
    return (
        f"hybrid beats dense-only by {gap:+.3f} recall@5. Some of that is real "
        "(exact-term recall) and some is this eval's vocabulary leakage: the "
        "questions were written from the chunks they are meant to find. Trust the "
        "structural diagnostics for the size of the real gain."
    )


# --- bootstrap noise margin --------------------------------------------------


def noise_margin(run: EvalRun, rounds: int = BOOTSTRAP_ROUNDS, seed: int = 7) -> float:
    """How much the tuning objective wobbles from resampling alone.

    The objective is **MRR@10**, not recall@5. Measured on a 24-question eval,
    recall@5 was identical (0.875) for every configuration tried — overlap 0 and
    300, target 900, 1200 and 1600 — while MRR@10 moved from 0.708 to 0.755 and
    recall@1 from 0.542 to 0.667. recall@5 is a coarse binary at a generous k: one
    question is worth 0.042, the margin is 0.072, so nothing short of a two-question
    swing can ever be accepted, and real improvements are invisible. MRR is graded,
    so a passage moving from rank 4 to rank 2 registers.
    """
    n = run.questions
    if n < 2:
        return 1.0
    outcomes = run.rr_vector()
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(rounds):
        means.append(sum(rng.choice(outcomes) for _ in range(n)) / n)
    mean = sum(means) / len(means)
    var = sum((m - mean) ** 2 for m in means) / len(means)
    return var**0.5


def objective(run: EvalRun) -> float:
    """What tuning maximises: MRR@10. See noise_margin for why not recall@5."""
    return run.mrr


def is_real_improvement(
    baseline: EvalRun, candidate: EvalRun, margin: float | None = None
) -> tuple[bool, str]:
    """Accept a change only if it beats the bootstrap noise margin."""
    m = margin if margin is not None else noise_margin(baseline)
    delta = objective(candidate) - objective(baseline)
    threshold = m * ACCEPT_MARGIN
    detail = (
        f"MRR@10 {delta:+.3f} (recall@5 "
        f"{candidate.recall_at_5 - baseline.recall_at_5:+.3f})"
    )
    if delta > threshold:
        return True, f"{detail} beats the {threshold:.3f} noise margin"
    return False, (
        f"{detail} is within the {threshold:.3f} noise margin (bootstrap), "
        "so it is not a real gain"
    )


# --- the tuning grid ---------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A parameter change to try. Chunking changes require re-embedding; retrieval
    changes do not, which is why they are tried first."""

    label: str
    # Retrieval-only knobs (cheap: no re-embedding).
    min_score: float | None = None
    per_section: int | None = None
    dense_only: bool | None = None
    # Chunking knobs (expensive: re-chunk and re-embed).
    target_chars: int | None = None
    overlap_chars: int | None = None

    @property
    def needs_reindex(self) -> bool:
        return self.target_chars is not None or self.overlap_chars is not None


# Ordered cheapest-first. The retrieval knobs alone recovered diversity from
# 3/10 to 8/10 sections in the Go work, so they earn the first look.
RETRIEVAL_CANDIDATES = (
    Candidate("per_section=1", per_section=1),
    Candidate("per_section=3", per_section=3),
    Candidate("min_score=0.55", min_score=0.55),
    Candidate("min_score=0.65", min_score=0.65),
)

CHUNK_CANDIDATES = (
    Candidate("target=900", target_chars=900),
    Candidate("target=1600", target_chars=1600),
    Candidate("overlap=300", overlap_chars=300),
    Candidate("overlap=0", overlap_chars=0),
)


def next_chunk_candidate(profile, history: list[dict]) -> "Candidate | None":
    """The next chunking candidate not already tried, and not a no-op.

    A candidate whose value already matches the current profile would spend a
    full re-embed to measure something already known — and a chunking change is
    the expensive half of tuning: every chunk is embedded again.
    """
    tried = {h["candidate"] for h in history}
    rules = profile.chunk_rules
    for cand in CHUNK_CANDIDATES:
        if cand.label in tried:
            continue
        if cand.target_chars is not None and cand.target_chars == rules.target_chars:
            continue
        if cand.overlap_chars is not None and cand.overlap_chars == rules.overlap_chars:
            continue
        return cand
    return None
