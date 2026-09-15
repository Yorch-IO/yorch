#!/usr/bin/env python3
"""Audit one indexed version against everything that produced it. Read-only.

    cd worker
    # The three stores, addressed as the app publishes them. `infra/.env` names
    # the ports and the Postgres password; it does not name a `BRAIN_DATABASE_URL`
    # because the containers build theirs from compose, so on the host it has to
    # be assembled. Getting the port wrong is the interesting mistake: 5432 is a
    # different Postgres, and this stack publishes 5532.
    export BRAIN_WORKSPACE_DIR=~/.local/share/io.sek.companybrain/workspace
    export BRAIN_QDRANT_URL=http://127.0.0.1:6433
    export BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7789
    export BRAIN_DATABASE_URL="postgresql://brain:$BRAIN_PG_PASSWORD@127.0.0.1:5532/brain"

    uv run python scripts/audit_version.py ver_0cde0e3196d06e4259a32a52
    uv run python scripts/audit_version.py ver_… --measure --json report.json

A leg whose store is down reports `available: false` and names the URL it tried;
the others still answer. That is the point of the split, not a nicety — "the
graph is stopped" and "this version has no concepts" are the two readings a
single failure would collapse into one.

Six legs, each with its own `available` flag:

1. **artifacts** — do the files still back the `run_artifact` rows, at the
   recorded sha256, under the workspace this process is pointed at?
2. **structure** — do `chunks.jsonl`, the byte stream, the Qdrant points and the
   graph nodes all describe the same document?
3. **semantics** — what did earlier runs leave behind in the graph that this
   version's own `semantics.json` does not name?
4. **trail** — every run against this version: stages, durations, what each one
   billed, and what was paid for more than once.
5. **retrieval** — `--measure` only: re-run the recorded measurement against the
   index as it stands today. **Unreachable for a video**, and that is not a gap
   here: `_recommended` switches `generate_evalset` off for a video, so there is
   no eval set to measure against and the leg says so before it constructs an
   embedder. A video has no recall figure and cannot be given one without paying
   for a stage its workflow does not have.
6. **video** — `run.kind == 'video'` only: which stream the offsets actually
   index, the cue table against the chunks, and what correction was allowed to
   change. The fact a reader wants back from a video is a time, and no other leg
   checks one.

Why it lives under `worker/` and imports rather than reimplements: a point id, a
payload scope and a claim id are the things being checked, so an audit that
derives them with its own arithmetic can only confirm its own arithmetic. Every
identity here comes from `brainworker.indexing`, `brainworker.graph.schema` and
`docagent.qdrant`, and the comparisons come from `brainworker.auditversion`,
which is pure and has tests.

**Nothing here writes.** The catalog is opened unpooled, the Cypher literals go
through `auditversion.assert_read_only`, and the only paid call in the file is
the query embedding behind `--measure`, which is opt-in and priced in the help
text: 80-odd query vectors, cents at most, but metered per minute.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import pathlib
import sys
from typing import Any, Iterator

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from brainworker import auditversion as av  # noqa: E402
from brainworker import auditlog, config, stages  # noqa: E402
from brainworker.artifacts import ArtifactStore  # noqa: E402
from brainworker.catalog import Catalog  # noqa: E402
from brainworker.graph.client import Graph  # noqa: E402
from brainworker.graph.projection import (  # noqa: E402
    _CANDIDATE_CONCEPTS,
    _COUNT_VERSION_SUBGRAPH,
)
from brainworker.graph.schema import chunk_id  # noqa: E402
from brainworker.indexing import version_scope  # noqa: E402

# Not a decoration: `projection.py` runs these two through the write path, and
# reusing them means the audit walks exactly the routes removal walks. If a
# fourth route to a concept is ever added there, this guard is what makes the
# omission here loud instead of silent.
av.assert_read_only(_CANDIDATE_CONCEPTS)
av.assert_read_only(_COUNT_VERSION_SUBGRAPH)

#: Taken from the vocabulary map rather than retyped, so a stage added there —
#: `semantics-replay` was — cannot go unnoticed here.
SEMANTICS_STAGES = tuple(
    stages.COST_STAGES["semantics"] + stages.COST_STAGES["replaying semantics"]
)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _settings() -> config.Settings:
    """Settings without `configure()`'s chdir or its `mkdir`.

    `configure()` creates the workspace layout, and an audit that creates a
    directory has already changed the thing it is measuring — that is how a
    missing bind-mount source became an empty directory once. Reading is enough.
    """
    return config.load()


@contextlib.contextmanager
def _catalog(url: str) -> Iterator[Catalog]:
    # Unpooled, for the reason bookkeeping writes are: a pool retries a refused
    # connection in the background, so a catalog that is merely down turns each
    # read into a full-timeout stall rather than one immediate error.
    cat = Catalog(url, pooled=False)
    try:
        yield cat
    finally:
        with contextlib.suppress(Exception):
            cat.close()


def _sha256(path: pathlib.Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _rows(graph: Graph, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one audited literal. `Graph.write` is the raw-Cypher route this
    codebase already uses for server-owned reads (`projection.py` reads its
    removal candidates through it); the safety is the literal and the guard, not
    the session's access mode, which Memgraph does not enforce anyway."""
    return [r.data for r in graph.write(av.assert_read_only(cypher), params)]


