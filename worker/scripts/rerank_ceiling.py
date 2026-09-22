#!/usr/bin/env python3
"""How much a perfect reranker could recover. Read-only, ≈$0.

    cd worker
    export BRAIN_WORKSPACE_DIR=… BRAIN_QDRANT_URL=… BRAIN_DATABASE_URL=… \
           BRAIN_GEMINI_PROJECT_ID=…
    uv run python scripts/rerank_ceiling.py ver_… [ver_… …] [--json out.json]

`answering/retrieve.py` fuses `candidate_limit` hits and then `diversify` picks
`top_k` of them by RRF rank and a per-section cap. A reranker would sit between
those two steps, so the most it can ever do is promote a chunk that is *inside*
the fused list and *outside* the served `top_k`. That gap is measurable before
any ranking model exists, from the eval sets already on disk:

    ceiling(level) = recall@candidate_limit − recall@top_k

A small gap means the candidate dies for free. A large one says how much is on
the table and at which level, which is what decides whether a ranking call is
worth its latency on a path where the embedding alone was measured swinging
0.4 s to 18.8 s.

**Nothing here writes.** The only paid call is each question's embedding, and
`docagent.embedcache` already holds every question the `evaluating` stage
asked, so the run reports its spend rather than assuming it is zero. The scope
is the version's own, the same predicate `runner.evaluate` refuses to run
without.

**Ranks are approximate in the same way `probe_retrieval.py` says they are** —
Qdrant's dense leg is an HNSW graph — and the widths compared against here are
the served ones, which is what makes the comparison honest rather than exact.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import audit_version as auditscript  # noqa: E402

from brainworker.answering.effort import BUDGETS  # noqa: E402
from brainworker.answering.retrieve import MIN_SCORE, PER_SECTION  # noqa: E402
from brainworker.artifacts import ArtifactStore  # noqa: E402
from brainworker.indexing import version_scope  # noqa: E402


#: Sourced 2026-09-21 from cloud.google.com/generative-ai-app-builder/pricing:
#: "Ranking $1.00 / 1,000 count … A query is defined as having up to 100
#: documents … 132 documents to rank = 2 queries". A third-party-free figure,
#: which is rarer here than it should be — and still a list price, not a bill.
RANK_USD_PER_1000_QUERIES = 1.00
RANK_DOCS_PER_QUERY = 100


class Ranker:
    """One POST per question to Vertex AI's Ranking API, ADC-authenticated.

    No key and no new dependency: `google-auth` is already installed and
    `httpx` is a direct dependency. `locations/global`, like every other call
    this product makes. Scores come back in [0, 1] already, so — following the
    contract in RAGFlow's `rerank_model.py::_normalize_rank` — a calibrated
    provider is passed through unchanged rather than min-maxed again.
    """

    def __init__(self, project: str, model: str) -> None:
        import google.auth
        import google.auth.transport.requests
        import httpx

        self._creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        self._refresh = google.auth.transport.requests.Request()
        self._client = httpx.Client(timeout=60)
        self.project = project
        self.model = model
        self.url = (
            f"https://discoveryengine.googleapis.com/v1/projects/{project}"
            "/locations/global/rankingConfigs/default_ranking_config:rank"
        )
        self.calls = 0
        self.queries = 0
        self.retries = 0
        self.seconds: list[float] = []

    def rank(self, question: str, hits: list[Any]) -> list[Any]:
        if not hits:
            return hits
        if not self._creds.valid:
            self._creds.refresh(self._refresh)
        records = [
            {"id": str(i), "content": h.payload.get("text") or ""}
            for i, h in enumerate(hits)
        ]
        t0 = time.perf_counter()
        # A 502 arrived 200-odd calls into the first real run. The embedder's
        # recorded lesson applies: a bounded, backing-off retry on the statuses
        # that mean "not you", and the latency figure includes the waiting,
        # because a caller would have waited too.
        for attempt in range(5):
            r = self._client.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self._creds.token}",
                    "x-goog-user-project": self.project,
                },
                json={"model": self.model, "query": question, "records": records,
                      "topN": len(records)},
            )
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                self.retries += 1
                time.sleep(2 ** attempt)
                continue
            break
        self.seconds.append(time.perf_counter() - t0)
        r.raise_for_status()
        self.calls += 1
        self.queries += math.ceil(len(records) / RANK_DOCS_PER_QUERY)
        scored = {int(rec["id"]): rec["score"] for rec in r.json()["records"]}
        order = sorted(range(len(hits)), key=lambda i: -scored.get(i, 0.0))
        return [hits[i] for i in order]

    @property
    def usd(self) -> float:
        return self.queries * RANK_USD_PER_1000_QUERIES / 1000


def _rank(hits: list[Any], item: Any) -> int:
    """1-based rank of the wanted chunk in `hits`, 0 when absent — matched by
    byte offset, exactly as `evaluate.measure` does, so a re-cut cannot move it."""
    return next((i for i, h in enumerate(hits, start=1) if item.matches(h.payload)), 0)


def measure_version(settings: Any, version: str, ranker: "Ranker | None" = None,
                    rerank_levels: tuple[str, ...] = ()) -> dict[str, Any]:
    from docagent.profiles import EvalItem
    from docagent.qdrant import Qdrant, SearchOpts, diversify
    from brainworker.activities.paid import (
        EMBED_WORKERS, CachedEmbedder, _embed_cache_dir, _provider,
    )
    from brainworker.providers.gemini import RETRIEVAL_QUERY

    with auditscript._catalog(settings.database_url) as cat:
        runs = auditscript._runs_for(cat, version)
    if not runs:
        return {"version_id": version, "available": False, "detail": "no run names this version"}
    run_id = auditscript._run_with(runs, "evalset.json", settings)
    if not run_id:
        return {"version_id": version, "available": False, "detail": "no evalset.json on disk"}
    tenant_id = (auditscript._newest_succeeded(runs) or runs[0]).tenant_id

    store = ArtifactStore(settings.workspace, run_id)
    items = [EvalItem(**d) for d in json.loads((store.run_dir / "evalset.json").read_text("utf-8"))]
    scope = version_scope(tenant_id, version)

    embedder = CachedEmbedder(
        _provider(), _embed_cache_dir(settings),
        model=settings.gemini.embedding_model,
        dimensions=settings.gemini.embedding_dimensions,
        workers=EMBED_WORKERS,
    )

    # One row per (level, question): the wanted chunk's rank in the fused list
    # and in the served list, so the ceiling is a difference of two recalls
    # over the same questions rather than two separate measurements.
    per_level: dict[str, dict[str, Any]] = {}
    with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
        questions = [it.question for it in items]
        vectors = dict(zip(questions, (e.values for e in embedder.embed_many(questions, task_type=RETRIEVAL_QUERY))))
        for level, budget in BUDGETS.items():
            fused_ranks: list[int] = []
            served_ranks: list[int] = []
            reranked_ranks: list[int] = []
            reranked_undiversified: list[int] = []
            gated = 0
            do_rerank = ranker is not None and level in rerank_levels
            for it in items:
                vec = vectors[it.question]
                opts = SearchOpts(
                    limit=budget.candidate_limit, min_score=MIN_SCORE,
                    query_text=it.question, filters=scope,
                    prefetch_limit=budget.prefetch_limit,
                )
                if not q.topicality_gate(vec, opts):
                    gated += 1
                    fused_ranks.append(0)
                    served_ranks.append(0)
                    reranked_ranks.append(0)
                    reranked_undiversified.append(0)
                    continue
                fused = q.search(vec, opts)
                served = diversify(fused, PER_SECTION, budget.top_k)
                fused_ranks.append(_rank(fused, it))
                served_ranks.append(_rank(served, it))
                if do_rerank:
                    # The reranker orders the *fused* list — the same candidates
                    # `diversify` would have seen — and then the same section
                    # cap applies, so the only thing that moved is the order.
                    ordered = ranker.rank(it.question, list(fused))
                    reranked_ranks.append(_rank(diversify(ordered, PER_SECTION, budget.top_k), it))
                    reranked_undiversified.append(_rank(ordered[: budget.top_k], it))
            n = len(items)
            in_fused = sum(1 for r in fused_ranks if r)
            in_served = sum(1 for r in served_ranks if r)
            # Where the reachable-but-unserved chunks sit, so a reranker's job
            # is described as "promote from ranks 9–40" and not as a percentage.
            promotable = sorted(f for f, s in zip(fused_ranks, served_ranks) if f and not s)
            per_level[level] = {
                "top_k": budget.top_k,
                "candidate_limit": budget.candidate_limit,
                "recall_at_top_k": round(in_served / n, 4),
                "recall_at_candidates": round(in_fused / n, 4),
                "ceiling": round((in_fused - in_served) / n, 4),
                "promotable": len(promotable),
                "promotable_ranks": promotable,
                "gated_off_corpus": gated,
                "beyond_candidates": n - in_fused - gated,
            }
            if do_rerank:
                in_rr = sum(1 for r in reranked_ranks if r)
                in_rr_flat = sum(1 for r in reranked_undiversified if r)
                per_level[level].update({
                    "reranked": ranker.model,
                    "recall_at_top_k_reranked": round(in_rr / n, 4),
                    "delta_reranked": round((in_rr - in_served) / n, 4),
                    "recall_at_top_k_reranked_no_diversify": round(in_rr_flat / n, 4),
                    "mrr_at_top_k": round(sum(1 / r for r in served_ranks if r) / n, 4),
                    "mrr_at_top_k_reranked": round(sum(1 / r for r in reranked_ranks if r) / n, 4),
                })
    return {
        "version_id": version,
        "run_id": run_id,
        "available": True,
        "questions": len(items),
        "levels": per_level,
        "spent": {
            "embedding_input_tokens": embedder.usage.input_tokens,
            "note": "0 means every query vector was already in the embedding cache",
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("version_ids", nargs="+")
    p.add_argument("--json", type=pathlib.Path, help="write the full report here")
    p.add_argument("--rerank", metavar="MODEL",
                   help="ALSO rerank the fused candidates with Vertex AI's Ranking API "
                        "and measure recall after it. Spends: $1 per 1,000 queries, "
                        "one query per 100 candidates per question — say which levels "
                        "with --levels.")
    p.add_argument("--levels", default="brief,standard",
                   help="effort levels to rerank (default: brief,standard — thorough's "
                        "ceiling is usually inside the margin, measure it on purpose)")
    args = p.parse_args()

    settings = auditscript._settings()
    ranker = Ranker(settings.gemini.project_id, args.rerank) if args.rerank else None
    levels = tuple(x.strip() for x in args.levels.split(",") if x.strip())
    reports = [measure_version(settings, v, ranker, levels) for v in args.version_ids]

    print(f"{'version':<28} {'level':<9} {'r@top_k':>8} {'r@cand':>8} {'ceiling':>8} {'promo':>6} {'gated':>6} {'beyond':>7}")
    for r in reports:
        if not r["available"]:
            print(f"{r['version_id']:<28} unavailable: {r['detail']}")
            continue
        for level, s in r["levels"].items():
            print(f"{r['version_id']:<28} {level:<9} {s['recall_at_top_k']:>8.4f} {s['recall_at_candidates']:>8.4f} "
                  f"{s['ceiling']:>+8.4f} {s['promotable']:>6} {s['gated_off_corpus']:>6} {s['beyond_candidates']:>7}")
    ok = [r for r in reports if r["available"]]
    if len(ok) > 1:
        print()
        for level in BUDGETS:
            n = sum(r["questions"] for r in ok)
            served = sum(r["levels"][level]["recall_at_top_k"] * r["questions"] for r in ok)
            fused = sum(r["levels"][level]["recall_at_candidates"] * r["questions"] for r in ok)
            print(f"pooled {level:<9} n={n} r@top_k={served / n:.4f} r@cand={fused / n:.4f} ceiling={(fused - served) / n:+.4f}")
    if ranker:
        print()
        for level in levels:
            rows = [r for r in ok if "recall_at_top_k_reranked" in r["levels"].get(level, {})]
            if not rows:
                continue
            n = sum(r["questions"] for r in rows)
            base = sum(r["levels"][level]["recall_at_top_k"] * r["questions"] for r in rows)
            rr = sum(r["levels"][level]["recall_at_top_k_reranked"] * r["questions"] for r in rows)
            flat = sum(r["levels"][level]["recall_at_top_k_reranked_no_diversify"] * r["questions"] for r in rows)
            mb = sum(r["levels"][level]["mrr_at_top_k"] * r["questions"] for r in rows)
            mr = sum(r["levels"][level]["mrr_at_top_k_reranked"] * r["questions"] for r in rows)
            print(f"reranked {level:<9} n={n} r@top_k {base / n:.4f} -> {rr / n:.4f} ({(rr - base) / n:+.4f}) "
                  f"| without diversify {flat / n:.4f} | mrr {mb / n:.4f} -> {mr / n:.4f}")
        secs = sorted(ranker.seconds)
        if secs:
            print(f"ranking calls: {ranker.calls} (+{ranker.retries} retried), queries billed: {ranker.queries}, "
                  f"≈${ranker.usd:.4f} at list price; latency p50 {secs[len(secs) // 2]:.2f}s "
                  f"p90 {secs[int(len(secs) * 0.9)]:.2f}s max {secs[-1]:.2f}s")
    spent = sum(r["spent"]["embedding_input_tokens"] for r in ok)
    print(f"\nspent: {spent} embedding input tokens" + (" (all cached)" if spent == 0 else " — REAL SPEND"))
    if args.json:
        args.json.write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
