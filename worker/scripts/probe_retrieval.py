#!/usr/bin/env python3
"""Why one chunk did or did not reach an answer. Read-only.

    cd worker
    export BRAIN_WORKSPACE_DIR=~/.local/share/io.sek.companybrain/workspace
    export BRAIN_QDRANT_URL=http://127.0.0.1:6433

    uv run python scripts/probe_retrieval.py "¿Quién fue Jesucristo?" \
        --library lib_teologia --tenant tnt_… --chunk chk_80a04dd78031ef67992a9b44

`answering/retrieve.py` reports what it found. It cannot report what it *missed*
and why, because by the time a chunk is absent from a fused result there is no
longer anything to point at: RRF returns ranks, so a chunk missing from the
output could have been below the dense floor, outside either prefetch, beaten in
the fusion, or dropped by `diversify` — four states with four different fixes,
and every one of them looks identical from the outside.

This walks the same gates in the same order, one leg at a time, and names the
first one that excluded the chunk. It exists because the alternative is what
produced it: a dozen ad-hoc `docker exec … python -c` one-liners whose numbers
nobody could reproduce a week later.

**Nothing here writes.** The only paid call is the question's own embedding, and
`docagent.embedcache` keys on (model, width, task, text) — so the second probe
of a question costs nothing, and the report says which it was rather than
assuming.

**A rank from this tool is approximate, and the report says so.** Qdrant's dense
leg is an HNSW graph, so a rank is a function of how deep the search looked:
measured on the canonical case, the same chunk placed at 316 with `--depth 400`,
317 at 500 and 318 at 1000 and 2000, converging as more of the graph is
explored. That is the index behaving correctly, not a defect, and it is why the
depth travels beside every rank instead of being dropped once the rank is known.
It does not reach the verdict — the comparisons that decide one are against a
prefetch width of 50 and a candidate list of 40, and a chunk in the 300s is on
the same side of both whichever of those three numbers is right — but a reader
told "rank 317" with no window would be entitled to believe the 7.

Two things it deliberately does not do. It does not decide whether the chunk
*should* have been retrieved — that is a judgement about the corpus, and a tool
that answered it would invite tuning against one anecdote. And it does not
compare against a stored measurement: the ranks here are of the index as it
stands now, which is the only thing that can explain today's answer.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from brainworker import config, proberetrieval as pr  # noqa: E402
from brainworker.indexing import version_scope  # noqa: E402

#: The served values, imported rather than restated. A probe that carried its
#: own copy of `MIN_SCORE` would keep agreeing with production right up until
#: somebody changed one of them, and would then explain a retrieval that never
#: happened.
from brainworker.answering.retrieve import (  # noqa: E402
    MIN_SCORE,
    PER_SECTION,
)
from brainworker.answering.effort import (  # noqa: E402
    DEFAULT_EFFORT,
    EFFORT_LEVELS,
    budget_for,
)

#: How deep to look when placing the chunk. Only affects the *reported* rank:
#: nothing here changes what a search returns, and a chunk outside this window
#: is reported as unranked within it rather than as ranked last.
DEFAULT_DEPTH = 500

#: What `Question.top_k` defaults to, at the default effort level.
#:
#: Derived rather than restated since levels exist: a probe run against a
#: question that was asked at `thorough` and defaulted to 8 here would explain
#: a retrieval half the size of the real one. `--effort` moves all three of the
#: figures the level owns together.
DEFAULT_TOP_K = budget_for(DEFAULT_EFFORT).top_k


def _ids(hits: list[Any]) -> list[str]:
    return [str(h.payload.get("chunk_id") or "") for h in hits]


def _score_of(hits: list[Any], target: str) -> "float | None":
    for h in hits:
        if str(h.payload.get("chunk_id") or "") == target:
            return float(h.score)
    return None


def probe(
    settings: config.Settings,
    *,
    question: str,
    target: str,
    filters: dict[str, str],
    min_score: float,
    prefetch_limit: int,
    candidate_limit: int,
    per_section: int,
    top_k: int,
    depth: int,
    effort: str = DEFAULT_EFFORT,
) -> dict[str, Any]:
    from docagent.bm25 import tokenize
    from docagent.qdrant import Qdrant, SearchOpts, diversify
    from brainworker.activities.paid import (
        EMBED_WORKERS,
        CachedEmbedder,
        _embed_cache_dir,
        _provider,
    )
    from brainworker.providers.gemini import RETRIEVAL_QUERY

    embedder = CachedEmbedder(
        _provider(),
        _embed_cache_dir(settings),
        model=settings.gemini.embedding_model,
        dimensions=settings.gemini.embedding_dimensions,
        workers=EMBED_WORKERS,
    )
    vector = embedder.embed_many([question], task_type=RETRIEVAL_QUERY)[0].values

    with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
        # Each leg on its own, with no floor, to place the chunk. `min_score` is
        # left off deliberately: the question here is "where does this rank",
        # and a floor would answer "nowhere" for exactly the chunks worth
        # explaining.
        dense_hits = q.search(
            vector,
            SearchOpts(limit=depth, min_score=0.0, dense_only=True,
                       query_text=question, filters=filters),
        )
        sparse_hits = q.search(
            vector,
            SearchOpts(limit=depth, sparse_only=True,
                       query_text=question, filters=filters),
        )
        # And then the search production actually issues.
        fused = q.search(
            vector,
            SearchOpts(limit=candidate_limit, min_score=min_score,
                       query_text=question, filters=filters,
                       prefetch_limit=prefetch_limit),
        )

    delivered = diversify(fused, per_section, top_k)

    dense = pr.leg(
        "dense",
        rank=pr.rank_of(_ids(dense_hits), target),
        score=_score_of(dense_hits, target),
        prefetch_limit=prefetch_limit,
        floor=min_score,
        searched=len(dense_hits),
    )
    sparse = pr.leg(
        "sparse",
        rank=pr.rank_of(_ids(sparse_hits), target),
        score=_score_of(sparse_hits, target),
        prefetch_limit=prefetch_limit,
        searched=len(sparse_hits),
    )

    report = pr.probe(
        question=question,
        terms=tokenize(question),
        target=target,
        dense=dense,
        sparse=sparse,
        fused_rank=pr.rank_of(_ids(fused), target),
        candidate_limit=candidate_limit,
        delivered_rank=pr.rank_of(_ids(delivered), target),
        top_k=top_k,
    )
    report["served_with"] = {
        "min_score": min_score,
        "prefetch_limit": prefetch_limit,
        "candidate_limit": candidate_limit,
        "per_section": per_section,
        "top_k": top_k,
        "effort": effort,
        # Compared against *the named level's* budget, not against the module
        # constants. Those describe `standard` only, so checking against them
        # would report "differs from production" for every faithful probe of a
        # question asked at any other level — and the note it prints is what a
        # reader uses to decide whether the explanation applies at all.
        # `min_score` and `per_section` stay on the constants because no level
        # scales them.
        "matches_production": (
            min_score == MIN_SCORE
            and per_section == PER_SECTION
            and candidate_limit == budget_for(effort).candidate_limit
            and prefetch_limit == budget_for(effort).prefetch_limit
        ),
    }
    report["scope"] = filters
    report["delivered"] = [
        {
            "chunk_id": h.payload.get("chunk_id"),
            "source": h.payload.get("source_title"),
            "breadcrumb": h.payload.get("breadcrumb"),
            "score": round(float(h.score), 4),
        }
        for h in delivered
    ]
    # Reported, not assumed: a non-zero figure here is real spend, and a zero
    # means the cache answered.
    report["spent"] = {
        "embedding_input_tokens": embedder.usage.input_tokens,
        "cache_hits": embedder.cache_hits,
    }
    return report


def render(report: dict[str, Any]) -> str:
    out: list[str] = []
    v = report["verdict"]
    served = report["served_with"]

    out.append(f"question : {report['question']}")
    out.append(f"target   : {report['target']}")
    out.append(f"scope    : {report['scope']}")
    terms = ", ".join(report["query_terms"]) or "(ninguno)"
    note = "  <-- one term: BM25 cannot rank by relevance here" if report["single_term_query"] else ""
    out.append(f"bm25 terms: [{terms}]{note}")
    if not served["matches_production"]:
        out.append("NOTE: parameters differ from what production serves")
    out.append("")

    for name in ("dense", "sparse"):
        leg = report[name]
        # The window travels with the rank. An HNSW rank moves by a place or two
        # with the depth searched, so a bare integer would claim a precision the
        # index does not offer.
        rank = f"{leg['rank']}~" if leg["rank"] is not None else f">{leg['searched']}"
        score = "-" if leg["score"] is None else f"{leg['score']:.6f}"
        bits = [f"rank {rank} of {leg['searched']}", f"score {score}"]
        if leg["floor"] is not None:
            bits.append(f"floor {leg['floor']} -> {'clears' if leg['clears_floor'] else 'REFUSED'}")
        bits.append(f"prefetch {leg['prefetch_limit']} -> {'in' if leg['in_prefetch'] else 'OUT'}")
        out.append(f"  {name:<7}: " + " | ".join(bits))

    fr = report["fused_rank"]
    out.append(f"  {'fused':<7}: rank {fr if fr is not None else 'absent'} "
               f"of {report['candidate_limit']} candidates")
    out.append("  (~ marks an HNSW rank: approximate, and it drifts with --depth)")
    out.append("")

    if v["reached"]:
        out.append(f"VERDICT: delivered, at position {v['delivered_rank']} of {report['top_k']}")
    else:
        out.append(f"VERDICT: lost at {v['lost_at']} -- {v['detail']}")
        out.append(f"         {v['remedy']}")
        if v.get("note"):
            out.append(f"         {v['note']}")

    out.append("")
    out.append(f"delivered evidence ({len(report['delivered'])}):")
    for i, d in enumerate(report["delivered"], 1):
        out.append(f"  {i}. {d['score']:<8} {str(d['source'])[:34]:<34} {str(d['breadcrumb'] or '')[:32]}")

    s = report["spent"]
    out.append("")
    out.append(f"spent: {s['embedding_input_tokens']} embedding input tokens "
               f"({s['cache_hits']} cache hit(s))")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Explain why one chunk did or did not reach an answer. Read-only.",
        epilog="SPENDS: one query embedding, and nothing if the cache already holds it.",
    )
    p.add_argument("question")
    p.add_argument("--chunk", required=True, help="the chunk_id to place, e.g. chk_…")
    p.add_argument("--library", required=True)
    p.add_argument("--tenant", required=True)
    p.add_argument("--version", default="", help="narrow the scope to one version")
    p.add_argument("--min-score", type=float, default=MIN_SCORE)
    p.add_argument("--effort", choices=EFFORT_LEVELS, default=DEFAULT_EFFORT,
                   help="the level the question was asked at; sets the defaults "
                        "for --prefetch, --candidates and --top-k")
    p.add_argument("--prefetch", type=int, default=None,
                   help="per-leg prefetch width (default: the level's)")
    p.add_argument("--candidates", type=int, default=None)
    p.add_argument("--per-section", type=int, default=PER_SECTION)
    p.add_argument("--top-k", type=int, default=None)
    p.add_argument("--depth", type=int, default=DEFAULT_DEPTH,
                   help="how deep to look when placing the chunk")
    p.add_argument("--json", dest="json_out", default="")
    args = p.parse_args()

    from docagent.qdrant import PREFETCH_LIMIT

    settings = config.load()
    filters = {"library_id": args.library, "tenant_id": args.tenant}
    if args.version:
        filters = {**filters, **version_scope(args.tenant, args.version)}

    # An explicit flag always wins; the level supplies the rest, so a probe of a
    # question asked at `thorough` explains that retrieval rather than a
    # standard one it never ran.
    budget = budget_for(args.effort)

    report = probe(
        settings,
        question=args.question,
        target=args.chunk,
        filters=filters,
        min_score=args.min_score,
        prefetch_limit=(
            args.prefetch if args.prefetch is not None else budget.prefetch_limit
        ),
        candidate_limit=(
            args.candidates if args.candidates is not None else budget.candidate_limit
        ),
        per_section=args.per_section,
        top_k=args.top_k if args.top_k is not None else budget.top_k,
        depth=args.depth,
        effort=args.effort,
    )

    print(render(report))
    if args.json_out:
        pathlib.Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
