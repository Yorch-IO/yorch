"""Editing a chunk after it was indexed, across three stores, in one order.

RAGFlow's loudest claim is *"visualization of text chunking to allow human
intervention"*, and this product's own record is the argument for it: a table of
contents indexed as chapters, `2. Ibídem.` promoted to a heading, 428 of 4,239
chunks carrying a citation as their breadcrumb. Every one of those is a chunk
somebody could have fixed in ten seconds and could not.

**What this costs, stated once and plainly.** An edited chunk's text is no
longer a byte-exact slice of any stream this product holds, so it has no
verifiable `char_span` — the property the whole engine is built around,
invariant #1, the thing `auditversion.choose_stream` exists to check. That was
given up deliberately, for what it buys: a wrong chunk can be made right. The
rules below are what keep it from costing anything else.

**Catalog first, then the projections** — the opposite of `removal.py`, and for
the same reason it gives. There the catalog is what makes a half-finished
removal retryable; here it is the catalog row that *is* the edit, and the vector
and the graph are derived from it. Write the override and crash, and the chunk
still reads as it did while the row says what somebody wanted: press again and
it finishes. Re-embed first and crash, and the index holds text nothing in the
catalog accounts for — the one state from which nobody can tell an edit from a
corruption.

**An override that no longer fits its chunk is never applied.** `chunk_index` is
not stable across a re-cut — one corrected profile took a document from 600
chunks to 631 — so reapplying by index alone would attach a person's correction
to a different passage, silently, and it would read exactly like a good edit.
`ChunkOverride.replaced_sha256` is what makes that impossible, and an orphan is
*reported* rather than dropped: an edit that vanished without a word is worse
than one that stopped being applied.

**It spends, so it opens a run row.** One embedding, about $0.000002 — and the
rule is not about size. `record_cost` derives its tenant from the run the charge
hangs off, so no run row means no bookkeeping of any kind, which is how the
ledger came to be missing every question ever asked. `run.kind = 'edit'` exists
for that, like `ask`, `chat` and `epub` before it.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from . import config
from .audit import audited, mint_run_id
from .catalog import Catalog
from .graph import Graph, GraphError
from .graph import projection as proj
from .graph.schema import chunk_id as make_chunk_id

log = logging.getLogger(__name__)


class EditRefused(RuntimeError):
    """The edit cannot be applied, and the reason is the caller's to act on."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class EditOutcome:
    """What applying one edit actually did, per store."""

    version_id: str
    chunk_index: int
    #: False when the edit only hid the chunk, so nothing was re-embedded.
    reindexed: bool
    #: Claims whose quote no longer appears in the new text and therefore lost
    #: their span. Not their existence — the claim is still a reading of a
    #: chunk a person can open.
    claims_checked: int = 0
    claims_unverified: int = 0
    #: What the embedding cost, or 0.0 when nothing was embedded. Recorded on
    #: the run row as well; carried here so the caller can show it.
    usd: float = 0.0


