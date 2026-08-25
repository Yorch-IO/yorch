"""Rebuilding a version's projections from artifacts it already paid for.

The verb this implements is not "index again". Re-running the ingest pipeline
with correction switched off would produce *different chunks* — correction runs
before chunking precisely because it changes the text's length, which moves every
`char_span` — so the cheap-looking shortcut silently replaces the index rather
than restoring it. Reproducing what was paid for means replaying the artifacts:
`chunks.jsonl` for the vectors and the structure, `semantics.json` for the
concepts and claims.

Only one stage here can spend, and it is embedding. Everything else is a file
read and a graph write.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from temporalio import activity

from ..artifacts import ArtifactRef, ArtifactStore
from ..catalog import Catalog
from ..graph import Graph
from ..graph import projection as proj
from ..pipeline import (
    IngestRequest,
    RebuildInputs,
    Registered,
    Semantics,
    Spend,
    Staged,
)
from .ingest import _settings

log = logging.getLogger(__name__)

#: Which artifact a rebuild is built from. Named once so the availability probe
#: the UI calls and the loader below cannot drift apart.
REQUIRED_ARTIFACT = "chunks"
OPTIONAL_ARTIFACT = "semantics"


class RebuildUnavailable(RuntimeError):
    """No run of this version left the artifacts a rebuild replays."""

    kind = "rebuild_unavailable"


def _ref(row: dict, kind: str) -> ArtifactRef:
    return ArtifactRef(
        kind=kind, path=row["rel_path"], sha256=row["sha256"], bytes=row["size_bytes"]
    )


def _artifact_of(catalog: Catalog, run_id: str, name: str) -> dict | None:
    for row in catalog.artifacts(run_id):
        if row["name"] == name:
            return row
    return None


@activity.defn(name="load_rebuild_inputs")
async def load_rebuild_inputs(
    library_id: str, document_id: str, run_id: str, workflow_id: str
) -> RebuildInputs:
    """Reassemble a rebuild's inputs from the catalog and one previous run.

    Everything the pipeline needs beyond the chunk rows is already recorded:
    the library, the source key, the author, the title, the format and the
    content digest. Nothing is read from the source file, which is what lets a
    rebuild work for a document whose file has since moved.
    """
    settings = _settings()
    with Catalog(settings.database_url) as catalog:
        document = catalog.document(document_id, library_id=library_id)
        if document is None:
            raise RebuildUnavailable(
                f"no existe {document_id!r} en la biblioteca {library_id!r}"
            )
        version_id = catalog.active_version(document_id)
        if version_id is None:
            versions = catalog.versions_of(document_id)
            if not versions:
                raise RebuildUnavailable(
                    f"{document.title!r} no tiene ninguna versión que reconstruir"
                )
            version_id = versions[0].id
        version = next(
            (v for v in catalog.versions_of(document_id) if v.id == version_id), None
        )

        source_run_id = catalog.latest_run_with_artifact(
            version_id, REQUIRED_ARTIFACT
        )
        if source_run_id is None:
            raise RebuildUnavailable(
                f"ninguna ejecución de {document.title!r} conserva su "
                f"{REQUIRED_ARTIFACT!r}: no hay nada que reproducir"
            )
        chunks_row = _artifact_of(catalog, source_run_id, REQUIRED_ARTIFACT)
        semantics_row = _artifact_of(catalog, source_run_id, OPTIONAL_ARTIFACT)

        # Recorded as its own kind. A rebuild spends only on embedding, and
        # filing it under 'reindex' would make the two indistinguishable in the
        # cost history — the one place a user can see where the money went.
        catalog.start_run(
            run_id=run_id,
            workflow_id=workflow_id,
            kind="rebuild",
            document_id=document_id,
            version_id=version_id,
        )

    chunks = _ref(chunks_row, REQUIRED_ARTIFACT)
    store = ArtifactStore(settings.workspace, source_run_id)
    rows = list(store.iter_jsonl(chunks))
    characters = sum(len(r.get("text") or "") for r in rows)
    # `run_artifact` records a path, a digest and a size, but no row count, so
    # the reference rebuilt from it has `rows=None` — and the gate then offered
    # the user "0 fragmentos" for a document with twelve. Counted here because
    # the file has just been read anyway.
    chunks = replace(chunks, rows=len(rows))

    return RebuildInputs(
        request=IngestRequest(
            library_id=library_id,
            # The path is carried for completeness and is deliberately not read:
            # a rebuild must work for a document whose file has moved away.
            source_path=document.source_path or "",
            source_key=document.source_key,
            title=document.title,
            author=document.author,
            reindex=True,
        ),
        staged=Staged(
            content_sha256=version.content_sha256 if version else "",
            byte_size=version.byte_size if version else 0,
            fmt=document.format,
            extractor="",
            title=document.title,
        ),
        registered=Registered(
            document_id=document_id,
            version_id=version_id,
            created=False,
            already_indexed=True,
        ),
        chunks=chunks,
        characters=characters,
        semantics=_ref(semantics_row, OPTIONAL_ARTIFACT) if semantics_row else None,
        source_run_id=source_run_id,
    )


@activity.defn(name="replay_semantics")
async def replay_semantics(
    run_id: str, registered: Registered, semantics: ArtifactRef
) -> Semantics:
    """Project a previous run's concepts, claims and edges again, for free.

    The edges carry their original `extractor_model` and `created_at` because
    those are provenance: a relation proposed by one model in August is still
    that relation, and rewriting the timestamp to now would claim a re-extraction
    that never happened.
    """
    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    payload = store.read_json(semantics)

    edges = [proj.SemanticEdge(**e) for e in payload.get("edges", [])]
    with Graph(settings.memgraph_url) as graph:
        graph.ensure_schema()
        proj.project_concepts(graph, payload.get("concepts", []))
        proj.project_claims(graph, payload.get("claims", []))
        written = proj.project_semantic_edges(graph, edges)

    replayed = payload.get("claims", [])
    return Semantics(
        concepts=len(payload.get("concepts", [])),
        claims=len(replayed),
        # Counted from the artifact rather than assumed: one written before
        # quotes existed replays every claim without one, and saying so is the
        # whole point of the field.
        claims_verified=sum(1 for c in replayed if c.get("quote")),
        edges=written,
        # Free: this is a file read and a graph write. Reporting a zero-token
        # spend rather than none keeps the stage visible in the run's ledger.
        spend=Spend(stage="semantics-replay", model="", usd=0.0),
    )
