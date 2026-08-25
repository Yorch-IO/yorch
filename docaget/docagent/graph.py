"""The LangGraph state machine.

    START
     └→ detect          extension + text-layer probe -> extractor, fingerprint
         └→ load_profile
             ├ hit  ────────────────────────────────────────→ extract
             └ miss → probe → propose → validate ─┬ pass ───→ extract
                                  ↑               └ fail → refine (<=3) ┘
         extract → classify_chunk → build_evalset → index → evaluate
              ├ meets target ──────────────→ persist → END
              └ below target → tune → classify_chunk (<=3 rounds) ┘

Two loops with very different budgets:

* **Rules** (propose/validate/refine): one Flash call, no embeddings. Cheap enough
  to iterate.
* **Parameters** (evaluate/tune): re-chunking and re-embedding, roughly $0.015 a
  round on a book. Bounded, and gated on a bootstrap noise margin.

The checkpointer means a long run — OCR of a scanned book takes minutes — can be
resumed rather than restarted.
"""

from __future__ import annotations

import time
from dataclasses import asdict, replace
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from . import correct as corr
from . import evaluate as ev
from . import profiles as prof
from . import rules as rl
from .chunk import Chunk, ChunkRules, DocRules, build_chunks, split_paragraphs
from .extract import Evidence, extract as run_extract, extractor_name
from .ledger import Ledger
from .qdrant import DENSE_VEC, Point, Qdrant, doc_id_for, point_id
from .vertex import EMBED_DIMS, TASK_DOCUMENT, Vertex

MAX_REFINE = 3
MAX_TUNE_ROUNDS = 3
RECALL_TARGET = 0.85
PAYLOAD_INDEXES = ("chapter", "section", "kind", "doc_id")

# Correction rewrites prose. Applying it to a spreadsheet's cells or a CSV's rows
# would corrupt data, not improve writing, so it is limited to prose extractors.
PROSE_EXTRACTORS = frozenset({"pdf_text", "pdf_ocr", "docx_", "plain"})


def _last(a: Any, b: Any) -> Any:
    """Reducer: later writes win. The default for scalars."""
    return b if b is not None else a


class IndexState(TypedDict, total=False):
    # --- inputs
    path: str
    collection: str
    qdrant_url: str
    workers: int
    eval_sample: int
    ocr_confirm: bool
    recreate: bool
    dry_run: bool
    no_correct: bool
    force_tune: bool

    # --- detection
    extractor: Annotated[str, _last]
    fingerprint: Annotated[str, _last]
    doc_id: Annotated[str, _last]
    profile: Annotated[Any, _last]  # prof.Profile
    profile_source: Annotated[str, _last]  # reused | learned | default

    # --- learning
    evidence: Annotated[Any, _last]  # Evidence
    proposal: Annotated[Any, _last]  # rl.Proposal
    validation: Annotated[Any, _last]  # rl.Validation
    refine_attempts: Annotated[int, _last]
    feedback: Annotated[list, _last]

    # --- content
    text: Annotated[Any, _last]  # bytes | None
    chunks: Annotated[list, _last]
    corrected_file: Annotated[str, _last]
    correction: Annotated[Any, _last]  # corr.CorrectionReport

    # --- evaluation
    evalset: Annotated[list, _last]
    scores: Annotated[Any, _last]  # prof.Scores
    baseline_run: Annotated[Any, _last]  # ev.EvalRun
    tune_round: Annotated[int, _last]
    tuning_history: Annotated[list, _last]
    pending_chunk_candidate: Annotated[str, _last]
    revert_reindex: Annotated[Any, _last]
    chunk_baseline: Annotated[Any, _last]
    chunk_rules_before: Annotated[Any, _last]
    chunk_margin: Annotated[Any, _last]
    leakage: Annotated[str, _last]

    # --- side channels
    log: Annotated[list, lambda a, b: (a or []) + (b or [])]


