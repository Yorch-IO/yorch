"""Why one chunk did or did not reach an answer. Pure, so it can be tested.

The same testability decision `auditversion.py` embodies, and for the same
reason: what is worth asserting here is the *verdict* — which gate dropped a
chunk — and a test that needed a Qdrant standing up to check an integer
comparison would run rarely enough to be worth nothing. Nothing in this module
opens a socket; `scripts/probe_retrieval.py` does the reading and hands the
numbers here.

**The verdict names the first gate that excluded it, not every gate it failed.**
A chunk below the dense floor is also, necessarily, outside the dense prefetch
and outside the fused candidates, and reporting all three as findings buries the
one that matters. The remedies differ per gate — a floor is a threshold, a
prefetch is a width, `diversify` is a policy about neighbours — so naming the
wrong one sends somebody to tune a parameter that was never in the way.

The gates, in the order `answering/retrieve.py` applies them:

1. the dense floor (`min_score`), on the dense prefetch only;
2. the two prefetches (`prefetch_limit` each), which feed RRF independently —
   a chunk in *either* one reaches fusion, so failing one is not yet a loss;
3. the fused candidate list (`CANDIDATE_LIMIT`);
4. `diversify`, which caps chunks per section and truncates to `top_k`.
"""

from __future__ import annotations

from typing import Any, Sequence

#: What a leg reports when the chunk was not in the window it was asked for.
#: Distinguished from a rank of 0 on purpose: "ranked below where we looked" and
#: "ranked first" must never be the same value, and an `Optional[int]` is the
#: only shape that cannot be quietly summed.
UNRANKED = None


def rank_of(ids: Sequence[str], target: str) -> "int | None":
    """1-based position of `target`, or `None` when it is not in `ids`.

    1-based because every rank this file compares against — a prefetch width, a
    candidate limit, `top_k` — is a count. Mixing a 0-based position with a
    count is how an off-by-one becomes a wrong verdict rather than a crash.
    """
    for i, value in enumerate(ids, 1):
        if value == target:
            return i
    return UNRANKED


def leg(
    name: str,
    *,
    rank: "int | None",
    score: "float | None",
    prefetch_limit: int,
    floor: "float | None" = None,
    searched: int = 0,
) -> dict[str, Any]:
    """One retrieval leg's account of a chunk.

    `floor` is the dense leg's `min_score` and is `None` for the sparse leg,
    because `SearchOpts.min_score` is applied to the dense prefetch and never to
    the sparse one — invariant #8, which is what lets a lexical match answer a
    question no vector cleared. Passing a floor here for BM25 would report a
    gate that does not exist.

    `searched` is how deep the probe actually looked. A chunk absent from a
    window of 400 is reported as unranked *within 400*, never as "worst" — the
    depth is part of the finding, since a wider look could still place it.
    """
    clears = None if floor is None else (score is not None and score >= floor)
    in_prefetch = rank is not None and rank <= prefetch_limit
    # A chunk below the dense floor is excluded from the dense prefetch whatever
    # its rank, because the threshold is applied inside that prefetch. Reporting
    # `in_prefetch: true` off the rank alone would contradict the leg's own
    # verdict two keys higher up.
    if clears is False:
        in_prefetch = False
    return {
        "leg": name,
        "rank": rank,
        "score": score,
        "searched": searched,
        "prefetch_limit": prefetch_limit,
        "floor": floor,
        "clears_floor": clears,
        "in_prefetch": in_prefetch,
    }


def where_lost(
    *,
    dense: dict[str, Any],
    sparse: dict[str, Any],
    fused_rank: "int | None",
    candidate_limit: int,
    delivered_rank: "int | None",
    top_k: int,
) -> dict[str, Any]:
    """The first gate that excluded the chunk, and what would have to change.

    Returns `reached: True` and `lost_at: None` when the chunk survived to the
    evidence the model was given. Everything else names one gate.

    The one case worth stating outright, because it is the case that reads as
    two failures and is one: when neither prefetch carried the chunk, RRF never
    saw it at all. Fusion cannot rank what it was not handed, so the finding is
    `prefetch` — not `fusion`, which would send somebody to look at a fusion
    parameter that had nothing to do with it.
    """
    if delivered_rank is not None:
        return {
            "reached": True,
            "lost_at": None,
            "delivered_rank": delivered_rank,
            "remedy": "",
        }

    if not dense["in_prefetch"] and not sparse["in_prefetch"]:
        # Name the dense floor only when it is what did the excluding. A chunk
        # that clears the floor and is simply ranked too deep is a width
        # problem, and lowering the floor would not move it by one place.
        if dense["clears_floor"] is False:
            reason = (
                f"dense: {dense['score']} < floor {dense['floor']}; "
                f"sparse: rank {sparse['rank']} > prefetch {sparse['prefetch_limit']}"
            )
            remedy = (
                "the floor excludes it from the dense prefetch, and the sparse "
                "leg does not rank it high enough to carry it — lowering the "
                "floor alone cannot admit it while its dense rank stays past "
                f"{dense['prefetch_limit']}"
            )
        else:
            reason = (
                f"dense: rank {dense['rank']} > prefetch {dense['prefetch_limit']}; "
                f"sparse: rank {sparse['rank']} > prefetch {sparse['prefetch_limit']}"
            )
            remedy = "neither leg ranks it inside its prefetch width"
        return {
            "reached": False,
            "lost_at": "prefetch",
            "detail": reason,
            "remedy": remedy,
            "note": "RRF never saw this chunk: fusion cannot rank what no leg handed it",
        }

    if fused_rank is None or fused_rank > candidate_limit:
        return {
            "reached": False,
            "lost_at": "fusion",
            "detail": (
                f"reached a prefetch but fused rank {fused_rank} is past "
                f"CANDIDATE_LIMIT {candidate_limit}"
            ),
            "remedy": "it competes in RRF and loses; a wider candidate list would admit it",
        }

    return {
        "reached": False,
        "lost_at": "diversify",
        "detail": (
            f"fused rank {fused_rank} is within {candidate_limit}, but it is not "
            f"in the {top_k} delivered"
        ),
        "remedy": (
            "dropped by the per-section cap or by truncation to top_k — a policy "
            "about its neighbours, not about its own score"
        ),
    }


def probe(
    *,
    question: str,
    terms: Sequence[str],
    target: str,
    dense: dict[str, Any],
    sparse: dict[str, Any],
    fused_rank: "int | None",
    candidate_limit: int,
    delivered_rank: "int | None",
    top_k: int,
) -> dict[str, Any]:
    """The whole account of one (question, chunk) pair.

    `terms` is what survived `docagent.bm25.tokenize` — carried because it is
    the sparse leg's entire input and is invisible from the score alone. A
    question that reduces to one term cannot be ranked by BM25 in any useful
    sense: with a single term the score is length-normalised term frequency, so
    it separates long chunks from short ones rather than relevant from
    irrelevant. That is a property of the *question*, and no index change fixes
    it — which is exactly the kind of finding a score would have hidden.
    """
    return {
        "question": question,
        "target": target,
        "query_terms": list(terms),
        "single_term_query": len(terms) <= 1,
        "dense": dense,
        "sparse": sparse,
        "fused_rank": fused_rank,
        "candidate_limit": candidate_limit,
        "top_k": top_k,
        "verdict": where_lost(
            dense=dense,
            sparse=sparse,
            fused_rank=fused_rank,
            candidate_limit=candidate_limit,
            delivered_rank=delivered_rank,
            top_k=top_k,
        ),
    }
