#!/usr/bin/env python3
"""How many refusals are recoverable? Read-only, ≈$0.

    cd worker
    export BRAIN_WORKSPACE_DIR=… BRAIN_QDRANT_URL=… BRAIN_DATABASE_URL=… \
           BRAIN_GEMINI_PROJECT_ID=…
    uv run python scripts/refusal_census.py ver_… [ver_… …] [--json out.json]

A second retrieval round — re-ask a refused question with a rewritten query
rather than returning `off_corpus` — costs a generation call per refusal on the
stage that is already 87% of every dollar this product has spent. Whether it is
worth that depends on one number nobody had: **how many of our refusals are
wrong**.

The durable record cannot answer it. `activities/asking.py::_record` maps a
refusal onto `succeeded` — correctly, because "the corpus does not cover this"
is an answer and not a fault — so all 44 `ask` runs in this catalog carry no
outcome at all, and `run.error_kind` is not the place to put one either
(`workflows/ingest.py::_outcome` records why: a succeeded row carrying an error
kind is the contradiction `20260831140000_run_blocked` was created to remove).
`conversation_turn` is the only durable refusal record and held **seven**, five
of them predating `_settle` carrying a reason.

So the census synthesises the sample instead of counting one, and it can,
because the eval sets are questions whose answer is **known to be in the
corpus**. Every one the gate refuses is a false refusal by construction. That
makes the recoverable-refusal rate measurable today, for nothing, over 640
questions rather than seven.

Three columns decide the feature, and the second and third are why counting
refusals alone would have been misleading:

* `refused` — questions the dense floor turned away although the corpus holds
  the answer. An upper bound on what any second round could recover.
* `target_rank` — where the wanted chunk actually sits in a deep dense probe of
  its own library. A rewrite moves the *query*; it cannot reach a passage the
  embedding space does not connect to the question at all. A target beyond the
  deep probe is not recoverable by rewriting, only by re-chunking or
  re-embedding.
* `best` — the top score the library offered. Every refusal sitting just under
  the floor is a floor artefact; a refusal whose best chunk scores far below it
  is an honest one.

**Library scope is the number that matters, and version scope is reported
beside it.** `retrieve.search` is scoped to a library, so that is what a
person's question meets; `rerank_ceiling.py` gates at version scope because a
recall measurement must be scoped to the document it is about
(`runner.evaluate` refuses an unscoped one). The two differ by three times in
this corpus, and quoting the wrong one overstates the problem.

**Nothing here writes**, and the only paid call is each question's embedding —
already in `docagent.embedcache` from the `evaluating` stage, so the run reports
its spend rather than assuming it is zero.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import audit_version as auditscript  # noqa: E402

from brainworker.answering.effort import budget_for  # noqa: E402
from brainworker.answering.retrieve import MIN_SCORE  # noqa: E402
from brainworker.artifacts import ArtifactStore  # noqa: E402
from brainworker.indexing import version_scope  # noqa: E402

#: How deep to look for a refused question's own target. Far past anything the
#: product serves: the question is not "would it have been returned" — it was
#: refused — but "is this passage anywhere near this question at all", which is
#: what separates a floor artefact from a gap a rewrite cannot close.
DEEP = 300

#: The level whose `top_k` the gate probes at. `search` runs the probe at the
#: resolved width, so the census has to name one; `standard` is the level that
#: reproduces what the product served before the ladder existed.
LEVEL = "standard"


def _scopes(tenant_id: str, library_id: str, version: str) -> dict[str, dict]:
    """The two scopes, each carrying what production carries.

    `disabled` is copied from `retrieve.search` rather than omitted: a chunk
    somebody hid is hidden from every question, so a census that ignored it
    would report a refusal the product would never make. It is an exclusion and
    never `enabled: true`, for the reason recorded there — absence means
    visible, which is what every point written before the flag existed says.
    """
    from docagent.qdrant import Excluded

    library = {"tenant_id": tenant_id, "library_id": library_id,
               "disabled": Excluded(True)}
    version_only = dict(version_scope(tenant_id, version))
    version_only["disabled"] = Excluded(True)
    return {"library": library, "version": version_only}


def census_version(settings: Any, version: str, deep: int = DEEP) -> dict[str, Any]:
    from docagent.profiles import EvalItem
    from docagent.qdrant import Qdrant, SearchOpts
    from brainworker.activities.paid import (
        EMBED_WORKERS, CachedEmbedder, _embed_cache_dir, _provider,
    )
    from brainworker.providers.gemini import RETRIEVAL_QUERY

    with auditscript._catalog(settings.database_url) as cat:
        runs = auditscript._runs_for(cat, version)
    if not runs:
        return {"version_id": version, "available": False,
                "detail": "no run names this version"}
    run_id = auditscript._run_with(runs, "evalset.json", settings)
    if not run_id:
        return {"version_id": version, "available": False,
                "detail": "no evalset.json on disk"}
    newest = auditscript._newest_succeeded(runs) or runs[0]
    tenant_id, library_id = newest.tenant_id, newest.library_id

    store = ArtifactStore(settings.workspace, run_id)
    items = [EvalItem(**d) for d in
             json.loads((store.run_dir / "evalset.json").read_text("utf-8"))]
    scopes = _scopes(tenant_id, library_id, version)
    top_k = budget_for(LEVEL).top_k

    embedder = CachedEmbedder(
        _provider(), _embed_cache_dir(settings),
        model=settings.gemini.embedding_model,
        dimensions=settings.gemini.embedding_dimensions,
        workers=EMBED_WORKERS,
    )

    refusals: list[dict[str, Any]] = []
    gated = {"library": 0, "version": 0}
    with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
        questions = [it.question for it in items]
        vectors = dict(zip(questions, (e.values for e in embedder.embed_many(
            questions, task_type=RETRIEVAL_QUERY))))
        for it in items:
            vec = vectors[it.question]
            for name, filters in scopes.items():
                opts = SearchOpts(limit=top_k, min_score=MIN_SCORE,
                                  dense_only=True, query_text=it.question,
                                  filters=filters)
                if q.topicality_gate(vec, opts):
                    continue
                gated[name] += 1
                if name != "library":
                    continue
                # Refused where it counts. Look far past the floor for the
                # passage this question was written from: its score says
                # whether the floor merely clipped it, and its absence says a
                # rewritten query has nothing to find.
                deep_hits = q.search(vec, SearchOpts(
                    limit=deep, min_score=0.0, dense_only=True,
                    query_text=it.question, filters=filters))
                rank = next((i for i, h in enumerate(deep_hits, start=1)
                             if it.matches(h.payload)), 0)
                refusals.append({
                    "question": it.question,
                    "best": round(deep_hits[0].score, 4) if deep_hits else None,
                    "target_score": round(deep_hits[rank - 1].score, 4) if rank else None,
                    "target_rank": rank or None,
                })
    n = len(items)
    near = sum(1 for r in refusals
               if r["target_score"] and r["target_score"] >= MIN_SCORE - 0.05)
    return {
        "version_id": version,
        "run_id": run_id,
        "available": True,
        "library_id": library_id,
        "questions": n,
        "floor": MIN_SCORE,
        "level": LEVEL,
        "refused_library_scope": gated["library"],
        "refused_version_scope": gated["version"],
        "rate_library_scope": round(gated["library"] / n, 4) if n else 0.0,
        # The two facts that decide the feature, separated: a target the deep
        # probe never saw is out of a rewriter's reach whatever it writes.
        "target_beyond_deep_probe": sum(1 for r in refusals if not r["target_rank"]),
        "target_within_005_of_floor": near,
        "target_was_best": sum(1 for r in refusals if r["target_rank"] == 1),
        "deep": deep,
        "refusals": refusals,
        "embedding_tokens": embedder.usage.input_tokens,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("versions", nargs="+")
    ap.add_argument("--deep", type=int, default=DEEP)
    ap.add_argument("--json", dest="out")
    args = ap.parse_args(argv)

    settings = auditscript._settings()
    reports = [census_version(settings, v, deep=args.deep) for v in args.versions]
    live = [r for r in reports if r.get("available")]

    print(f"{'version':28} {'lib':>5} {'ver':>5} {'n':>4}  {'rate':>6}  beyond  near")
    for r in reports:
        if not r.get("available"):
            print(f"{r['version_id']:28} {'—':>5} {'—':>5}   —       —       —     —"
                  f"   ({r.get('detail', '')})")
            continue
        print(f"{r['version_id']:28} {r['refused_library_scope']:5} "
              f"{r['refused_version_scope']:5} {r['questions']:4}  "
              f"{r['rate_library_scope'] * 100:5.1f}%  "
              f"{r['target_beyond_deep_probe']:6}  {r['target_within_005_of_floor']:4}")
    if live:
        n = sum(r["questions"] for r in live)
        lib = sum(r["refused_library_scope"] for r in live)
        ver = sum(r["refused_version_scope"] for r in live)
        beyond = sum(r["target_beyond_deep_probe"] for r in live)
        best = sum(r["target_was_best"] for r in live)
        tokens = sum(r["embedding_tokens"] for r in live)
        print(f"\n{n} questions, every one answerable from the corpus by construction.")
        print(f"refused at library scope : {lib} ({lib / n * 100:.1f}%)")
        print(f"refused at version scope : {ver} ({ver / n * 100:.1f}%)")
        print(f"of the library-scope refusals, target beyond rank {args.deep}: "
              f"{beyond}/{lib}" if lib else "")
        print(f"of the library-scope refusals, target was the best chunk: {best}/{lib}"
              if lib else "")
        print(f"embedding tokens spent   : {tokens}"
              + ("  (all cached)" if not tokens else "  — real spend, the cache key moved"))
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(reports, indent=2, ensure_ascii=False), "utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