class Deps:
    """Handles the nodes need. Passed in rather than constructed per node so a
    run makes one HTTP client and one ledger, and tests can substitute fakes."""

    def __init__(
        self,
        vertex: Vertex | None = None,
        qdrant_factory=None,
        ledger: Ledger | None = None,
    ) -> None:
        self.ledger = ledger or Ledger()
        self._vertex = vertex
        self._qdrant_factory = qdrant_factory

    @property
    def vertex(self) -> Vertex:
        if self._vertex is None:
            self._vertex = Vertex(ledger=self.ledger)
        return self._vertex

    def qdrant(self, url: str, collection: str) -> Qdrant:
        if self._qdrant_factory is not None:
            return self._qdrant_factory(url, collection)
        return Qdrant(base_url=url, collection=collection)

    def close(self) -> None:
        if self._vertex is not None:
            self._vertex.close()


# --- nodes -------------------------------------------------------------------


def n_detect(state: IndexState, deps: Deps) -> dict:
    """Pick the extractor and take a cheap structural reading of the document.

    Extraction runs twice for text-like sources: once here with no rules, to
    gather the evidence rule learning needs, and once later with the learned
    rules applied. That is deliberate — the header patterns cannot be learned
    from text that has already had headers stripped.
    """
    path = state["path"]
    name = extractor_name(path)
    extracted = run_extract(path, DocRules(), name)
    fp = prof.fingerprint(extracted.evidence, name)
    return {
        "extractor": name,
        "evidence": extracted.evidence,
        "fingerprint": fp,
        "doc_id": doc_id_for(path),
        "log": [
            f"detect: {name}, {extracted.evidence.pages} page(s), fingerprint {fp}",
            *[f"  note: {n}" for n in extracted.evidence.notes],
        ],
    }


def n_load_profile(state: IndexState, deps: Deps) -> dict:
    if state.get("force_tune"):
        return {
            "profile_source": "default",
            "profile": None,
            "log": ["profile: --force-tune specified, will learn new rules"],
        }
    p = prof.load(state["fingerprint"])
    if p is None:
        return {
            "profile_source": "default",
            "profile": None,
            "log": ["profile: no match for this fingerprint — will learn rules"],
        }
    log = [
        f"profile: reusing {p.slug} (revision {p.revisions}, "
        f"learned from {p.learned_from}) — skipping rule learning",
    ]
    # The fingerprint identifies a *family*, so two different books from the same
    # course match each other — which is the point for the rules, and wrong for the
    # eval set. The second book of this corpus inherited the first's questions and
    # scored 0.000: every question asked about a passage that does not exist in it.
    # Rules travel between documents; questions and the scores they produced do not.
    # The slug is re-derived too. Keeping the inherited one made the reused profile
    # save over the file of the document that had learned it: the family's profile
    # ended up named after a church-history book while holding a hermeneutics
    # chapter's eval set and scores, and the measured numbers of two books were
    # lost. One file per document, found by fingerprint, newest wins (`prof.load`).
    if p.learned_from and p.learned_from != state["path"]:
        return {
            "profile_source": "reused",
            "profile": replace(
                p,
                evalset=[],
                scores=prof.Scores(),
                learned_from=state["path"],
                slug=prof.slug_for(state["path"], state["fingerprint"]),
            ),
            "log": log
            + [
                f"  eval set dropped: it was built from {p.learned_from}, not this "
                f"document — a fresh one will be generated and measured",
            ],
        }
    return {
        "profile_source": "reused",
        "profile": p,
        "log": log + [f"  previous scores: {p.scores.summary()}"],
    }


def n_propose(state: IndexState, deps: Deps) -> dict:
    proposal = rl.propose(deps.vertex, state["evidence"], state.get("feedback"))
    attempts = state.get("refine_attempts", 0) + 1
    return {
        "proposal": proposal,
        "refine_attempts": attempts,
        "log": [
            f"propose (attempt {attempts}): headers={list(proposal.header_patterns)} "
            f"l1_max={proposal.heading_l1_max} l2_max={proposal.heading_l2_max} "
            f"q={proposal.question_pattern!r} n={proposal.footnote_pattern!r}",
            f"  reasoning: {proposal.reasoning[:200]}",
        ],
    }


