"""Command line entry point.

    docagent index  <file> [...]     learn rules if needed, chunk, embed, evaluate
    docagent query  "pregunta"       hybrid retrieval over the corpus
    docagent eval                    re-measure a document already indexed
    docagent profiles                what the agent has learned so far
    docagent diag                    structural diagnostics, no ground truth needed
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from . import evaluate as ev
from . import profiles as prof
from .graph import Deps, RECALL_TARGET, build_graph
from .ledger import Ledger
from .qdrant import Qdrant, SearchOpts, diversify, doc_id_for
from .vertex import TASK_QUERY, Vertex

DEFAULT_COLLECTION = "docagent"
DEFAULT_QDRANT = "http://localhost:6333"
STATE_DIR = pathlib.Path("state")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docagent", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--collection", default=DEFAULT_COLLECTION)
        p.add_argument("--qdrant", default=DEFAULT_QDRANT)

    p_index = sub.add_parser("index", help="index one or more documents")
    p_index.add_argument("paths", nargs="+")
    common(p_index)
    p_index.add_argument("--workers", type=int, default=6)
    p_index.add_argument("--eval-sample", type=int, default=ev.DEFAULT_SAMPLE)
    p_index.add_argument("--recreate", action="store_true", help="drop the collection first")
    p_index.add_argument(
        "--dry-run",
        action="store_true",
        help="extract, learn rules and chunk, but spend nothing on embeddings",
    )
    p_index.add_argument(
        "--no-correct",
        action="store_true",
        help="skip the orthographic correction pass (it is on by default for prose)",
    )
    p_index.add_argument("--ocr-confirm", action="store_true", help="authorise paid OCR")
    p_index.add_argument(
        "--force-tune",
        action="store_true",
        help="keep tuning even if the recall target is already met",
    )
    p_index.add_argument("--no-checkpoint", action="store_true")
    p_index.add_argument(
        "--resume",
        default="",
        help="resume an interrupted run by its thread id (see the run header)",
    )

    p_query = sub.add_parser("query", help="search the corpus")
    p_query.add_argument("question")
    common(p_query)
    p_query.add_argument("-k", type=int, default=5)
    p_query.add_argument("--min-score", type=float, default=0.60)
    p_query.add_argument("--per-section", type=int, default=2)
    p_query.add_argument("--kind", default="")
    p_query.add_argument("--doc", default="", help="restrict to one doc_id")
    p_query.add_argument("--dense-only", action="store_true")

    p_prof = sub.add_parser("profiles", help="list learned profiles")
    p_prof.add_argument("--verbose", action="store_true")

    p_diag = sub.add_parser("diag", help="structural diagnostics")
    common(p_diag)
    p_diag.add_argument("--doc", default="")
    p_diag.add_argument(
        "--suite", default="", help="diagnostic suite JSON (default: <doc>.diag.json)"
    )
    p_diag.add_argument("--dense-only", action="store_true")

    args = parser.parse_args(argv)
    return {
        "index": cmd_index,
        "query": cmd_query,
        "profiles": cmd_profiles,
        "diag": cmd_diag,
    }[args.cmd](args)


# --- index -------------------------------------------------------------------


def cmd_index(args) -> int:
    run_id = time.strftime("%Y%m%dT%H%M%S")
    ledger = Ledger()
    deps = Deps(ledger=ledger)
    checkpointer, ctx = _checkpointer(args.no_checkpoint)

    try:
        graph = build_graph(deps, checkpointer=checkpointer)
        failures = 0
        for path in args.paths:
            print(f"\n{'=' * 72}\n{path}\n{'=' * 72}")
            state = {
                "path": path,
                "collection": args.collection,
                "qdrant_url": args.qdrant,
                "workers": args.workers,
                "eval_sample": args.eval_sample,
                "recreate": args.recreate,
                "dry_run": args.dry_run,
                "ocr_confirm": args.ocr_confirm,
                "no_correct": args.no_correct,
                "force_tune": args.force_tune,
            }
            # A fresh thread per invocation. Keying the thread on the file alone
            # makes every re-run continue the previous thread instead of starting
            # over, which silently replays old state on top of new input — the
            # graph then appears to run twice. Resuming is opt-in via --resume.
            config = {}
            if checkpointer:
                thread = args.resume or f"{pathlib.Path(path).stem}:{run_id}"
                config = {"configurable": {"thread_id": thread}}
                print(f"  thread: {thread}")
            try:
                out = graph.invoke(state, config=config)  # type: ignore[arg-type]
            except Exception as e:  # one bad document must not sink the batch
                print(f"  FAILED: {type(e).__name__}: {e}")
                failures += 1
                continue

            for line in out.get("log", []):
                print(f"  {line}")
            scores = out.get("scores")
            if scores and scores.eval_questions:
                verdict = "meets" if scores.recall_at_5 >= RECALL_TARGET else "below"
                print(f"  → {verdict} the recall@5 target of {RECALL_TARGET:.2f}")

            # Only the first document of a batch should recreate the collection.
            args.recreate = False

        print(f"\n{ledger.summary()}")
        if not args.dry_run:
            # `run_id` and the paths travel with the numbers, because a history
            # of runs that cannot say *which document* a run was for is only
            # marginally better than the single run it replaced.
            ledger.dump("costo.json", run_id=run_id, documents=list(args.paths))
            print("wrote costo.json")
        return 1 if failures else 0
    finally:
        deps.close()
        if ctx is not None:
            ctx.__exit__(None, None, None)


def _checkpointer(disabled: bool):
    """SQLite checkpointer so a long run (OCR of a scanned book takes minutes) can
    be resumed instead of restarted."""
    if disabled:
        return None, None
    from langgraph.checkpoint.sqlite import SqliteSaver

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ctx = SqliteSaver.from_conn_string(str(STATE_DIR / "checkpoints.sqlite"))
    return ctx.__enter__(), ctx


# --- query -------------------------------------------------------------------


def cmd_query(args) -> int:
    filters: dict[str, str] = {}
    if args.kind:
        filters["kind"] = args.kind
    if args.doc:
        filters["doc_id"] = doc_id_for(args.doc)

    with Vertex() as v, Qdrant(args.qdrant, args.collection) as q:
        q.wait_ready()
        vec = v.embed(args.question, TASK_QUERY, stage="query").values
        opts = SearchOpts(
            limit=args.k * (5 if args.per_section > 0 else 1),
            min_score=args.min_score,
            dense_only=args.dense_only,
            query_text=args.question,
            filters=filters,
        )
        if not args.dense_only and not q.topicality_gate(vec, opts):
            print(
                f"nothing clears the {args.min_score:.2f} cosine floor — the query "
                "looks off-topic for this corpus (try --min-score 0)"
            )
            return 0
        hits = diversify(q.search(vec, opts), args.per_section, args.k)

    if not hits:
        print(f"no results above the {args.min_score:.2f} cosine floor")
        return 0

    print(f"query: {args.question}")
    for i, h in enumerate(hits, 1):
        p = h.payload
        anchor = p.get("cell_ref") or f"bytes {p.get('char_span')}"
        print(f"\n{i}. score {h.score:.4f} · [{p.get('kind')}] {p.get('breadcrumb')}")
        print(f"   {p.get('source_file')} · {anchor}")
        print(f"   {_collapse(str(p.get('text', '')))[:300]}")
    return 0


# --- profiles ----------------------------------------------------------------


def cmd_profiles(args) -> int:
    ps = prof.all_profiles()
    if not ps:
        print("no profiles learned yet")
        return 0
    print(f"{len(ps)} profile(s) in {prof.PROFILE_DIR}/\n")
    for p in ps:
        print(f"{p.slug}  [{p.fingerprint}]  rev {p.revisions}  via {p.extractor}")
        print(f"  learned from : {p.learned_from}")
        print(f"  headers      : {list(p.doc_rules.header_patterns) or '—'}")
        print(f"  heading caps : l1<={p.chunk_rules.heading_l1_max} l2<={p.chunk_rules.heading_l2_max}")
        print(f"  kind rules   : q={p.question_pattern!r} n={p.footnote_pattern!r}")
        print(
            f"  chunking     : target={p.chunk_rules.target_chars} "
            f"cap={p.chunk_rules.hard_cap_chars} overlap={p.chunk_rules.overlap_chars}"
        )
        print(
            f"  retrieval    : min_score={p.retrieval.min_score} "
            f"per_section={p.retrieval.per_section} dense_only={p.retrieval.dense_only}"
        )
        print(f"  scores       : {p.scores.summary()}")
        if args.verbose:
            for n in p.validation_notes:
                print(f"    {n}")
            for h in p.tuning_history:
                print(f"    tune r{h['round']} {h['candidate']}: {h['why']}")
        print()
    return 0


# --- diag --------------------------------------------------------------------

DEFAULT_DIAG_SUITE = {
    "on_topic": (
        "¿qué es la soberanía de las esferas?",
        "el cogito cartesiano",
        "crítica de Lyotard a los metarrelatos",
    ),
    "exact": ("Kierkegaard", "Gén. 2:15", "hilemorfismo", "Dooyeweerd"),
}

# A diagnostic suite is per-document data, not code: its queries only mean anything
# for one book. Keeping them in a dict keyed by filename put the corpus in the
# source tree and made the key a literal string that no longer matches the moment
# the file is renamed — silently falling back to questions about Dooyeweerd.
DIAG_SUITE_SUFFIX = ".diag.json"


def diag_suite(doc_path: str, override: str = "") -> tuple[dict, str]:
    """The suite for one document, and a one-line provenance note.

    Read from ``<document>.diag.json`` beside the document, or from ``--suite``.
    A missing sidecar is reported, not hidden: the default suite asks about a
    different corpus entirely, so silently using it produces a diagnostic that
    looks like a total retrieval failure.
    """
    path = pathlib.Path(override) if override else None
    if path is None and doc_path:
        path = pathlib.Path(doc_path).with_suffix(
            pathlib.Path(doc_path).suffix + DIAG_SUITE_SUFFIX
        )
    if path is None:
        return DEFAULT_DIAG_SUITE, "suite: built-in default (no --doc given)"
    if not path.exists():
        return DEFAULT_DIAG_SUITE, (
            f"suite: NO SIDECAR at {path} — falling back to the built-in default, "
            f"whose queries are about another corpus — low scores below mean nothing"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = {"on_topic", "exact"} - data.keys()
    if missing:
        raise SystemExit(f"{path}: diagnostic suite is missing {sorted(missing)}")
    return (
        {"on_topic": tuple(data["on_topic"]), "exact": tuple(data["exact"])},
        f"suite: {path.name} ({len(data['on_topic'])} on-topic, {len(data['exact'])} exact)",
    )


def cmd_diag(args) -> int:
    """Structural diagnostics: no ground truth needed, so this works on any
    collection and stays comparable across configurations.

    Scores are only comparable within one retrieval mode: RRF fusion returns
    reciprocal ranks, not cosines.
    """
    filters = {"doc_id": doc_id_for(args.doc)} if args.doc else {}
    mode = "dense only" if args.dense_only else "hybrid (dense + BM25/RRF)"

    suite, provenance = diag_suite(args.doc, args.suite)
    DIAG_ON_TOPIC = suite["on_topic"]
    DIAG_EXACT = suite["exact"]
    print(provenance)

    with Vertex() as v, Qdrant(args.qdrant, args.collection) as q:
        q.wait_ready()
        info = q.info()
        print(f"collection {args.collection!r} · {info.points_count} points · {mode}")
        if not args.dense_only:
            print("NOTE: scores below are RRF ranks, not cosines — compare only against")
            print("      another hybrid run.")

        print(f"\n{'kind':<10}{'top1':>8}{'topN':>8}{'secciones':>12}  consulta")
        print("-" * 90)
        for label, queries in (("EN TEMA", DIAG_ON_TOPIC), ("RUIDO", ev.NOISE_QUERIES)):
            for text in queries:
                vec = v.embed(text, TASK_QUERY, stage="diag").values
                opts = SearchOpts(
                    limit=10, min_score=0.60, dense_only=args.dense_only,
                    query_text=text, filters=filters,
                )
                if not args.dense_only and not q.topicality_gate(vec, opts):
                    print(f"{label:<10}{'—':>8}{'—':>8}{'0/0':>12}  {text}  (gated)")
                    continue
                hits = q.search(vec, opts)
                if not hits:
                    print(f"{label:<10}{'—':>8}{'—':>8}{'0/0':>12}  {text}  (below floor)")
                    continue
                secs = len({h.payload.get("breadcrumb") for h in hits})
                print(
                    f"{label:<10}{hits[0].score:>8.3f}{hits[-1].score:>8.3f}"
                    f"{f'{secs}/{len(hits)}':>12}  {text}"
                )

        print("\ntérminos exactos (¿aparece el término literal en el rank 1?)")
        ok = 0
        for term in DIAG_EXACT:
            vec = v.embed(term, TASK_QUERY, stage="diag").values
            opts = SearchOpts(
                limit=5, min_score=0.60, dense_only=args.dense_only,
                query_text=term, filters=filters,
            )
            hits = q.search(vec, opts)
            if not hits:
                print(f"  {term:<14} —      NO   (nothing above the floor)")
                continue
            literal = term.lower() in str(hits[0].payload.get("text", "")).lower()
            ok += literal
            print(
                f"  {term:<14} {hits[0].score:.3f}  {'sí' if literal else 'NO':<4} "
                f"{str(hits[0].payload.get('breadcrumb'))[:56]}"
            )
        print(f"  → literal at rank 1: {ok} of {len(DIAG_EXACT)}")

        payloads = q.scroll_all()
        bad = sum(
            1 for p in payloads if p.get("kind") == "preguntas" and p.get("section")
        )
        total_q = sum(1 for p in payloads if p.get("kind") == "preguntas")
        print(f"\nquestion chunks: {total_q} · carrying a section path: {bad} (must be 0)")
    return 0


def _collapse(s: str) -> str:
    return " ".join(s.split())


if __name__ == "__main__":
    sys.exit(main())
