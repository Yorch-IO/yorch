#!/usr/bin/env python3
"""What parent-child retrieval could buy, and what it would cost. ≈$0.

    cd worker
    export BRAIN_WORKSPACE_DIR=… BRAIN_QDRANT_URL=… BRAIN_DATABASE_URL=… \
           BRAIN_GEMINI_PROJECT_ID=…
    uv run python scripts/parent_ceiling.py ver_… [ver_… …] [--rerank] [--json out.json]

RAGFlow indexes small children for matching and serves the *parent* to the
model (`rag/nlp/search.py::retrieval_by_children`). The whole recall benefit is
that a near miss becomes a hit: if the retriever returns chunk 41 and the answer
is in 42, serving the unit that holds both captures it. That is measurable
against the index we already have, before anything is re-chunked:

    ceiling(level, parent) = parent_recall@top_k − chunk_recall@top_k

**The plan this came from assumed the candidate costs a full re-embedding pass
to evaluate, and for a *window* parent that is wrong.** A window is
`chunk_index // k` — the parent is assembled at read time out of chunks that are
already indexed, so nothing is embedded and nothing is written. Only a parent
whose text is itself indexed would need that pass, and this says whether one
would ever be worth building.

Three legs, because the first two can each kill the idea on their own.

* **Is there a sane parent at all?** The structural parent is `(chapter,
  section)`, and `build_chunks` consumes a heading paragraph — so a document
  whose headings were never detected really is one untitled section. This
  reports the distribution before quoting any recall.
* **The ceiling**, per level and per parent definition.
* **The curve it has to beat.** A parent costs characters, and so does a wider
  `top_k`. "Recall went up" is not a result when the budget went up with it, so
  the plain-chunk sweep is measured over the same questions and the comparison
  is read at *equal characters*, never at equal unit count.

`--rerank` reruns the ceiling through the ordering production actually serves.
It is not free (`$1.00 / 1,000` queries, about $1.20 for 640 questions at two
levels) and it is worth it, because reranking improves the chunk-level ordering
and should therefore *shrink* the ceiling — which is a prediction, and this is
where it stops being one.

**Nothing here writes.** Every eval question's vector is already in
`docagent.embedcache` from the `evaluating` stage, so the embedding leg reports
its spend rather than assuming it is zero.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import audit_version as auditscript  # noqa: E402

from brainworker.answering.effort import BUDGETS  # noqa: E402
from brainworker.answering.retrieve import MIN_SCORE, PER_SECTION  # noqa: E402
from brainworker.artifacts import ArtifactStore  # noqa: E402
from brainworker.indexing import version_scope  # noqa: E402

#: Parent definitions. `section` is the structural one; `wN` groups N
#: consecutive chunks, which is the definition that always exists — and the one
#: that needs no re-embedding, since the unit is assembled from chunks already
#: indexed.
MODES = ("section", "w2", "w3", "w4", "w6")

#: Widths for the plain-chunk curve. It reaches past `thorough` deliberately:
#: the recorded citation measurement turns between 48 and 64, so a parent config
#: that only beats a width nobody would ship has not beaten anything.
SWEEP = (4, 6, 8, 12, 16, 24, 32, 48, 64, 96)


def parent_of(payload: dict, mode: str) -> tuple:
    """The parent a chunk belongs to under one definition."""
    if mode == "section":
        return ("s", payload.get("chapter") or "", payload.get("section") or "")
    return ("w", int(payload.get("chunk_index", -1)) // int(mode[1:]))


def reordered(ranker: Any, question: str, hits: list, spent: dict) -> list:
    """The fused list in the reranker's order, or unchanged when there is none.

    **The one place either leg may rerank.** Both legs must rerank or neither
    may: the ceiling leg reranking while the plain-chunk curve did not was a
    live defect in this script, and it handed the reranker's own +0.094 to the
    parent — on one book it turned every row of the verdict from "plain wins or
    ties" into "parent wins by +0.05 to +0.075", four times the noise margin,
    printed with no sign that anything was wrong. A comparison that looks sound
    and silently compares two different things is the failure this whole file
    exists to avoid making about parent-child, so it must not be reachable by
    writing the call twice.
    """
    if ranker is None or not hits:
        return hits
    ranked = ranker.rank(question, [h.payload.get("text") or "" for h in hits])
    spent["rank_queries"] += ranked.queries
    spent["rank_usd"] += ranked.usd
    return [h for _, h in sorted(zip(ranked.scores, hits), key=lambda t: -t[0])]


def _load(settings: Any, version: str) -> tuple | None:
    with auditscript._catalog(settings.database_url) as cat:
        runs = auditscript._runs_for(cat, version)
    if not runs:
        return None
    run_id = auditscript._run_with(runs, "evalset.json", settings)
    if not run_id:
        return None
    tenant = (auditscript._newest_succeeded(runs) or runs[0]).tenant_id
    store = ArtifactStore(settings.workspace, run_id)
    raw = json.loads((store.run_dir / "evalset.json").read_text("utf-8"))
    return run_id, tenant, raw


def measure(settings: Any, versions: list[str], rerank: bool = False) -> dict[str, Any]:
    from docagent.profiles import EvalItem
    from docagent.qdrant import Qdrant, SearchOpts, diversify
    from brainworker.activities.paid import (
        EMBED_WORKERS, CachedEmbedder, _embed_cache_dir, _provider,
    )
    from brainworker.providers.gemini import RETRIEVAL_QUERY

    ranker = None
    spent = {"rank_queries": 0, "rank_usd": 0.0}
    if rerank:
        from brainworker.providers.ranking import Ranker
        ranker = Ranker(settings.gemini.project_id)

    embedder = CachedEmbedder(
        _provider(), _embed_cache_dir(settings),
        model=settings.gemini.embedding_model,
        dimensions=settings.gemini.embedding_dimensions,
        workers=EMBED_WORKERS,
    )
    levels = ("brief", "standard") if rerank else tuple(BUDGETS)
    ceiling = {
        lv: {"n": 0, "chunk": 0, "chars": 0,
             "parent": {m: 0 for m in MODES}, "parent_chars": {m: 0 for m in MODES}}
        for lv in levels
    }
    sweep = {k: {"hit": 0, "chars": 0} for k in SWEEP}
    shapes: list[dict[str, Any]] = []
    asked = 0

    with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
        for version in versions:
            loaded = _load(settings, version)
            if not loaded:
                continue
            _run, tenant, raw = loaded
            items = [EvalItem(**d) for d in raw]
            scope = version_scope(tenant, version)
            points = [r["payload"] for r in q.scroll(scope)]
            if not points:
                continue

            # Leg 1: what the parents would be, before any recall is quoted.
            sizes: dict[str, dict[tuple, int]] = {m: {} for m in MODES}
            counts: dict[str, dict[tuple, int]] = {m: {} for m in MODES}
            for p in points:
                for m in MODES:
                    key = parent_of(p, m)
                    sizes[m][key] = sizes[m].get(key, 0) + len(p.get("text") or "")
                    counts[m][key] = counts[m].get(key, 0) + 1
            shapes.append({
                "version_id": version, "chunks": len(points),
                "sections": len(sizes["section"]),
                "biggest_section_chunks": max(counts["section"].values()),
                "biggest_section_chars": max(sizes["section"].values()),
                "whole_document_is_one_section": len(sizes["section"]) == 1,
            })

            questions = [it.question for it in items]
            vectors = dict(zip(questions, (e.values for e in embedder.embed_many(
                questions, task_type=RETRIEVAL_QUERY))))

            for it in items:
                asked += 1
                vec = vectors[it.question]
                target = next((p for p in points if it.matches(p)), None)

                # Leg 3: the plain-chunk curve, fused once at the widest budget.
                #
                # **It is reranked exactly when the parent leg is.** Without
                # this the verdict compares a reranked parent against an
                # unreranked baseline and hands the reranker's own +0.094 to
                # the parent — a confidently wrong answer from a comparison
                # that looks sound. Reranking the one wide fused list covers
                # every `k` below it, so honesty here costs one extra call per
                # question and not one per width.
                wide = BUDGETS["thorough"]
                wide_opts = SearchOpts(
                    limit=wide.candidate_limit, min_score=MIN_SCORE,
                    query_text=it.question, filters=scope,
                    prefetch_limit=wide.prefetch_limit)
                if q.topicality_gate(vec, wide_opts):
                    fused = q.search(vec, wide_opts)
                    fused = reordered(ranker, it.question, fused, spent)
                    for k in SWEEP:
                        served = diversify(fused, PER_SECTION, k)
                        sweep[k]["chars"] += sum(len(h.payload.get("text") or "") for h in served)
                        if any(it.matches(h.payload) for h in served):
                            sweep[k]["hit"] += 1

                # Leg 2: the ceiling, at each level's own budget.
                for lv in levels:
                    b = BUDGETS[lv]
                    acc = ceiling[lv]
                    acc["n"] += 1
                    opts = SearchOpts(
                        limit=b.candidate_limit, min_score=MIN_SCORE,
                        query_text=it.question, filters=scope,
                        prefetch_limit=b.prefetch_limit)
                    if not q.topicality_gate(vec, opts):
                        continue
                    hits = q.search(vec, opts)
                    # Exactly where `retrieve.search` puts it: after RRF has
                    # chosen the candidates and before `diversify` chooses
                    # which of them are read.
                    hits = reordered(ranker, it.question, hits, spent)
                    served = diversify(hits, PER_SECTION, b.top_k)
                    acc["chars"] += sum(len(h.payload.get("text") or "") for h in served)
                    if any(it.matches(h.payload) for h in served):
                        acc["chunk"] += 1
                    for m in MODES:
                        family = {parent_of(h.payload, m) for h in served}
                        acc["parent_chars"][m] += sum(sizes[m][f] for f in family)
                        if target is not None and parent_of(target, m) in family:
                            acc["parent"][m] += 1

    spent["embedding_tokens"] = embedder.usage.input_tokens
    return {"questions": asked, "reranked": bool(ranker), "shapes": shapes,
            "ceiling": ceiling, "sweep": sweep, "spent": spent}


def _plain_at(sweep: dict, n: int, budget: float) -> tuple[int, float] | None:
    """The widest plain-chunk setting that fits inside `budget` characters.

    Read at equal *characters*, never at equal unit count: a parent is bigger by
    construction, so comparing 8 parents with 8 chunks compares two prompts of
    different sizes and calls the larger one better. What a parent has to beat
    is what the same characters buy when spent on more chunks — which is the one
    comparison that can tell a real gain from a bigger budget.
    """
    # Keys are read by name rather than by value: a `--json` written by one run
    # and read back by another arrives with its integer keys turned into
    # strings, and a bare `sweep[k]` then raises on the saved file while working
    # in memory — a report that only breaks for the reader who kept it.
    rows = {int(k): v for k, v in sweep.items()}
    best = None
    for k in sorted(rows):
        if rows[k]["chars"] / n <= budget:
            best = (k, rows[k]["hit"] / n)
    return best


def _verdict(r: dict[str, Any]) -> None:
    """Every parent configuration against the plain chunks its budget would buy."""
    n = r["questions"] or 1
    sweep = r["sweep"]
    tag = " (both sides reranked)" if r["reranked"] else ""
    print(f"--- at equal characters, which wins?{tag} ---")
    print(f"  {'level':9} {'parent':8} {'chars':>9} {'parent':>8} {'plain':>8} "
          f"{'at k':>5} {'delta':>8}")
    for level, acc in r["ceiling"].items():
        m_n = acc["n"] or 1
        for m in MODES:
            budget = acc["parent_chars"][m] / m_n
            plain = _plain_at(sweep, n, budget)
            if plain is None:
                # Cheaper than the narrowest plain setting: nothing to compare.
                continue
            k, plain_recall = plain
            parent_recall = acc["parent"][m] / m_n
            delta = parent_recall - plain_recall
            mark = "parent" if delta > 0 else "plain"
            print(f"  {level:9} {m:8} {budget:9,.0f} {parent_recall:8.4f} "
                  f"{plain_recall:8.4f} {k:5} {delta:+8.4f}  {mark}")
    print("\n  The pooled bootstrap margin over these eight books is about "
          "\u00b10.014;\n  anything inside it is a tie, not a win.\n")


def report(r: dict[str, Any]) -> None:
    shapes = r["shapes"]
    if shapes:
        degenerate = [s for s in shapes if s["whole_document_is_one_section"]]
        print("--- is there a sane parent? ---")
        print(f"  documents measured                      : {len(shapes)}")
        print(f"  whose whole text is ONE (chapter,section): {len(degenerate)}")
        print(f"  biggest single section                  : "
              f"{max(s['biggest_section_chunks'] for s in shapes)} chunks, "
              f"{max(s['biggest_section_chars'] for s in shapes):,} characters")
        print(f"  median sections per document            : "
              f"{statistics.median(s['sections'] for s in shapes):.0f}\n")

    sweep = r["sweep"]
    n = r["questions"] or 1
    print("--- the curve a parent has to beat (plain chunks) ---")
    print(f"  {'top_k':>6} {'recall':>8} {'chars/question':>16}")
    for k in SWEEP:
        print(f"  {k:6} {sweep[k]['hit'] / n:8.4f} {sweep[k]['chars'] / n:16,.0f}")
    print()

    for level, acc in r["ceiling"].items():
        m_n = acc["n"] or 1
        tag = " (reranked)" if r["reranked"] else ""
        print(f"--- {level}{tag}  top_k={BUDGETS[level].top_k} ---")
        print(f"  chunk recall {acc['chunk'] / m_n:.4f}   "
              f"chars {acc['chars'] / m_n:,.0f}/question")
        for m in MODES:
            p, pc = acc["parent"][m], acc["parent_chars"][m]
            print(f"  parent={m:8} recall {p / m_n:.4f}  ceiling {(p - acc['chunk']) / m_n:+.4f}"
                  f"   chars {pc / m_n:,.0f} ({pc / max(acc['chars'], 1):.1f}x)")
        print()
    _verdict(r)
    s = r["spent"]
    print(f"embedding tokens {s['embedding_tokens']}"
          + ("  (all cached)" if not s["embedding_tokens"] else "  — real spend"))
    if s["rank_queries"]:
        print(f"ranking queries  {s['rank_queries']}  ≈ ${s['rank_usd']:.4f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("versions", nargs="+")
    ap.add_argument("--rerank", action="store_true",
                    help="measure against production's reranked order (spends)")
    ap.add_argument("--json", dest="out")
    args = ap.parse_args(argv)

    r = measure(auditscript._settings(), args.versions, rerank=args.rerank)
    report(r)
    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(r, indent=2, ensure_ascii=False, default=str), "utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