def n_validate(state: IndexState, deps: Deps) -> dict:
    """Apply the proposal to the whole document and demand independent evidence."""
    proposal = state["proposal"]
    evidence: Evidence = state["evidence"]

    # Validate against the document as extracted with NO rules applied: that is
    # the text the rules will be applied to.
    extracted = run_extract(state["path"], DocRules(), state["extractor"])
    text = extracted.text if extracted.text is not None else b""

    v = rl.validate(proposal, text, evidence)
    return {
        "validation": v,
        "feedback": v.feedback,
        "log": [f"validate: {'PASS' if v.passed else 'FAIL'}", *[f"  {n}" for n in v.notes()]],
    }


def n_adopt_rules(state: IndexState, deps: Deps) -> dict:
    """Turn a validated proposal into a profile, rule by rule.

    The adoption logic itself lives in `rules.adopt`, because the Temporal worker
    adopts profiles too and partial adoption is a measured rule — dropping only
    what failed, rather than discarding a whole proposal over one degenerate
    optional rule, is what keeps a cleanly-validated header pattern that a
    stricter version once threw away along with 175 running-header lines.
    """
    validation = state["validation"]
    fp = state["fingerprint"]
    p = rl.adopt(
        state["proposal"],
        validation,
        fingerprint=fp,
        slug=prof.slug_for(state["path"], fp),
        extractor=state["extractor"],
        learned_from=state["path"],
    )
    failed = validation.failed_rules()
    log = [f"adopt: learned profile {p.slug} · adopted {rl.adopted_rules(validation)}"]
    if failed:
        log.append(
            f"  dropped {sorted(failed)} — built-in defaults apply to those "
            "(they were measured, not guessed)"
        )
    return {"profile": p, "profile_source": "learned", "log": log}


def n_fallback_rules(state: IndexState, deps: Deps) -> dict:
    """Rule learning exhausted its attempts: fall back to the measured defaults.

    Not a failure. The built-in guards and the questions/footnotes discriminator
    were themselves arrived at by measurement on a real book, so falling back to
    them is a known-good configuration rather than a guess.
    """
    fp = state["fingerprint"]
    p = prof.Profile(
        fingerprint=fp,
        slug=prof.slug_for(state["path"], fp),
        extractor=state["extractor"],
        learned_from=state["path"],
        learned_at=time.time(),
        validation_notes=(state.get("validation").notes() if state.get("validation") else []),
    )
    return {
        "profile": p,
        "profile_source": "default",
        "log": [
            f"rule learning failed {MAX_REFINE} times — using the measured defaults "
            "(these are known-good, not a guess)"
        ],
    }


def n_extract(state: IndexState, deps: Deps) -> dict:
    p: prof.Profile = state["profile"]
    extracted = run_extract(state["path"], p.doc_rules, state["extractor"])

    if extracted.is_structured:
        chunks = extracted.chunks or []
        return {
            "text": None,
            "chunks": chunks,
            "log": [f"extract: {len(chunks)} structured chunks ({state['extractor']})"],
        }
    text = extracted.text or b""
    return {
        "text": text,
        "chunks": [],
        "log": [f"extract: {len(text):,} bytes of text ({state['extractor']})"],
    }


