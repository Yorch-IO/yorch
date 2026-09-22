"""Run one question through retrieval's gates and account for every candidate.

The impure half of `proberetrieval.py`, which is deliberately pure and says
so: it decides verdicts, and this module is what reads the stores to hand it
the numbers. Extracted from `scripts/probe_retrieval.py` on 2026-09-21 so a
route could serve it — until then the whole tool was reachable from a shell,
a workspace variable, a tenant id and a chunk id, and from no screen.

Two things it reproduces exactly, because a probe that explained a retrieval
production never ran would be worse than none: the constants come from
`answering/retrieve.py` and the widths from `answering/effort.py`, never
restated; and the reranker runs where `retrieve.search` runs it — after the
topicality gate, at the levels whose `Budget.rerank` is set — so the order a
reader sees is the order the model would have read.

What it adds over the script's original report is the thing RAGFlow's
retrieval-testing screen shows and ours could not: **every candidate with its
legs pulled apart** — dense rank and cosine, sparse rank and BM25, RRF rank,
and the reranker's score when one ran — rather than one fused number. A hit
that scored well on one leg and badly on the other is a different diagnosis
from one that scored middling on both, and the fused figure hides which.

**Spends, and reports it, and records nothing.** One query embedding (usually
cached) and, at a level that reranks, one ranking query at list price. There
is no run row to hang a cost row off, exactly like `POST /provider/probe`, so
the figure travels in the report for the screen to print — a sandbox that
spent a tenth of a cent should say so rather than look free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config
from . import proberetrieval as pr
from .answering.effort import DEFAULT_EFFORT, budget_for
from .answering.retrieve import MIN_SCORE, PER_SECTION

#: How deep each leg looks when placing a chunk. Only the *reported* rank
#: depends on it — a chunk outside the window is "unranked within N", never
#: "last" — and `scripts/probe_retrieval.py` explains why a rank from an HNSW
#: index is approximate at any depth.
DEFAULT_DEPTH = 500

#: Characters of a candidate's text carried in the report. Enough to recognise
#: a passage; the full text is one `/chunks/{id}/context` away.
PREVIEW_CHARS = 240


@dataclass(frozen=True)
class ProbeRequest:
    question: str
    library_id: str
    tenant_id: str
    #: The chunk to place, or empty for the ranked view alone.
    chunk_id: str = ""
    version_id: str = ""
    effort: str = DEFAULT_EFFORT
    depth: int = DEFAULT_DEPTH


def _ids(hits: list[Any]) -> list[str]:
    return [str(h.payload.get("chunk_id") or "") for h in hits]


def _score_of(hits: list[Any], target: str) -> "float | None":
    for h in hits:
        if str(h.payload.get("chunk_id") or "") == target:
            return float(h.score)
    return None


def probe(settings: config.Settings, req: ProbeRequest, provider: Any = None) -> dict[str, Any]:
    """`provider` is injectable for the reason `retrieve.search` takes one: the
    tests hand in a double that answers with a vector already in the index, so
    a probe is exercised end to end against the real stores without a network
    call. The route passes nothing and gets the real one."""
    from docagent.bm25 import tokenize
    from docagent.qdrant import Qdrant, SearchOpts, diversify

    from .activities.paid import EMBED_WORKERS, CachedEmbedder, _embed_cache_dir
    from .indexing import version_scope
    from .providers import Provider, ProviderError
    from .providers.gemini import RETRIEVAL_QUERY
    from .providers.ranking import usd_for

    budget = budget_for(req.effort)
    filters: dict[str, str] = {"library_id": req.library_id, "tenant_id": req.tenant_id}
    if req.version_id:
        filters = {**filters, **version_scope(req.tenant_id, req.version_id)}

    provider = provider or Provider(settings.gemini)
    embedder = CachedEmbedder(
        provider, _embed_cache_dir(settings),
        model=settings.gemini.embedding_model,
        dimensions=settings.gemini.embedding_dimensions,
        workers=EMBED_WORKERS,
    )
    vector = embedder.embed_many([req.question], task_type=RETRIEVAL_QUERY)[0].values

    with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
        # Each leg on its own, with no floor, to place chunks. `min_score` is
        # left off deliberately: the question is "where does this rank", and a
        # floor would answer "nowhere" for exactly the chunks worth explaining.
        dense_hits = q.search(
            vector, SearchOpts(limit=req.depth, min_score=0.0, dense_only=True,
                               query_text=req.question, filters=filters),
        )
        sparse_hits = q.search(
            vector, SearchOpts(limit=req.depth, sparse_only=True,
                               query_text=req.question, filters=filters),
        )
        # The gate production runs, at full width, so `dense_supported` is the
        # same count `retrieve.search` reports as `evidence.dense`.
        gate = q.search(
            vector, SearchOpts(limit=budget.top_k, min_score=MIN_SCORE, dense_only=True,
                               query_text=req.question, filters=filters),
        )
        # And the search production actually issues.
        fused = q.search(
            vector, SearchOpts(limit=budget.candidate_limit, min_score=MIN_SCORE,
                               query_text=req.question, filters=filters,
                               prefetch_limit=budget.prefetch_limit),
        )

    on_topic = bool(gate)
    rrf_order = _ids(fused)

    # Reranked exactly where and when `retrieve.search` does it. A failure is
    # reported as such rather than hidden: the screen is explaining production,
    # and "the reranker was skipped" is a fact about this retrieval.
    rerank_scores: dict[str, float] = {}
    rerank_note = ""
    rerank_usd = 0.0
    ordered = list(fused)
    if on_topic and budget.rerank and settings.gemini.rerank_model and fused:
        try:
            ranked = provider.rank(req.question, [h.payload.get("text") or "" for h in fused])
            rerank_scores = dict(zip(rrf_order, ranked.scores))
            ordered = sorted(fused, key=lambda h: -rerank_scores.get(str(h.payload.get("chunk_id") or ""), 0.0))
            rerank_usd = usd_for(ranked.records)
        except ProviderError as e:
            rerank_note = f"reranking skipped, fused order kept ({e.kind}): {e}"

    delivered = diversify(ordered, PER_SECTION, budget.top_k) if on_topic else []
    delivered_ids = _ids(delivered)
    dense_ids, sparse_ids = _ids(dense_hits), _ids(sparse_hits)
    final_order = _ids(ordered)

    candidates = []
    for h in ordered:
        cid = str(h.payload.get("chunk_id") or "")
        text = h.payload.get("text") or ""
        candidates.append({
            "chunk_id": cid,
            "source": h.payload.get("source_title"),
            "breadcrumb": h.payload.get("breadcrumb"),
            "kind": h.payload.get("kind"),
            "preview": text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else ""),
            "rrf_rank": pr.rank_of(rrf_order, cid),
            "rank": pr.rank_of(final_order, cid),
            "dense_rank": pr.rank_of(dense_ids, cid),
            "dense_score": _score_of(dense_hits, cid),
            "sparse_rank": pr.rank_of(sparse_ids, cid),
            "sparse_score": _score_of(sparse_hits, cid),
            "rerank_score": rerank_scores.get(cid),
            "delivered_rank": pr.rank_of(delivered_ids, cid),
        })

    report: dict[str, Any] = {
        "question": req.question,
        "effort": req.effort,
        "scope": filters,
        "query_terms": list(tokenize(req.question)),
        "on_topic": on_topic,
        "dense_supported": len(gate),
        "served_with": {
            "min_score": MIN_SCORE,
            "prefetch_limit": budget.prefetch_limit,
            "candidate_limit": budget.candidate_limit,
            "per_section": PER_SECTION,
            "top_k": budget.top_k,
            "effort": req.effort,
            "reranked": bool(rerank_scores),
            "rerank_model": settings.gemini.rerank_model if budget.rerank else "",
            "rerank_note": rerank_note,
            "depth": req.depth,
        },
        "candidates": candidates,
        "target": None,
        "spent": {
            "embedding_input_tokens": embedder.usage.input_tokens,
            "cache_hits": embedder.cache_hits,
            "rerank_usd": rerank_usd,
            "recorded": False,
        },
    }

    if req.chunk_id:
        target = req.chunk_id
        dense = pr.leg(
            "dense", rank=pr.rank_of(dense_ids, target), score=_score_of(dense_hits, target),
            prefetch_limit=budget.prefetch_limit, floor=MIN_SCORE, searched=len(dense_hits),
        )
        sparse = pr.leg(
            "sparse", rank=pr.rank_of(sparse_ids, target), score=_score_of(sparse_hits, target),
            prefetch_limit=budget.prefetch_limit, searched=len(sparse_hits),
        )
        verdict = pr.probe(
            question=req.question, terms=report["query_terms"], target=target,
            dense=dense, sparse=sparse,
            # The position in the list `diversify` actually saw — reranked when
            # production reranked — because the `diversify` gate is about
            # neighbours in *that* order. Whether the chunk was inside the
            # candidate list at all is unchanged by reordering it.
            fused_rank=pr.rank_of(final_order, target),
            candidate_limit=budget.candidate_limit,
            delivered_rank=pr.rank_of(delivered_ids, target),
            top_k=budget.top_k,
        )
        verdict["rrf_rank"] = pr.rank_of(rrf_order, target)
        verdict["rerank_score"] = rerank_scores.get(target)
        if not on_topic:
            verdict["note"] = (
                "the question itself was refused as off-corpus: nothing cleared "
                "the dense floor, so no chunk was delivered"
            )
        report["target"] = verdict

    return report