# ---------------------------------------------------------------------------
# Leg 1 — artifacts
# ---------------------------------------------------------------------------


def audit_artifacts(cat: Catalog, workspace: pathlib.Path, run_ids: list[str]) -> dict:
    per_run = []
    for run_id in run_ids:
        rows = cat.artifacts(run_id)
        checked = []
        for row in rows:
            path = (workspace / row["rel_path"]).resolve()
            if not path.is_file():
                # The workspace is named, not just the relative path: one catalog
                # can hold rows written under two workspaces, and the interesting
                # failure is "it exists, under the other one".
                checked.append(
                    {
                        "name": row["name"],
                        "rel_path": row["rel_path"],
                        "state": "ausente",
                        "detail": f"not under {workspace}",
                    }
                )
                continue
            digest, size = _sha256(path)
            same = digest == row["sha256"]
            checked.append(
                {
                    "name": row["name"],
                    "rel_path": row["rel_path"],
                    "state": "íntegro" if same else "sha256 distinto",
                    "recorded_bytes": row["size_bytes"],
                    "actual_bytes": size,
                    **({} if same else {"recorded_sha256": row["sha256"][:12],
                                        "actual_sha256": digest[:12]}),
                }
            )
        per_run.append(
            {
                "run_id": run_id,
                "recorded": len(rows),
                "intact": sum(1 for c in checked if c["state"] == "íntegro"),
                "missing": sum(1 for c in checked if c["state"] == "ausente"),
                "changed": sum(1 for c in checked if c["state"] == "sha256 distinto"),
                "artifacts": checked,
            }
        )
    return av.leg("artifacts", {"workspace": str(workspace), "runs": per_run})


# ---------------------------------------------------------------------------
# Leg 2 — structure
# ---------------------------------------------------------------------------


def _load_chunks(store: ArtifactStore) -> list[dict[str, Any]]:
    path = store.run_dir / "chunks.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"no chunks.jsonl under {store.run_dir}")
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _streams(store: ArtifactStore) -> dict[str, bytes]:
    """Every extracted stream this run left on disk, by label."""
    out = {}
    for name, label in av.STREAMS:
        path = store.run_dir / name
        if path.is_file():
            out[label] = path.read_bytes()
    return out


def audit_structure(
    settings: config.Settings,
    *,
    run_id: str,
    tenant_id: str,
    version: str,
    library_id: str,
    document_id: str,
) -> dict:
    from docagent.qdrant import Qdrant, point_id

    store = ArtifactStore(settings.workspace, run_id)
    try:
        chunks = _load_chunks(store)
        streams = _streams(store)
    except (FileNotFoundError, ValueError) as e:
        return av.unavailable("structure", f"artifacts unreadable: {e}")
    if not streams:
        return av.unavailable(
            "structure",
            f"no extracted stream under {store.run_dir}; looked for "
            + ", ".join(name for name, _ in av.STREAMS),
        )

    picked = av.choose_stream(streams, chunks)
    raw = streams[picked["chosen"]]
    report: dict[str, Any] = {
        "source_run": run_id,
        "stream": picked["chosen"],
        "stream_verifies_completely": picked["unanimous"],
        "streams_considered": picked["streams"],
        "spans": picked["report"],
        "sequence": av.chunk_sequence(chunks, len(raw)),
    }

    scope = version_scope(tenant_id, version)
    report["scope"] = scope
    try:
        with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
            points = q.scroll(scope)
        report["qdrant"] = _audit_points(
            points, chunks, version, tenant_id, library_id, document_id, point_id
        )
    except Exception as e:  # noqa: BLE001 — any store failure degrades this half
        report["qdrant"] = av.unavailable(
            "qdrant", f"{settings.qdrant_url}/{settings.qdrant_collection}: {e}"
        )

    try:
        with Graph(settings.memgraph_url) as g:
            report["graph"] = _audit_graph(g, version, tenant_id, chunks)
    except Exception as e:  # noqa: BLE001
        report["graph"] = av.unavailable("graph", _graph_detail(settings, e))

    return av.leg("structure", report)


def _graph_detail(settings: config.Settings, error: Exception) -> str:
    """The URL that was tried, and Memgraph's own `kind` when it has one.

    `graph_unreachable` and `graph_refused` are different problems — one is a
    stopped container, the other a query the database rejected — and both reach
    here as the same exception type.
    """
    kind = getattr(error, "kind", None)
    return f"{settings.memgraph_url}: {f'[{kind}] ' if kind else ''}{error}"