def n_correct(state: IndexState, deps: Deps) -> dict:
    """Fix orthography, accents and punctuation before chunking.

    Runs before chunking on purpose: correction changes the text's length, so
    doing it afterwards would invalidate every char_span. The corrected text is
    written to disk and becomes what char_span refers to — the PDF no longer
    does — which keeps invariant #1 auditable and leaves a record of what the
    model changed.

    Skipped for structured sources: "correcting" a spreadsheet's cells would
    corrupt data rather than improve writing.
    """
    if state.get("text") is None or state["extractor"] not in PROSE_EXTRACTORS:
        return {"log": [f"correct: skipped ({state['extractor']} is not prose)"]}
    if state.get("no_correct"):
        return {"log": ["correct: skipped (--no-correct)"]}
    if state.get("dry_run"):
        return {"log": ["correct: skipped (dry run)"]}

    text: bytes = state["text"]
    paras = split_paragraphs(text)
    def show(n: int, total: int, detail: str) -> None:
        print(f"    correcting batch {n}/{total}: {detail}", flush=True)

    fixed, report = corr.correct_paragraphs(
        deps.vertex, [p.text for p in paras], progress=show
    )

    corrected = "\n\n".join(fixed).encode("utf-8")
    path = corr.write_corrected(state["path"], corrected)

    log = [f"correct: {report.summary()}", f"  wrote {path}"]
    if report.rejected:
        # Rejections are the interesting output: they say where the model tried
        # to change something it had no business changing.
        by_reason: dict[str, int] = {}
        for r in report.rejected:
            by_reason[r.reason] = by_reason.get(r.reason, 0) + 1
        log.append(f"  rejected by reason: {by_reason}")
        for r in report.rejected[:3]:
            log.append(f"    paragraph {r.index} [{r.reason}]: {r.detail}")

    return {
        "text": corrected,
        "corrected_file": str(path),
        "correction": report,
        "log": log,
    }


def n_chunk(state: IndexState, deps: Deps) -> dict:
    """Chunk text-like documents. Structured ones arrive pre-chunked."""
    # Consumed here: whatever brought us back, the collection is about to be
    # rebuilt from the current profile.
    if state.get("text") is None:
        chunks = state["chunks"]
        return {"log": [f"chunk: {len(chunks)} pre-built chunks kept as-is"]}

    p: prof.Profile = state["profile"]
    text: bytes = state["text"]
    paras = split_paragraphs(text)
    # The learned kind patterns go *into* the chunker rather than over its
    # output: a change of kind is a chunk boundary, so relabelling finished
    # chunks can only rename ones the default rules already cut. A family whose
    # questions are marked "P1" instead of "1." had them merged into the
    # preceding prose and then relabelled by that prose's first paragraph.
    chunks = build_chunks(text, paras, p.chunk_rules, rl.classifier_for(p))

    counts: dict[str, int] = {}
    for c in chunks:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    bad = sum(1 for c in chunks if c.kind == "preguntas" and c.section)
    return {
        "chunks": chunks,
        "revert_reindex": False,
        "log": [
            f"chunk: {len(paras)} paragraphs -> {len(chunks)} chunks, kinds {counts}",
            f"  question chunks carrying a section path: {bad} (must be 0)",
        ],
    }


def n_build_evalset(state: IndexState, deps: Deps) -> dict:
    """Build the eval set once, then never again for this run.

    Every tuning round loops back through this node, so it must reuse what is
    already in **state** and not only what is in the profile — the profile's copy is
    written at persist, i.e. after tuning has finished. Checking the profile alone
    regenerated the questions on every round: observed sets of 88, 90, 88, 86 and 88
    questions in one run, which made the baselines drift (0.773 -> 0.782 -> 0.789)
    for no reason other than a changing eval, and invalidated every comparison the
    loop made. Comparing tuning rounds on different questions measures the
    questions, not the change.
    """
    if existing := state.get("evalset"):
        return {
            "evalset": existing,
            "log": [f"evalset: reusing {len(existing)} questions from this run"],
        }

    p: prof.Profile = state["profile"]
    if p.evalset:
        return {
            "evalset": p.evalset,
            "log": [f"evalset: reusing {len(p.evalset)} questions from the profile"],
        }
    if state.get("dry_run"):
        return {"evalset": [], "log": ["evalset: skipped (dry run)"]}

    def show(n: int, total: int) -> None:
        # Every phase that takes minutes must say so. Without this the terminal
        # goes silent for the whole eval-set build, which is indistinguishable
        # from a hang — a stall detector fired on exactly that false positive.
        if n == 1 or n % 10 == 0 or n == total:
            print(f"    generating eval question {n}/{total}", flush=True)

    items = ev.build_evalset(
        deps.vertex,
        state["chunks"],
        state.get("eval_sample", ev.DEFAULT_SAMPLE),
        progress=show,
    )
    return {
        "evalset": items,
        "log": [f"evalset: generated {len(items)} synthetic questions"],
    }