def edit_chunk(
    settings: config.Settings,
    *,
    tenant_id: str,
    version_id: str,
    chunk_index: int,
    text: str | None,
    disabled: bool,
    edited_by: str = "",
) -> EditOutcome:
    """Record an edit and make it real, in the one order that is safe.

    `text=None, disabled=False` is an undo: the override is deleted and the
    chunk goes back to exactly what the run produced, which is why the stored
    original is never overwritten anywhere.
    """
    original = _chunk_text(settings, tenant_id, version_id, chunk_index)
    replaced = hashlib.sha256(original.encode("utf-8")).hexdigest()

    # Minted here rather than left to `audited`, because the charge below has
    # to name the run it hangs off: `record_cost` derives its tenant from that
    # row, so a charge with no id is a charge that is silently not recorded —
    # which is exactly what the first real edit did, and what the run-kind
    # migration exists to prevent.
    run_id = mint_run_id("edit")
    with audited(
        settings, kind="edit", tenant_id=tenant_id, run_id=run_id,
        version_id=version_id,
    ) as step:
        step("recording")
        with Catalog(settings.database_url) as catalog:
            catalog.save_chunk_override(
                version_id, chunk_index,
                text=text, disabled=disabled,
                replaced_sha256=replaced, edited_by=edited_by,
            )

        # The text a reader will now be served: what they wrote, or — for an
        # undo, or for a chunk only hidden — what the run produced.
        serving = text if text is not None else original

        step("indexing")
        usd, tokens = _reindex_one(settings, tenant_id, version_id, chunk_index,
                                   serving, disabled)
        # The row is written even when the cache answered and the figure is
        # zero: a stage that ran for nothing and a stage that did not run are
        # different facts, the same distinction the cached query embedding
        # books a zero row for. Best-effort, like every other bookkeeping
        # write — an edit refused because the catalog blinked would be the
        # worse trade.
        try:
            with Catalog(settings.database_url, pooled=False) as catalog:
                catalog.record_cost(
                    run_id, stage="edit-embedding", provider="vertex",
                    model=settings.gemini.embedding_model,
                    input_tokens=tokens, output_tokens=0, usd=usd,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("edit %s: could not record the embedding charge: %s",
                        run_id, e)

        step("projecting")
        checked = unverified = 0
        try:
            with Graph(settings.memgraph_url) as graph:
                cid = make_chunk_id(version_id, chunk_index)
                graph.write(
                    "MATCH (c:Chunk {id: $id}) SET c.text = $text, c.edited = $edited",
                    {"id": cid, "text": serving, "edited": text is not None},
                )
                # The locator carries the marker, and `citation_id` is keyed on
                # the locator — so an edit mints a *different* citation rather
                # than updating one. `project_structure` prunes what it did not
                # produce; an edit is not a projection and has nothing to hang
                # that off, so the replacement is one statement in `recite_chunk`.
                #
                # Without it an edited chunk keeps a citation printing a byte
                # range into a stream its text is no longer a slice of — a
                # pointer at nothing, on the surface whose job is to be
                # checkable, and it reads perfectly.
                _recite(graph, cid, version_id, tenant_id, edited=text is not None)
                counts = proj.reverify_claims(graph, cid, serving)
            checked, unverified = counts["claims"], counts["unverified"]
        except GraphError as e:
            # The graph is a projection and can be rebuilt; refusing the edit
            # because Memgraph blinked would leave the catalog and the index
            # agreeing while the caller is told nothing happened.
            log.warning("edit %s/%s: graph not updated: %s",
                        version_id, chunk_index, e)

    return EditOutcome(
        version_id=version_id, chunk_index=chunk_index,
        reindexed=text is not None, claims_checked=checked,
        claims_unverified=unverified, usd=usd,
    )


def _recite(graph: Any, cid: str, version_id: str, tenant_id: str,
            *, edited: bool) -> None:
    """Rebuild this chunk's locator from what the graph already holds.

    Read back rather than reconstructed, so the parts that did not change —
    the title, the page, the section — are exactly the ones the projection
    wrote. Building them again here would be a second implementation of
    `_locator`, and the two would drift on the first format that grows a part.
    """
    rows = graph.write(
        "MATCH (c:Chunk {id: $id}) OPTIONAL MATCH (c)-[:CITES]->(cit:Citation) "
        "RETURN cit.locator AS locator, cit.page AS page, "
        "cit.section_title AS section_title",
        {"id": cid},
    )
    if not rows or not rows[0]["locator"]:
        return
    row = rows[0]
    parts = row["locator"].split(" · ")
    # The byte range is the last part for a document and absent for a timed
    # source, whose locator is a clock and a link and must not gain a marker:
    # a transcript's chunk has no span to lose.
    tail = parts[-1]
    if not (tail.startswith("[") or tail == "editado"):
        return
    parts[-1] = "editado" if edited else tail
    if not edited and tail == "editado":
        # An undo cannot recover the byte range from here, and inventing one
        # would be worse than leaving the marker: the text is back to the
        # run's, so the next projection restores the range honestly.
        return
    proj.recite_chunk(
        graph, cid, " · ".join(parts), version_id=version_id,
        tenant_id=tenant_id, page=row["page"], section_title=row["section_title"],
    )


def _chunk_text(
    settings: config.Settings, tenant_id: str, version_id: str, chunk_index: int
) -> str:
    """What the *run* produced for this chunk, never what is being served.

    Read from the graph rather than from `chunks.jsonl`, because a version's
    artifacts may have been pruned — 79 of 238 were, on this installation —
    while the projection is what every screen already reads. An edit is
    therefore possible for any indexed version, not only for one whose run
    directory survives.

    It is also the ownership predicate: the chunk id is salted with the tenant,
    so a member of another organisation naming this one finds nothing. An id is
    not authorization, and this is the read that makes it one.
    """
    cid = make_chunk_id(version_id, chunk_index)
    try:
        with Graph(settings.memgraph_url) as graph:
            rows = graph.write(
                "MATCH (c:Chunk {id: $id})<-[:HAS_CHUNK]-()"
                " RETURN c.text AS text, c.edited AS edited LIMIT 1",
                {"id": cid},
            ) or graph.write(
                "MATCH (c:Chunk {id: $id}) RETURN c.text AS text, c.edited AS edited",
                {"id": cid},
            )
    except GraphError as e:
        raise EditRefused(
            f"el grafo no responde, no se puede editar ahora: {e}",
            kind="graph_unreachable",
        ) from e
    if not rows:
        raise EditRefused(
            "no existe ese fragmento en esta versión", kind="chunk_not_found"
        )
    # A chunk already edited: the *original* is what the override was written
    # against, and it is still in the catalog's `replaced_sha256`. Re-editing
    # an edited chunk therefore keys on what is being served, which is what the
    # next `applies_to` will be asked about.
    return rows[0]["text"] or ""


def _reindex_one(
    settings: config.Settings,
    tenant_id: str,
    version_id: str,
    chunk_index: int,
    text: str,
    disabled: bool,
) -> tuple[float, int]:
    """Re-embed one chunk and overwrite its point, and say what it cost.

    Idempotent by construction.

    The point id is `point_id(version_id, chunk_index)`, so this overwrites
    rather than adding — the same property that makes `embed_and_index`
    retryable under Temporal, applied to a single row.

    The payload is written whole, which is why the flags go through the writer
    rather than a `set_payload`: a flag poked into a point is wiped by the next
    re-index, silently, and the catalog row is what survives.
    """
    from docagent.qdrant import Point, Qdrant, point_id

    from .activities.paid import _embed_cache_dir, _provider
    from .indexing import INTEGER_INDEXES, PAYLOAD_INDEXES, version_scope
    from .providers import CachedEmbedder
    from .providers.gemini import RETRIEVAL_DOCUMENT

    embedder = CachedEmbedder(
        _provider(), _embed_cache_dir(settings),
        model=settings.gemini.embedding_model,
        dimensions=settings.gemini.embedding_dimensions,
        workers=1,
    )
    with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
        # Ensures the `disabled` index as well, which is why it is called here
        # and not assumed: a filter over an unindexed field scans the whole
        # collection, and this is the first writer that can create one.
        q.create(settings.gemini.embedding_dimensions,
                 PAYLOAD_INDEXES, INTEGER_INDEXES)
        # Every point of this version, because the sparse vector needs the
        # version's own `avgdl` — see `_sparse_for`. The read is scoped, so it
        # is one document's worth and not the collection's.
        points = q.scroll(version_scope(tenant_id, version_id))
        pid = point_id(version_id, chunk_index)
        mine = next((p for p in points if p["id"] == pid), None)
        if mine is None:
            raise EditRefused(
                "ese fragmento no está indexado", kind="chunk_not_indexed"
            )
        payload = dict(mine.get("payload") or {})
        breadcrumb = payload.get("breadcrumb") or ""
        embed_text = f"{breadcrumb}\n\n{text}" if breadcrumb else text
        vector = embedder.embed_many([embed_text], task_type=RETRIEVAL_DOCUMENT)[0]

        payload["text"] = text
        payload["edited"] = True
        if disabled:
            payload["disabled"] = True
        else:
            payload.pop("disabled", None)

        q.upsert([
            Point(id=pid, dense=vector.values,
                  sparse=_sparse_for(text, points, pid), payload=payload)
        ])

    from .activities.ingest import price_for

    # **From the embedder, not from the returned vector.** A cache hit hands
    # back an `Embedding` carrying the token count the *original* call cost,
    # which is right for saying what a vector was worth and wrong to bill:
    # reading it would book money nobody spent on every repeated edit.
    tokens = embedder.usage.input_tokens
    return (price_for(settings.gemini.embedding_model, tokens, 0) or 0.0), tokens


def _sparse_for(text: str, points: list[dict], pid: str):
    """The BM25 vector for the edited text, on **this version's** length scale.

    `doc_sparse_vector` divides by `avgdl`, and `runner.index_chunks` computes
    that over the batch it was handed — which is one version. So a single chunk
    re-weighted against any other figure is stored on a different scale from
    its siblings: the recorded "the same chunk weighted differently depending
    on which book it was indexed with" defect, reproduced *inside* one book by
    the one write that touches a chunk on its own.

    So the version's other chunks are re-tokenised from their payloads and the
    mean is taken exactly as indexing takes it, with the edited chunk's own new
    length in place of its old one. Pure CPU, and a 600-chunk book is
    milliseconds.

    Rebuilding it at all is not optional: leaving the old vector would make the
    lexical leg match words the chunk no longer contains — findable by what it
    used to say, which is the failure substituting-before-embedding exists to
    avoid, reached through BM25 instead.
    """
    from docagent.bm25 import avg_doc_len, doc_sparse_vector, tokenize

    mine = tokenize(text)
    docs = [
        mine if p["id"] == pid else tokenize((p.get("payload") or {}).get("text") or "")
        for p in points
    ]
    return doc_sparse_vector(mine, avg_doc_len(docs) or float(len(mine) or 1))