def _audit_points(
    points: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    version: str,
    tenant_id: str,
    library_id: str,
    document_id: str,
    point_id,
) -> dict[str, Any]:
    by_index = {int(c["index"]): c for c in chunks}
    expected_ids = {point_id(version, i): i for i in by_index}

    wrong_id, wrong_payload, drifted = [], [], []
    beyond = []
    seen: set[int] = set()
    for p in points:
        pl, pid = p["payload"], str(p["id"])
        index = pl.get("chunk_index")
        seen.add(index)
        if pid != point_id(version, index):
            wrong_id.append({"chunk_index": index, "point_id": pid})
        # A point with no tenant is paid for and unreachable by either plane,
        # and nothing errors when it is written. Checked field by field rather
        # than as a dict, so the report names which one is wrong.
        for key, want in (
            ("tenant_id", tenant_id),
            ("library_id", library_id),
            ("document_id", document_id),
            ("version_id", version),
            ("chunk_id", chunk_id(version, index) if index is not None else None),
        ):
            if pl.get(key) != want:
                wrong_payload.append(
                    {"chunk_index": index, "field": key,
                     "found": pl.get(key), "want": want}
                )
        chunk = by_index.get(index)
        if chunk is None:
            # The tail `prune_tail` exists to remove: a point at an index this
            # chunking does not produce, carrying a span into a stream nothing
            # holds.
            beyond.append({"chunk_index": index, "point_id": pid})
            continue
        if list(pl.get("char_span") or []) != [chunk["char_from"], chunk["char_to"]]:
            drifted.append(
                {"chunk_index": index, "point": pl.get("char_span"),
                 "chunk": [chunk["char_from"], chunk["char_to"]]}
            )
        elif pl.get("text") != chunk["text"]:
            drifted.append({"chunk_index": index, "why": "text differs from chunk"})

    kinds: dict[str, int] = {}
    chapters: dict[str, int] = {}
    for p in points:
        kinds[p["payload"].get("kind")] = kinds.get(p["payload"].get("kind"), 0) + 1
        ch = p["payload"].get("chapter") or ""
        if ch:
            chapters[ch] = chapters.get(ch, 0) + 1

    return {
        "available": True,
        "points": len(points),
        "chunks": len(chunks),
        "counts_agree": len(points) == len(chunks),
        "ids_unexpected": wrong_id[:20],
        "payload_wrong": wrong_payload[:20],
        "payload_wrong_total": len(wrong_payload),
        "spans_drifted": drifted[:20],
        "stale_tail": beyond[:20],
        "stale_tail_total": len(beyond),
        "indices_absent": sorted(set(expected_ids.values()) - seen)[:20],
        "kinds": dict(sorted(kinds.items())),
        "chapters": len(chapters),
    }


def _audit_graph(
    g: Graph, version: str, tenant_id: str, chunks: list[dict[str, Any]]
) -> dict[str, Any]:
    node = _rows(g, av.VERSION_NODE, {"version_id": version})
    if not node or not node[0].get("id"):
        return av.unavailable("graph", f"{version} is not in the projection")

    counts = _rows(g, _COUNT_VERSION_SUBGRAPH, {"version_id": version})[0]
    scope = {"version_id": version, "tenant_id": tenant_id}
    graph_chunks = _rows(g, av.CHUNK_NODES, scope)
    sections = _rows(g, av.SECTION_NODES, {"version_id": version})
    citations = _rows(g, av.CITATION_NODES, {"version_id": version})

    expected = {chunk_id(version, int(c["index"])): c for c in chunks}
    present = {c["id"]: c for c in graph_chunks}

    wrong_tenant = [
        {"label": label, "id": r["id"], "tenant_id": r.get("tenant_id")}
        for label, rows in (
            ("DocumentVersion", node),
            ("Chunk", graph_chunks),
            ("Section", sections),
            ("Citation", citations),
        )
        for r in rows
        if r.get("tenant_id") != tenant_id
    ]

    span_drift = [
        {"chunk_id": cid,
         "graph": [present[cid].get("char_start"), present[cid].get("char_end")],
         "chunk": [row["char_from"], row["char_to"]]}
        for cid, row in expected.items()
        if cid in present
        and [present[cid].get("char_start"), present[cid].get("char_end")]
        != [row["char_from"], row["char_to"]]
    ]

    # `_locator` builds "<title> · <breadcrumb> · [a:b]", so the title is the
    # part before the first separator. More than one distinct value means a
    # re-projection under a changed title left the old citations beside the new.
    # `None` for a timed source, which carries no title in its locator at all —
    # see `auditversion.title_prefixes`.
    titles = av.title_prefixes([c.get("locator") or "" for c in citations])
    return {
        "available": True,
        "version": {k: node[0].get(k) for k in ("id", "tenant_id", "title", "documents")},
        "counts": {k: int(counts.get(k) or 0) for k in
                   ("chunks", "sections", "citations", "claims")},
        "chunks_expected": len(expected),
        "chunks_absent": sorted(set(expected) - set(present))[:20],
        "chunks_unexpected": sorted(set(present) - set(expected))[:20],
        "span_drift": span_drift[:20],
        "sections": len(sections),
        "citations": len(citations),
        # The locator embeds the version's title, which can outlive the document
        # that supplied it. More than one distinct prefix means a re-projection
        # under a changed title left the old citations behind. `null` means the
        # question does not apply: a timed locator holds a clock, not a title.
        "citation_title_prefixes": None if titles is None else titles[:10],
        "wrong_tenant": wrong_tenant[:20],
        "wrong_tenant_total": len(wrong_tenant),
    }