def n_index(state: IndexState, deps: Deps) -> dict:
    """Embed and upsert. One API call per chunk; concurrency, not batching."""
    if state.get("dry_run"):
        return {"log": ["index: skipped (dry run)"]}

    chunks: list[Chunk] = state["chunks"]
    from .bm25 import avg_doc_len, doc_sparse_vector, tokenize

    q = deps.qdrant(state["qdrant_url"], state["collection"])
    try:
        q.wait_ready()
        if state.get("recreate"):
            q.drop()
        q.create(EMBED_DIMS, PAYLOAD_INDEXES)

        def show(done: int, total: int) -> None:
            if done == 1 or done % 50 == 0 or done == total:
                print(f"    embedding {done}/{total}", flush=True)

        results = deps.vertex.embed_many(
            [c.embed_text() for c in chunks],
            TASK_DOCUMENT,
            workers=state.get("workers", 6),
            on_done=show,
            stage="embed",
        )

        docs = [tokenize(c.text) for c in chunks]
        avgdl = avg_doc_len(docs)
        doc_id = state["doc_id"]
        points = [
            Point(
                id=point_id(doc_id, c.index),
                dense=r.values,
                sparse=doc_sparse_vector(docs[i], avgdl),
                payload={
                    "doc_id": doc_id,
                    "source_file": state["path"].rsplit("/", 1)[-1],
                    # char_span indexes THIS file, not the original: correction
                    # changed the byte offsets.
                    "corrected_file": state.get("corrected_file", ""),
                    "chunk_index": c.index,
                    "kind": c.kind,
                    "chapter": c.chapter,
                    "section": c.section,
                    "breadcrumb": c.breadcrumb(),
                    "text": c.text,
                    "context": c.context,
                    "para_idx": [c.para_from, c.para_to],
                    "char_span": [c.char_from, c.char_to],
                    "cell_ref": c.cell_ref,
                    "tokens": r.tokens,
                },
            )
            for i, (c, r) in enumerate(zip(chunks, results))
        ]
        q.upsert(points)
        info = q.info()
    finally:
        q.close()

    return {
        "log": [
            f"index: {len(points)} points into {state['collection']!r} "
            f"(status {info.status}, {info.points_count} total)"
        ]
    }


