"""Yorch's half of the engine's indexing seam: identity, and the stale tail.

`docagent.runner.index_chunks` embeds chunks and hands back vectors. It does not
know what a tenant is, and it must not learn: `doc_id_for()` hashes a filename,
and a point written into the product's collection without a `tenant_id` is
unreachable by either plane — the embedding is paid for and nothing errors.

So the engine hands over `IndexRow`s and this module turns each into a point.
Everything identity-shaped lives here: the id, the payload, and the deletion of
whatever a previous, longer chunking left behind.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from . import scripture
from .graph.schema import chunk_id as make_chunk_id

log = logging.getLogger(__name__)

#: Payload fields Qdrant must index for filtering to be usable at all.
#:
#: `tenant_id` is first because it is the one filter every search carries: a
#: library is a shelf inside an organisation, and every other key here narrows
#: within one.
PAYLOAD_INDEXES = ("tenant_id", "library_id", "version_id", "kind", "document_id",
                   "source_name", "scripture_refs", "scripture_chapters",
                   # Written only on a chunk somebody hid, and read as a
                   # `must_not`. **Never as `enabled: true`**: a positive flag
                   # has to be present on every point to mean anything, so
                   # adopting one hides every point written before it — 8,050
                   # of them — unless a backfill runs and never misses. Phrased
                   # as the exception, absence means visible, which is what
                   # every existing point already says.
                   "disabled")
#: Payload fields indexed as integers rather than keywords: a `range` filter
#: over an unindexed field scans the collection, and `recorded_day` is what a
#: date range narrows on.
INTEGER_INDEXES = ("recorded_day",)


def version_scope(tenant_id: str, version_id: str) -> dict[str, str]:
    """The payload filter identifying one version's points, and nothing wider.

    One definition, used by the writer's tail prune and by the evaluation's
    filter. Two call sites that disagreed about this would score a document
    against another document's chunks and report the result as its recall.
    """
    return {"tenant_id": tenant_id, "version_id": version_id}


@dataclass(frozen=True)
class StoredChunk:
    """A chunk read back from `chunks.jsonl`, as `docagent.runner` wants it.

    Chunks cannot cross a Temporal payload — a book is far past the ~2 MB cap and
    workflow history is kept for the namespace's whole retention period — so
    `chunk_final` writes them and every later stage reads them back.

    **`embed_text` is the stored value, never recomputed.** It is what the
    preview showed and what the estimate was priced from; deriving it again here
    would make the two silently divergent the day `Chunk.embed_text` changes.
    """

    index: int
    kind: str
    chapter: str
    section: str
    text: str
    context: str
    char_from: int
    char_to: int
    cell_ref: str
    _embed_text: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "StoredChunk":
        return cls(
            index=row["index"],
            kind=row["kind"],
            chapter=row.get("chapter", ""),
            section=row.get("section", ""),
            text=row["text"],
            context=row.get("context", ""),
            char_from=row["char_from"],
            char_to=row["char_to"],
            cell_ref=row.get("cell_ref", ""),
            # Falls back to the chunk's own text for a row written before
            # `chunk_final` recorded one — a rebuild replays artifacts that may
            # predate any given field, and embedding the bare text is right
            # rather than merely safe.
            _embed_text=row.get("embed_text") or row["text"],
        )

    def embed_text(self) -> str:
        return self._embed_text

    def breadcrumb(self) -> str:
        """A method, not a property, because `docagent.chunk.Chunk` makes it one.

        `evaluate.build_evalset` calls `c.breadcrumb()` on whatever it is handed,
        so the two shapes have to agree or the eval set is built against a
        different type from the one that was indexed.
        """
        return " > ".join(p for p in (self.chapter, self.section) if p)


def pages_by_paragraph(rows: "Sequence[dict[str, Any]] | None") -> dict[int, int]:
    """A paragraph index to the page it is on, from the `positions` sidecar.

    A dict rather than a list because a stream with no positions is a real and
    ordinary case — a `.txt` has no pages — and a sparse lookup makes "this
    paragraph has no page" the same answer as "this document has none, so no
    chunk gets one", which is what must render as absence rather than as 0.
    """
    return {int(r["para"]): int(r["page"]) for r in (rows or []) if r.get("page")}


def chunk_row(chunk: Any, *, start_s: float | None = None,
              end_s: float | None = None,
              pages: "dict[int, int] | None" = None) -> dict[str, Any]:
    """One row of ``chunks.jsonl``, written by every stage that writes one.

    A function rather than a dict literal at each call site, and it lives here
    rather than beside a caller, because :meth:`StoredChunk.from_row` is the
    *reader* and the two must not drift. Two writers with their own literals is
    how a field ends up written by one and expected by the other.

    ``para_from``/``para_to`` are recorded even though only a timed source needs
    them: they are already on ``docagent.chunk.Chunk`` and were being discarded,
    and they are what makes a chunk's timestamp checkable after the fact against
    the run's own cue table rather than only at the moment it was derived.

    ``start_s``/``end_s`` are absent for a document, which is what keeps
    ``project_structure``'s ``row.get`` returning ``None`` and the locator on
    its byte-range branch.

    ``page`` is the page the chunk *starts* on, looked up by ``para_from``
    against the extraction's `positions` sidecar. It is the field that makes
    `ChunkNode.page` — declared, written by `_MERGE_CHUNKS`, returned by the
    graph, and set by nothing until now — and wakes the `p. {page}` branch
    `_locator` has carried unreachable since it was written. Absent when the
    document has no positions, never 0: a citation naming a page the reader
    cannot find is worse than one naming none.
    """
    row: dict[str, Any] = {
        "index": chunk.index,
        "kind": chunk.kind,
        "chapter": chunk.chapter,
        "section": chunk.section,
        "text": chunk.text,
        "context": chunk.context,
        "overlap": chunk.overlap,
        "embed_text": chunk.embed_text(),
        "char_from": chunk.char_from,
        "char_to": chunk.char_to,
        "cell_ref": chunk.cell_ref,
        "para_from": chunk.para_from,
        "para_to": chunk.para_to,
    }
    page = (pages or {}).get(chunk.para_from)
    if page is not None:
        row["page"] = page
    if start_s is not None:
        row["start_s"] = start_s
        row["end_s"] = end_s
    return row


def apply_overrides(
    chunks: "list[StoredChunk]", overrides: "dict[int, Any]"
) -> "tuple[list[StoredChunk], list[Any]]":
    """Substitute what a person wrote, and report what no longer fits.

    Applied **before** the engine embeds, so the dense vector, the BM25 sparse
    vector, the scripture filters and the stored payload are all of the same
    words. Replacing the text in the payload alone would retrieve on what the
    chunk used to say and display what it now says — plausible, and wrong in a
    way nothing downstream could see.

    An override whose `replaced_sha256` no longer matches the chunk at its
    index is **orphaned and returned**, never applied: `chunk_index` is not
    stable across a re-cut, and reapplying by index alone attaches a person's
    correction to a different passage. Returned rather than dropped, because a
    screen has to be able to say "this edit no longer fits the document" — and
    a correction that vanished without a word is worse than one that stopped
    being applied.

    `embed_text` moves with the text. It is what the vector is made from, and a
    chunk whose text changed while its embed text did not would be findable
    only by the words it no longer contains.
    """
    import dataclasses

    out: list[StoredChunk] = []
    orphans: list[Any] = []
    seen: set[int] = set()
    for chunk in chunks:
        override = overrides.get(chunk.index)
        if override is None:
            out.append(chunk)
            continue
        seen.add(chunk.index)
        if not override.applies_to(chunk.text):
            orphans.append(override)
            out.append(chunk)
            continue
        if override.text is None:
            out.append(chunk)
            continue
        breadcrumb = chunk.breadcrumb()
        out.append(
            dataclasses.replace(
                chunk,
                text=override.text,
                _embed_text=(
                    f"{breadcrumb}\n\n{override.text}" if breadcrumb else override.text
                ),
            )
        )
    # An override pointing at an index the document no longer has is orphaned
    # for the same reason, and is the shape a re-cut that *shrank* produces.
    orphans.extend(o for i, o in overrides.items() if i not in seen)
    return out, orphans


class QdrantWriter:
    """Stamps this organisation's identity onto every point the engine produces.

    **Point ids come from the version, not the path.** The engine's
    `doc_id_for()` hashes the filename, which is what lets byte-identical
    duplicates index twice and then compete in ranking; using the content-derived
    version id means a re-index of the same bytes overwrites the same points and
    a duplicate file adds none. That also makes the calling activity idempotent,
    which Temporal requires of it anyway.
    """

    def __init__(
        self,
        qdrant: Any,
        *,
        tenant_id: str,
        library_id: str,
        document_id: str,
        version_id: str,
        source_title: str,
        model: str,
        dimensions: int,
        recorded_day: int | None = None,
        source_name: str = "",
        overrides: "dict[int, Any] | None" = None,
    ) -> None:
        self._q = qdrant
        self.tenant_id = tenant_id
        self.library_id = library_id
        self.document_id = document_id
        self.version_id = version_id
        self.source_title = source_title
        #: Two document-level facts a retrieval filter narrows on, written on
        #: every point of the version because a point is what a filter sees.
        #: `recorded_day` is days since the epoch — an integer so Qdrant's
        #: `range` applies with no datetime index — and absent, never zero,
        #: for a document with no date: zero would be 1970 and would match a
        #: range that reaches it. Both appended and defaulted so every
        #: existing caller and every existing point are unchanged.
        self.recorded_day = recorded_day
        self.source_name = source_name
        #: What a person changed about this version's chunks, by chunk index.
        #:
        #: Applied **here, at index time, from the catalog** rather than poked
        #: into a point afterwards. `upsert` writes the payload as a whole dict
        #: and Qdrant replaces it, so a flag set out of band is wiped by the
        #: next re-index or rebuild — silently, which is the "ids survive while
        #: what they point at changes" family this repository has recorded
        #: twice. The catalog is the source of truth and the index is derived
        #: from it, which is the same ordering `removal.py` states.
        self.overrides = overrides or {}
        #: Read by `runner.index_chunks`, which refuses to write vectors from a
        #: model this collection does not already hold. Two models of equal width
        #: are interchangeable to Qdrant and not to the cosine.
        self.model = model
        self.dimensions = dimensions

    # -- the scope every read, write and delete carries ----------------------

    @property
    def scope(self) -> dict[str, str]:
        """What identifies this version's points, and nothing wider.

        Used for the tail count, for removal, and as the evaluation's filter —
        one definition, so a measurement cannot end up scored against another
        document's chunks because two call sites disagreed.
        """
        return version_scope(self.tenant_id, self.version_id)

    def point_id_for(self, chunk_index: int) -> str:
        from docagent.qdrant import point_id

        return point_id(self.version_id, chunk_index)

    # -- the writer protocol -------------------------------------------------

    def upsert(self, rows: Sequence[Any]) -> int:
        from docagent.qdrant import Point

        points = [
            Point(
                id=self.point_id_for(r.chunk.index),
                dense=r.dense,
                sparse=r.sparse,
                payload={
                    # Written on every point and forced into every search. A
                    # point id is `uuid5(ns, f"{version_id}:{index}")` and the
                    # version id is salted, so two customers holding the same
                    # file no longer collide — but a collision is not the same
                    # thing as authorization, and this is what a query actually
                    # filters on.
                    "tenant_id": self.tenant_id,
                    "library_id": self.library_id,
                    "document_id": self.document_id,
                    "version_id": self.version_id,
                    # The graph's id for the same chunk, so a Qdrant hit can be
                    # expanded through the graph without a lookup table.
                    "chunk_id": make_chunk_id(self.version_id, r.chunk.index),
                    "source_title": self.source_title,
                    "chunk_index": r.chunk.index,
                    "kind": r.chunk.kind,
                    "chapter": r.chunk.chapter,
                    "section": r.chunk.section,
                    "breadcrumb": r.chunk.breadcrumb(),
                    "text": r.chunk.text,
                    "context": r.chunk.context,
                    # Indexes the *corrected* byte stream, not the original file:
                    # correction changed the offsets.
                    "char_span": [r.chunk.char_from, r.chunk.char_to],
                    "cell_ref": r.chunk.cell_ref,
                    **self._filterable(r.chunk.text),
                    **self._override(r.chunk),
                },
            )
            for r in rows
        ]
        self._q.upsert(points)
        return len(points)

    def _override(self, chunk: Any) -> dict[str, Any]:
        """What a person changed about this chunk, as payload.

        Two keys, both *absent* unless somebody edited the chunk, and that is
        the whole design. `disabled` is read as a `must_not` so absence means
        visible, and `edited` marks a chunk whose `char_span` no longer
        verifies against any stream this product holds, so a reader is told
        rather than left to assume.

        **The edited *text* is not here**, and that is the point: it is
        substituted by `apply_overrides` before the engine ever sees the chunk,
        so the vector, the BM25 sparse vector, the scripture filters and the
        payload are all of the same words. Replacing it in the payload alone
        would have retrieved on what the chunk used to say and displayed what
        it now says — plausible, and wrong in a way nothing could see.

        **An override that no longer matches its chunk is ignored, not
        applied.** `chunk_index` is not stable across a re-cut — one corrected
        profile took a document from 600 chunks to 631 — so reapplying by index
        alone would attach a person's correction to a different passage and
        read exactly like a good one. The orphan is left in the catalog for a
        screen to report; silently dropping it would be the same mistake with
        the evidence removed.
        """
        override = self.overrides.get(chunk.index)
        if override is None or not override.applies_to(chunk.text):
            return {}
        out: dict[str, Any] = {"edited": True}
        if override.disabled:
            out["disabled"] = True
        return out

    def _filterable(self, text: str) -> dict[str, Any]:
        """The payload fields a `Question` may narrow on beyond the scope.

        The scripture lists are computed per chunk from its own text through
        `scripture.payload_fields`, so a filter on `Romanos 8` reaches the
        chunk where the reference was *spoken* — and not the exposition that
        follows without repeating it, which the screen says in so many words.
        """
        refs, chapters = scripture.payload_fields(text)
        out: dict[str, Any] = {"scripture_refs": refs, "scripture_chapters": chapters}
        if self.recorded_day is not None:
            out["recorded_day"] = self.recorded_day
        if self.source_name:
            out["source_name"] = self.source_name
        return out

    def prune_tail(self, keep: int) -> int:
        """Delete this version's points whose `chunk_index` is at or above `keep`.

        `upsert` overwrites only the ids the new run produced (`0..keep-1`); it
        never removes ids beyond them. So **any** re-index that yields fewer
        chunks than the last one — a corrected profile, a different chunk size, a
        rejected tuning candidate — leaves the old tail alive in the collection,
        carrying `char_span`s into a byte stream nothing holds any more. Nothing
        else in this repository deletes them: `removal.py` deletes a whole
        version, and that is the only other delete there is.

        Ids are deterministic in the chunk index, so the stale range is exactly
        `[keep, old_count)` and needs no range filter — Qdrant's payload filter is
        equality-only and cannot express one.
        """
        old_count = self._q.count(self.scope)
        if old_count <= keep:
            return 0
        pruned = self._q.delete_by_ids(
            [self.point_id_for(i) for i in range(keep, old_count)]
        )
        log.info(
            "pruned %d stale point(s) left by a longer previous chunking of %s",
            pruned, self.version_id,
        )
        return pruned