# ---------------------------------------------------------------------------
# Leg 3 — semantics
# ---------------------------------------------------------------------------


def audit_semantics(
    settings: config.Settings, *, run_id: str, tenant_id: str, version: str
) -> dict:
    store = ArtifactStore(settings.workspace, run_id)
    path = store.run_dir / "semantics.json"
    if not path.is_file():
        return av.unavailable(
            "semantics",
            f"no semantics.json under {store.run_dir} — this version's semantics "
            "were never extracted, or were extracted by a run whose artifact is gone",
        )
    doc = json.loads(path.read_text(encoding="utf-8"))

    try:
        with Graph(settings.memgraph_url) as g:
            scope = {"version_id": version, "tenant_id": tenant_id}
            claims = _rows(g, av.CLAIM_NODES, scope)
            concepts = _rows(g, av.CONCEPTS_REACHED, scope)
            mentions = _rows(g, av.MENTION_EDGES, scope)
            chunk_rows = _rows(g, av.CHUNK_NODES, scope)
            orphans = _rows(
                g, av.ORPHAN_CLAIMS, {"chunk_ids": [c["id"] for c in chunk_rows]}
            )
            candidates = _rows(g, _CANDIDATE_CONCEPTS, {"version_id": version})
    except Exception as e:  # noqa: BLE001
        return av.unavailable("semantics", _graph_detail(settings, e))

    # The three set differences, keyed on the *pre-images* of the derived ids
    # rather than on the ids. `auditversion.semantic_diff` records why; the short
    # version is that the paid plane has to make the same comparison, and keying
    # on `claim_id`/`concept_id` would fork the tenant-salted id contract into a
    # second language. Measured on this version's own artifact: 3 055 claims,
    # 3 055 distinct pre-image keys, 3 055 distinct `claim_id`s — the partition
    # is identical, and 2 634 concept names fold to 2 517 ids either way.
    diff = av.semantic_diff(doc, claims=claims, concepts=concepts, mentions=mentions)
    stale = av.stale_claims(doc, claims)

    # A stale claim's quote was verified — against a chunk that has since been
    # re-cut. Checking it against the text the chunk holds *now* is what makes
    # the difference visible, and it is the one check `claims_verified` cannot
    # make from inside its own run.
    text_by_chunk = {c["id"]: c for c in chunk_rows}
    unlocatable = 0
    quoted = 0
    for c in stale:
        chunk = text_by_chunk.get(c.get("source_chunk_id"))
        verdict = av.quote_still_locates(c.get("quote"), _chunk_text(chunk))
        if verdict is None:
            continue
        quoted += 1
        if not verdict:
            unlocatable += 1

    return av.leg(
        "semantics",
        {
            "source_run": run_id,
            "extractor_model": doc.get("extractor_model"),
            **diff,
            "stale_claim_quotes": {
                "with_a_quote": quoted,
                "quote_no_longer_locates": unlocatable,
                "note": "checked only for claims the graph holds and this run "
                "did not produce",
            },
            "orphan_claims": {
                "count": len(orphans),
                "note": "source_chunk_id names one of this version's chunks but "
                "no DERIVED_FROM edge reaches it — invisible to every read "
                "surface, which is why they accumulate unnoticed",
                "sample": [o["id"] for o in orphans[:10]],
            },
            "concepts_removal_would_collect": len(
                set(candidates[0]["ids"]) if candidates else set()
            ),
            "wrong_tenant_concepts": [
                {"id": c["id"], "tenant_id": c.get("tenant_id")}
                for c in concepts
                if c.get("tenant_id") != tenant_id
            ][:20],
        },
    )