def n_evaluate(state: IndexState, deps: Deps) -> dict:
    """Measure hybrid and dense-only side by side, so the eval's own lexical
    leakage is visible rather than hidden."""
    if state.get("dry_run") or not state.get("evalset"):
        return {
            "scores": prof.Scores(chunks=len(state.get("chunks", []))),
            "log": ["evaluate: skipped (no eval set)"],
        }

    p: prof.Profile = state["profile"]
    q = deps.qdrant(state["qdrant_url"], state["collection"])
    try:
        # Embed each question once and share the vectors across configurations.
        vectors = {
            item.question: deps.vertex.embed(
                item.question, "RETRIEVAL_QUERY", stage="eval_query"
            ).values
            for item in state["evalset"]
        }
        common = dict(
            doc_id=state["doc_id"], query_vectors=vectors, per_section=p.retrieval.per_section
        )
        hybrid = ev.measure(
            deps.vertex, q, state["evalset"], min_score=p.retrieval.min_score,
            dense_only=False, **common
        )
        dense = ev.measure(
            deps.vertex, q, state["evalset"], min_score=p.retrieval.min_score,
            dense_only=True, **common
        )
        floor = ev.noise_floor(deps.vertex, q, state["doc_id"])
    finally:
        q.close()

    scores = ev.to_scores(hybrid, dense, floor, len(state["chunks"]))
    leak = ev.leakage_note(hybrid, dense)
    out: dict = {
        "scores": scores,
        "baseline_run": hybrid,
        "leakage": leak,
        "log": [
            f"evaluate: {scores.summary()}",
            f"  leakage: {leak}",
            f"  bootstrap noise margin on recall@5: ±{ev.noise_margin(hybrid):.3f}",
        ],
    }

    # Judge a chunking candidate that was just reindexed. Without this a candidate
    # would be adopted merely for having been tried.
    if label := state.get("pending_chunk_candidate"):
        before = state.get("chunk_baseline") or 0.0
        margin = state.get("chunk_margin")
        if margin is None:
            margin = ev.noise_margin(hybrid)
        now = ev.objective(hybrid)
        delta = now - before
        keep = delta > margin
        out["log"].append(
            f"  chunk candidate {label}: MRR@10 {before:.3f} -> {now:.3f} "
            f"({delta:+.3f}) vs margin ±{margin:.3f} — "
            f"{'KEEP' if keep else 'REVERT'}"
        )
        history = list(state.get("tuning_history", []))
        for h in reversed(history):
            if h["candidate"] == label and h.get("mrr_at_10") is None:
                h["mrr_at_10"] = round(now, 4)
                h["recall_at_5"] = round(hybrid.recall_at_5, 4)
                h["accepted"] = keep
                h["why"] = (
                    f"MRR@10 {delta:+.3f} beats the {margin:.3f} noise margin"
                    if keep
                    else f"MRR@10 {delta:+.3f} is within the {margin:.3f} noise margin"
                )
                break
        out["tuning_history"] = history
        out["pending_chunk_candidate"] = ""
        if not keep:
            out["profile"] = replace(p, chunk_rules=state["chunk_rules_before"])
            # Reverting the profile leaves the COLLECTION holding the candidate's
            # chunks, so the next round would measure a baseline that belongs to a
            # configuration already rejected. Observed: baselines drifting
            # 0.729 -> 0.762 -> 0.700 across three rounds that had each reverted.
            # Routing back through chunk re-indexes the reverted rules so the
            # comparison stays honest.
            out["revert_reindex"] = True

    return out


def n_tune(state: IndexState, deps: Deps) -> dict:
    """Try retrieval knobs on the current index; accept only real gains.

    Retrieval-only candidates are free — the index does not change — so they are
    tried exhaustively. Chunking candidates would require re-embedding and are
    left to a future round rather than spent here.
    """
    p: prof.Profile = state["profile"]
    baseline: ev.EvalRun = state["baseline_run"]
    margin = ev.noise_margin(baseline)
    rnd = state.get("tune_round", 0) + 1
    history = list(state.get("tuning_history", []))
    log = [
        f"tune (round {rnd}): baseline MRR@10={ev.objective(baseline):.3f} "
        f"(recall@5={baseline.recall_at_5:.3f}), margin ±{margin:.3f}"
    ]

    q = deps.qdrant(state["qdrant_url"], state["collection"])
    best_label, best_run, best_params = None, baseline, p.retrieval
    try:
        vectors = {
            item.question: deps.vertex.embed(
                item.question, "RETRIEVAL_QUERY", stage="eval_query"
            ).values
            for item in state["evalset"]
        }
        for cand in ev.RETRIEVAL_CANDIDATES:
            params = replace(
                p.retrieval,
                **{
                    k: v
                    for k, v in (
                        ("min_score", cand.min_score),
                        ("per_section", cand.per_section),
                        ("dense_only", cand.dense_only),
                    )
                    if v is not None
                },
            )
            run = ev.measure(
                deps.vertex, q, state["evalset"],
                min_score=params.min_score, per_section=params.per_section,
                dense_only=params.dense_only, doc_id=state["doc_id"], query_vectors=vectors,
            )
            better, why = ev.is_real_improvement(baseline, run, margin)
            log.append(f"  {cand.label}: MRR@10={ev.objective(run):.3f} — {why}")
            history.append(
                {"round": rnd, "candidate": cand.label,
                 "mrr_at_10": round(ev.objective(run), 4),
                 "recall_at_5": round(run.recall_at_5, 4), "accepted": better, "why": why}
            )
            if better and ev.objective(run) > ev.objective(best_run):
                best_label, best_run, best_params = cand.label, run, params
    finally:
        q.close()

    if best_label is None:
        # Retrieval knobs are free to try — the index does not change — so they are
        # exhausted first. Only when none of them wins is a chunking change worth
        # its cost: re-chunking means re-embedding every chunk, so exactly one
        # candidate is proposed per round and the graph loops back through chunk.
        cand = _next_chunk_candidate(p, history)
        if cand is None:
            log.append("  no candidate left to try — keeping current parameters")
            return {"tune_round": rnd, "tuning_history": history, "log": log}

        overrides = {
            k: v
            for k, v in (
                ("target_chars", cand.target_chars),
                ("overlap_chars", cand.overlap_chars),
            )
            if v is not None
        }
        log.append(
            f"  retrieval knobs exhausted; trying {cand.label} — this re-chunks and "
            "re-embeds, so it costs a full indexing pass"
        )
        history.append(
            {"round": rnd, "candidate": cand.label, "mrr_at_10": None,
             "recall_at_5": None, "accepted": None, "why": "pending reindex"}
        )
        return {
            "profile": replace(p, chunk_rules=replace(p.chunk_rules, **overrides)),
            "pending_chunk_candidate": cand.label,
            "chunk_baseline": ev.objective(baseline),
            "chunk_rules_before": p.chunk_rules,
            "chunk_margin": margin,
            "tune_round": rnd,
            "tuning_history": history,
            "log": log,
        }

    log.append(
        f"  adopting {best_label}: MRR@10 {ev.objective(baseline):.3f} -> "
        f"{ev.objective(best_run):.3f}"
    )
    return {
        "profile": replace(p, retrieval=best_params),
        "baseline_run": best_run,
        "tune_round": rnd,
        "tuning_history": history,
        "log": log,
    }


def n_persist(state: IndexState, deps: Deps) -> dict:
    p: prof.Profile = state["profile"]
    scores = state.get("scores") or prof.Scores()
    updated = replace(
        p,
        scores=scores,
        evalset=state.get("evalset") or p.evalset,
        tuning_history=state.get("tuning_history", []),
        revisions=p.revisions + 1,
        learned_at=time.time(),
    )
    path = updated.save()
    return {
        "profile": updated,
        "log": [
            f"persist: {path} (revision {updated.revisions})",
            f"  {scores.summary()}",
        ],
    }


def _next_chunk_candidate(p: prof.Profile, history: list[dict]) -> ev.Candidate | None:
    """The next chunking candidate not already tried, and not a no-op.

    A candidate whose value already matches the current profile would spend a full
    reindex to measure something known.
    """
    tried = {h["candidate"] for h in history}
    for cand in ev.CHUNK_CANDIDATES:
        if cand.label in tried:
            continue
        if cand.target_chars is not None and cand.target_chars == p.chunk_rules.target_chars:
            continue
        if cand.overlap_chars is not None and cand.overlap_chars == p.chunk_rules.overlap_chars:
            continue
        return cand
    return None


# --- conditional edges -------------------------------------------------------


def e_needs_learning(state: IndexState) -> Literal["propose", "extract"]:
    return "extract" if state.get("profile") is not None else "propose"