def _chunk_text(chunk: dict[str, Any] | None) -> str | None:
    """The chunk's text as the graph holds it *now*, or None for "cannot say".

    `None` propagates through `quote_still_locates` as "cannot say" rather than
    as a failure, which is the distinction the whole audit is built on: a claim
    whose chunk is gone is a different finding from one whose quote no longer
    occurs in it."""
    return None if chunk is None else chunk.get("text")


# ---------------------------------------------------------------------------
# Leg 4 — the trail and the bill
# ---------------------------------------------------------------------------


def audit_trail(cat: Catalog, *, tenant_id: str, runs: list[Any]) -> dict:
    per_run, charged = [], []
    for run in runs:
        events = cat.run_events(run.id, tenant_id=tenant_id)
        costs = cat.costs(run.id, tenant_id=tenant_id)
        artifacts = cat.artifacts(run.id)
        warnings = cat.open_profile_warnings(run.version_id) if run.version_id else []
        # The ledger is `auditlog.build`, not a second assembly: per-stage
        # durations, the stage vocabulary that joins a charge to the stage that
        # made it, and the trailing `stage: null` group that keeps the totals
        # equal to the bill are all decisions with tests behind them, and a
        # reimplementation here would be a second opinion about the same rows.
        summary = cat.run(run.id, tenant_id=tenant_id)
        ledger = (
            auditlog.build(summary, events, costs, artifacts, warnings)
            if summary is not None
            else None
        )

        charged.append(
            {
                "run_id": run.id,
                "state": run.state,
                "costs": [
                    {"stage": c.stage, "usd": None if c.usd is None else float(c.usd)}
                    for c in costs
                ],
            }
        )

        per_run.append(
            {
                "run_id": run.id,
                "kind": run.kind,
                "state": run.state,
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                # A run with no rows is not a run that did nothing: `run_event`
                # is newer than some of the runs in this catalog. "Unavailable"
                # and "empty" are different answers and must not render alike,
                # and the ledger cannot make the distinction — it renders both as
                # an empty `stages` list.
                "trail": (
                    {"available": False,
                     "detail": "no run_event rows — this run predates the audit trail"}
                    if not events
                    else {"available": True, "transitions": len(events)}
                ),
                "ledger": ledger,
                "profile_warnings": [
                    {
                        **w,
                        # `_topical_overlap` compares against the profile's
                        # `learned_from` *filename stem* using evidence only
                        # `pdf_text` fills in, so for a plain-text document it
                        # returns 0.0 by construction — and 0.0 is documented as
                        # the most dangerous case. Reported with a flag rather
                        # than repeated as a measurement.
                        "comparable": bool(w.get("similarity")),
                    }
                    for w in warnings
                ],
            }
        )

    # Shared with `/libraries/{id}/versions/{id}/statistics`, not copied: the
    # rule that a version's bill is not one run's bill is the finding, and two
    # implementations of it would be two answers about the same rows.
    ledger = av.cost_by_stage(charged)
    by_stage = ledger["by_stage"]
    return av.leg(
        "trail",
        {
            "runs": per_run,
            "cost_by_stage": by_stage,
            "total_usd": ledger["total_usd"],
            "charged_in_more_than_one_run": ledger["charged_in_more_than_one_run"],
            "usd_by_run_state": ledger["usd_by_run_state"],
            "semantics_charged_in": sorted(
                {r for s in SEMANTICS_STAGES for r in by_stage.get(s, {}).get("runs", [])}
            ),
        },
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
# Leg 5 — retrieval
# ---------------------------------------------------------------------------


def audit_retrieval(
    settings: config.Settings,
    *,
    run_id: str,
    tenant_id: str,
    version: str,
    measure: bool,
) -> dict:
    from docagent.profiles import EvalItem, RetrievalParams

    store = ArtifactStore(settings.workspace, run_id)
    evalset_path = store.run_dir / "evalset.json"
    scores_path = store.run_dir / "scores.json"
    if not evalset_path.is_file():
        return av.unavailable("retrieval", f"no evalset.json under {store.run_dir}")

    items = json.loads(evalset_path.read_text(encoding="utf-8"))
    recorded = (
        json.loads(scores_path.read_text(encoding="utf-8"))
        if scores_path.is_file()
        else None
    )
    scan = av.scan_questions(items)

    report: dict[str, Any] = {
        "source_run": run_id,
        "questions": scan,
        "recorded": recorded and {
            "scores": recorded.get("scores"),
            "margin": recorded.get("margin"),
            "retrieval": recorded.get("retrieval"),
            "scope": recorded.get("scope"),
            "leakage": recorded.get("leakage"),
        },
    }
    if recorded:
        report["misses_explained"] = av.misses_explained(
            scan["suspects"], recorded.get("misses") or []
        )
        params = recorded.get("retrieval") or {}
        floor = (recorded.get("scores") or {}).get("noise_floor")
        if params.get("min_score") is not None and floor is not None:
            report["floor"] = av.floor_verdict(params["min_score"], floor)
        # The scope a figure was measured under is part of the figure. A scores
        # artifact written before that was recorded gets said so, not assumed.
        report["scope_recorded"] = bool(recorded.get("scope"))

    # What the product actually serves. `answering/retrieve.py` reads module
    # constants, never the profile's `retrieval` block, so an agreement between
    # the two is a coincidence and has to be reported as one or the block looks
    # wired up when it is not.
    report["served_with"] = _served_params()

    if not measure:
        report["measured"] = None
        report["note"] = "pass --measure to re-run this against the live index"
        return av.leg("retrieval", report)

    from docagent.qdrant import Qdrant
    from docagent.runner import evaluate as run_evaluate
    from brainworker.activities.paid import (
        EMBED_WORKERS,
        CachedEmbedder,
        _embed_cache_dir,
        _provider,
    )

    scope = version_scope(tenant_id, version)
    params = RetrievalParams(**(recorded.get("retrieval") or {})) if recorded else RetrievalParams()
    embedder = CachedEmbedder(
        _provider(),
        _embed_cache_dir(settings),
        model=settings.gemini.embedding_model,
        dimensions=settings.gemini.embedding_dimensions,
        workers=EMBED_WORKERS,
    )

    def run(subset: list[dict[str, Any]]) -> dict[str, Any]:
        with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
            outcome = run_evaluate(
                embedder,
                q,
                [EvalItem(**d) for d in subset],
                scope=scope,
                params=params,
                chunks=(recorded or {}).get("scores", {}).get("chunks", 0),
            )
        return {
            "scores": dataclasses.asdict(outcome.scores),
            "margin": round(outcome.margin, 4),
            "leakage": outcome.leakage,
        }

    try:
        as_stored = run(items)
    except Exception as e:  # noqa: BLE001
        return av.leg("retrieval", {**report, "measured": None,
                                    "measure_failed": str(e)})

    report["scope_used"] = scope
    report["measured"] = as_stored
    if recorded:
        report["comparison"] = av.compare_scores(
            as_stored["scores"], recorded.get("scores") or {},
            float(recorded.get("margin") or 0.0),
        )
    if scan["suspect"]:
        keep = {s["position"] for s in scan["suspects"]}
        report["measured_without_suspect_questions"] = run(
            [d for i, d in enumerate(items) if i not in keep]
        )
    # Zero means every query vector came back from `docagent.embedcache` — the
    # `evaluating` stage embedded these same 80 questions under this same model,
    # and the cache is keyed on (model, width, task, text). A re-measurement of a
    # run that already measured is therefore usually free, which is not something
    # to assume: it is reported, because a non-zero figure here is real spend.
    report["spent"] = {
        "embedding_input_tokens": embedder.usage.input_tokens,
        "note": "0 means every query vector was already in the embedding cache",
    }
    return av.leg("retrieval", report)


def _served_params() -> dict[str, Any]:
    from brainworker.answering import retrieve

    return {
        "min_score": retrieve.MIN_SCORE,
        "per_section": retrieve.PER_SECTION,
        "note": "module constants; retrieve.search does not read the profile's "
        "retrieval block, so agreement with it is coincidence",
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
# Leg 6 — video
# ---------------------------------------------------------------------------


def audit_video(settings: config.Settings, *, run_id: str) -> dict:
    """What a timed source has that a document does not, and what that costs.

    Only for `run.kind == 'video'`. Everything here reads the run's own
    artifacts and re-derives through the functions the pipeline used, because
    the fact a reader wants back from a video — *where in the video was this
    said* — is not checkable from the index alone: the Qdrant payload carries
    no `start_s`, so a wrong timestamp is an unverifiable citation that looks
    verifiable, and the only person who finds out is the one who clicks it.

    The load-bearing entry is `stream`. `chunk_transcript` falls back to the
    *uncorrected* transcript when correction moves the paragraph count, and
    until `Chunked` carried its warnings that fact reached the worker's stderr
    and nowhere else. Scoring both streams is what makes "the correction was
    paid for and not indexed" answerable after the log is gone — which, on the
    first real video import, it already was.
    """
    store = ArtifactStore(settings.workspace, run_id)
    d = store.run_dir

    def load(name: str):
        path = d / name
        return path if path.is_file() else None

    if not (chunks_path := load("chunks.jsonl")):
        return av.unavailable("video", f"no chunks.jsonl under {d}")
    if not (cues_path := load("transcript.json")):
        return av.unavailable("video", f"no transcript.json under {d}")

    from brainworker import videosource

    chunks = [json.loads(line) for line in
              chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    cues = json.loads(cues_path.read_text(encoding="utf-8"))
    table = [videosource.ParagraphTime(r["para"], r["start_s"], r["end_s"])
             for r in cues.get("paragraphs", [])]
    probe = json.loads(p.read_text(encoding="utf-8")) if (
        p := load("video-probe.json")) else {}

    report: dict[str, Any] = {
        "source": cues.get("source"),
        "video_id": probe.get("video_id") or cues.get("video_id"),
        "cues": cues.get("cues"),
        "timed_paragraphs": len(table),
        "chosen_track": probe.get("chosen"),
    }

    # Identity, re-derived rather than trusted. On the caption path the basis's
    # last line is the digest of bytes that were free to fetch, so this is a
    # true content hash and not the proxy the Transcribe path leaves.
    if (basis := probe.get("identity_basis")) and probe.get("content_sha256"):
        report["identity"] = {
            "content_sha256_rederives": hashlib.sha256(
                basis.encode("utf-8")
            ).hexdigest() == probe["content_sha256"],
            "captions_digest_matches_basis": (
                load("captions.vtt") is not None
                and _sha256(d / "captions.vtt")[0] == basis.split("\n")[-1]
            ),
        }

    streams = {
        label: path.read_bytes()
        for label, path in (("corrected", load("corrected.txt")),
                            ("transcript", load("transcript.txt")))
        if path is not None
    }
    if streams:
        picked = av.choose_stream(streams, chunks)
        report["stream"] = {
            "chunked": picked["chosen"],
            "verifies_completely": picked.get("unanimous"),
            "scored": picked["streams"],
            # The whole reason this leg exists. `corrected` means the paid
            # correction is what a reader retrieves; `transcript` means the
            # fallback fired and the correction was bought and discarded.
            "correction_fallback_fired": picked["chosen"] == "transcript",
        }
        if (data := streams.get(picked["chosen"])) is not None:
            from docagent.chunk import split_paragraphs

            try:
                videosource.assert_aligned(
                    [para.text for para in split_paragraphs(data)], table
                )
                report["stream"]["aligned"] = True
            except Exception as e:  # noqa: BLE001
                report["stream"]["aligned"] = f"{type(e).__name__}: {e}"

    report["paragraph_ranges"] = av.para_ranges_unique(chunks)
    report["times"] = av.time_report(chunks, table, probe.get("duration_s"))
    report["uncovered_paragraphs"] = videosource.uncovered_paragraphs(
        [_ParaRange(c.get("para_from"), c.get("para_to")) for c in chunks], table
    )
    report["kinds"] = {
        k: sum(1 for c in chunks if c.get("kind") == k)
        for k in sorted({c.get("kind") for c in chunks})
    }
    if preview := load("chunks.preview.jsonl"):
        report["preview_vs_final"] = {
            "preview": sum(1 for line in
                           preview.read_text(encoding="utf-8").splitlines()
                           if line.strip()),
            "final": len(chunks),
        }
    if rep := load("correction-report.json"):
        data = json.loads(rep.read_text(encoding="utf-8"))
        rejected = data.get("rejected") or []
        by_reason: dict[str, int] = {}
        for row in rejected:
            by_reason[str(row.get("reason"))] = by_reason.get(str(row.get("reason")), 0) + 1
        report["correction"] = {
            k: data.get(k) for k in
            ("paragraphs", "changed", "unchanged", "missing", "cache_hits", "calls")
        }
        # Measured on the first real video: 22 of 107 paragraphs rejected, every
        # one for a proper noun the *captioner* got wrong. The rule was measured
        # on books, where a capitalised word is a real name; on speech a machine
        # transcribed it keeps the machine's mistake.
        report["correction"]["rejected"] = len(rejected)
        report["correction"]["rejected_by_reason"] = by_reason
        report["correction"]["rejected_indices"] = [r.get("index") for r in rejected][:30]

    return av.leg("video", report)


@dataclasses.dataclass(frozen=True)
class _ParaRange:
    """The two fields `uncovered_paragraphs` reads, so a jsonl row can be passed
    to the same function the activity passes real chunks to."""

    para_from: int
    para_to: int


# ---------------------------------------------------------------------------


def audit(version: str, *, measure: bool = False) -> dict[str, Any]:
    settings = _settings()
    report: dict[str, Any] = {
        "version_id": version,
        "workspace": str(settings.workspace),
        "qdrant": f"{settings.qdrant_url}/{settings.qdrant_collection}",
        "memgraph": settings.memgraph_url,
    }

    try:
        with _catalog(settings.database_url) as cat:
            runs = _runs_for(cat, version)
            if not runs:
                report["legs"] = [
                    av.unavailable(
                        "catalog",
                        f"no run in the catalog names {version} — either the id is "
                        "wrong or its runs were written under another catalog",
                    )
                ]
                return report
            newest = _newest_succeeded(runs) or runs[0]
            tenant_id = newest.tenant_id
            document_id = newest.document_id
            library_id = newest.library_id
            report["tenant_id"] = tenant_id
            report["library_id"] = library_id
            report["document_id"] = document_id
            report["runs"] = [r.id for r in runs]
            report["audited_run"] = newest.id

            legs = [
                audit_artifacts(cat, settings.workspace, [r.id for r in runs]),
                audit_trail(cat, tenant_id=tenant_id, runs=runs),
            ]
    except Exception as e:  # noqa: BLE001
        return {**report, "legs": [av.unavailable(
            "catalog", f"{_redact(settings.database_url)}: {e}")]}

    legs.insert(
        1,
        audit_structure(
            settings,
            run_id=newest.id,
            tenant_id=tenant_id,
            version=version,
            library_id=library_id,
            document_id=document_id,
        ),
    )
    legs.insert(
        2,
        audit_semantics(
            settings, run_id=_run_with(runs, "semantics.json", settings) or newest.id,
            tenant_id=tenant_id, version=version,
        ),
    )
    if newest.kind == "video":
        # Gated on the kind rather than on a file being present: "this run has
        # no transcript" and "this is not a video" are different findings, and
        # a leg that appeared for a document would be the second wearing the
        # first's clothes.
        legs.append(audit_video(settings, run_id=newest.id))
    legs.append(
        audit_retrieval(
            settings,
            run_id=_run_with(runs, "scores.json", settings) or newest.id,
            tenant_id=tenant_id,
            version=version,
            measure=measure,
        )
    )
    report["legs"] = legs
    return report


def _runs_for(cat: Catalog, version: str) -> list[Any]:
    """Every run against this version, across every organisation.

    `Catalog.runs` requires a tenant — rightly, it is a listing — and an audit
    starts from a version id and does not know one yet. So the tenant is resolved
    from the runs themselves, oldest first, which is the one query here that has
    to go around the listing predicate. An id is not authorization, and this is a
    local read-only tool run by whoever holds the database URL; a route must
    never do this.
    """
    with cat._conn() as conn, conn.cursor() as cur:  # noqa: SLF001
        cur.execute(
            # `run` carries no library — a library is a property of the
            # document, and the run points at one. Joined rather than looked up
            # separately so a run whose document was deleted still lists, with a
            # null library, instead of dropping out of the audit entirely.
            "SELECT r.id, r.kind, r.state, r.tenant_id, d.library_id, "
            "r.document_id, r.version_id, r.started_at, r.finished_at "
            "FROM run r LEFT JOIN document d ON d.id = r.document_id "
            "WHERE r.version_id = %s ORDER BY r.started_at",
            (version,),
        )
        rows = cur.fetchall()
    return [_Run(*r) for r in rows]


@dataclasses.dataclass(frozen=True)
class _Run:
    id: str
    kind: str
    state: str
    tenant_id: str
    library_id: str
    document_id: str
    version_id: str
    started_at: Any
    finished_at: Any


def _newest_succeeded(runs: list[_Run]) -> _Run | None:
    done = [r for r in runs if r.state == "succeeded"]
    return done[-1] if done else None


def _run_with(runs: list[_Run], filename: str, settings: config.Settings) -> str | None:
    """The newest run whose artifact directory actually holds this file.

    The catalog row is not the check: `run_artifact` can name a file that was
    pruned, or one written under the other workspace. `can_rebuild` learned this
    the same way.
    """
    for run in reversed(runs):
        if (settings.workspace / "runs" / run.id / filename).is_file():
            return run.id
    return None


def _redact(url: str) -> str:
    return url.split("@")[-1] if "@" in url else url


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(report: dict[str, Any]) -> str:
    lines = [
        f"version   {report['version_id']}",
        f"tenant    {report.get('tenant_id', '—')}   library {report.get('library_id', '—')}",
        f"runs      {', '.join(report.get('runs', [])) or '—'}",
        f"workspace {report['workspace']}",
        "",
    ]
    for leg in report.get("legs", []):
        name = leg["leg"]
        if not leg["available"]:
            lines += [f"[{name}] no disponible — {leg['detail']}", ""]
            continue
        lines.append(f"[{name}]")
        lines.append(
            "\n".join(
                "  " + line
                for line in json.dumps(
                    {k: v for k, v in leg.items() if k not in ("leg", "available")},
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ).splitlines()
            )
        )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("version_id", help="the ver_… to audit")
    parser.add_argument(
        "--measure",
        action="store_true",
        help="re-run the recorded retrieval measurement against the live index. "
        "SPENDS: one query embedding per eval question plus four noise queries "
        "(cents at most, but metered per minute)",
    )
    parser.add_argument("--json", dest="json_path", help="also write the full report here")
    args = parser.parse_args(argv)

    report = audit(args.version_id, measure=args.measure)
    print(render(report))
    if args.json_path:
        pathlib.Path(args.json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"wrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