def e_validation(state: IndexState) -> Literal["adopt_rules", "propose", "fallback_rules"]:
    if state["validation"].passed:
        return "adopt_rules"
    if state.get("refine_attempts", 0) >= MAX_REFINE:
        return "fallback_rules"
    return "propose"


def e_needs_tuning(state: IndexState) -> Literal["chunk", "tune", "persist", "__end__"]:
    # A dry run measures nothing, so it has nothing to record. Routing it to persist
    # would write a profile with empty scores and bump `revisions`, i.e. a run that
    # "spends nothing" would still overwrite the memory of a run that paid for its
    # numbers.
    if state.get("dry_run"):
        return END
    # A reverted chunk candidate left the collection out of step with the profile.
    # Reindex before doing anything else, otherwise every later measurement is
    # against the wrong chunks.
    if state.get("revert_reindex"):
        return "chunk"
    scores = state.get("scores")
    if scores is None or not state.get("evalset"):
        return "persist"
    if state.get("tune_round", 0) >= MAX_TUNE_ROUNDS:
        return "persist"
    # force_tune keeps searching even when the target is already met. Without it
    # the loop is unreachable on any document that happens to score well, which
    # makes it untestable — the first attempt to verify the loop with a sabotaged
    # overlap never entered it, because recall@5 was 0.875 against a 0.85 target.
    if scores.recall_at_5 >= RECALL_TARGET and not state.get("force_tune"):
        return "persist"
    return "tune"


def e_after_tune(state: IndexState) -> Literal["chunk", "evaluate", "persist"]:
    """Where to go after tuning.

    A chunking candidate has to be re-chunked and re-embedded before it can be
    measured, so it loops back through ``chunk``. A retrieval change needs no
    reindex and goes straight to ``evaluate``.
    """
    if state.get("pending_chunk_candidate"):
        return "chunk"
    return "persist" if state.get("tune_round", 0) >= MAX_TUNE_ROUNDS else "evaluate"


# --- wiring ------------------------------------------------------------------


def build_graph(deps: Deps, checkpointer=None):
    def bind(fn):
        def node(state: IndexState) -> dict:
            return fn(state, deps)

        node.__name__ = fn.__name__
        return node

    g: StateGraph = StateGraph(IndexState)
    g.add_node("detect", bind(n_detect))
    g.add_node("load_profile", bind(n_load_profile))
    g.add_node("propose", bind(n_propose))
    g.add_node("validate", bind(n_validate))
    g.add_node("adopt_rules", bind(n_adopt_rules))
    g.add_node("fallback_rules", bind(n_fallback_rules))
    g.add_node("extract", bind(n_extract))
    g.add_node("correct", bind(n_correct))
    g.add_node("chunk", bind(n_chunk))
    g.add_node("build_evalset", bind(n_build_evalset))
    g.add_node("index", bind(n_index))
    g.add_node("evaluate", bind(n_evaluate))
    g.add_node("tune", bind(n_tune))
    g.add_node("persist", bind(n_persist))

    g.add_edge(START, "detect")
    g.add_edge("detect", "load_profile")
    g.add_conditional_edges("load_profile", e_needs_learning)
    g.add_edge("propose", "validate")
    g.add_conditional_edges("validate", e_validation)
    g.add_edge("adopt_rules", "extract")
    g.add_edge("fallback_rules", "extract")
    g.add_edge("extract", "correct")
    g.add_edge("correct", "chunk")
    g.add_edge("chunk", "build_evalset")
    g.add_edge("build_evalset", "index")
    g.add_edge("index", "evaluate")
    g.add_conditional_edges("evaluate", e_needs_tuning, ["chunk", "tune", "persist", END])
    g.add_conditional_edges("tune", e_after_tune, ["chunk", "evaluate", "persist"])
    g.add_edge("persist", END)

    return g.compile(checkpointer=checkpointer)
