"""Writing a document version's structure and semantics into Memgraph.

Every statement here is a ``MERGE`` keyed on a derived id, so projecting the
same version twice converges instead of duplicating. That is not tidiness: a
Temporal activity is retried on any transport failure, and an activity that had
already written half the graph before the connection dropped must be safe to run
again from the top.

Structure and semantics are projected separately and in that order. Structure is
free and deterministic; semantics costs money and is model-dependent. Splitting
them means a failed extraction leaves a document that can still be browsed,
cited and searched — only its concept edges are missing — rather than a document
that is absent from the library entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .client import Graph
from .schema import (
    SEMANTIC_EDGE_PROPERTIES,
    SEMANTIC_EDGES,
    canonical_concept,
    chunk_id,
    citation_id,
    claim_id,
    concept_id,
    document_id,
    section_id,
    version_id,
)

#: Batch size for chunk and edge writes. Memgraph accepts a list parameter and
#: unwinds it server-side, which turns 4 000 chunks from 4 000 round trips into
#: eight. The number is bounded because one transaction holding every chunk of a
#: 900-page book is how the 2 GiB memory limit gets reached.
BATCH = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SectionNode:
    path: tuple[int, ...]
    title: str
    level: int
    #: Byte offsets into the corrected text, carried through from docagent so a
    #: citation can be resolved back to the source without re-parsing.
    char_start: int | None = None
    char_end: int | None = None


@dataclass(frozen=True)
class ChunkNode:
    ordinal: int
    kind: str
    text: str
    char_start: int
    char_end: int
    #: The ordinal path of the section this chunk sits under, or None for
    #: front matter that precedes every heading.
    section_path: tuple[int, ...] | None = None
    page: int | None = None
    sheet: str | None = None
    slide: int | None = None
    qdrant_point_id: str | None = None


@dataclass(frozen=True)
class VersionNode:
    library: str
    source_key: str
    content_sha256: str
    title: str
    author: str | None = None
    fmt: str = "unknown"
    indexed_at: str = field(default_factory=_now)
    sections: Sequence[SectionNode] = ()
    chunks: Sequence[ChunkNode] = ()

    @property
    def document(self) -> str:
        return document_id(self.library, self.source_key)

    @property
    def version(self) -> str:
        return version_id(self.content_sha256)


@dataclass(frozen=True)
class SemanticEdge:
    """A model-proposed relation, always traceable to the chunk that produced it."""

    type: str
    source_id: str
    target_id: str
    confidence: float
    extractor_model: str
    source_chunk_id: str
    created_at: str = field(default_factory=_now)

    def properties(self) -> dict[str, Any]:
        props = {
            "confidence": float(self.confidence),
            "extractor_model": self.extractor_model,
            "created_at": self.created_at,
            "source_chunk_id": self.source_chunk_id,
        }
        missing = SEMANTIC_EDGE_PROPERTIES - set(props)
        if missing:  # unreachable while the dataclass and the set agree
            raise ValueError(f"semantic edge missing {sorted(missing)}")
        return props


def _batched(items: Sequence[Any], size: int = BATCH) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

_MERGE_DOCUMENT = """
MERGE (d:Document {id: $document_id})
  ON CREATE SET d.created_at = $now
SET d.library_id = $library_id, d.source_key = $source_key,
    d.title = $title, d.author = $author, d.format = $format
MERGE (v:DocumentVersion {id: $version_id})
  ON CREATE SET v.created_at = $now
SET v.content_sha256 = $content_sha256, v.title = $title,
    v.indexed_at = $indexed_at, v.active = false
MERGE (d)-[:HAS_VERSION]->(v)
"""

_MERGE_SECTIONS = """
UNWIND $rows AS row
MATCH (v:DocumentVersion {id: $version_id})
MERGE (s:Section {id: row.id})
SET s.title = row.title, s.path = row.path, s.level = row.level,
    s.ordinal = row.ordinal, s.version_id = $version_id,
    s.char_start = row.char_start, s.char_end = row.char_end
MERGE (v)-[:HAS_SECTION]->(s)
"""

# Nesting is a separate pass because a child can appear before its parent in the
# input order, and MERGE on a not-yet-existing parent would create a bare node
# with no properties that the pass above would then never update.
_NEST_SECTIONS = """
UNWIND $rows AS row
MATCH (parent:Section {id: row.parent_id})
MATCH (child:Section {id: row.child_id})
MERGE (parent)-[:CONTAINS]->(child)
"""

_MERGE_CHUNKS = """
UNWIND $rows AS row
MATCH (v:DocumentVersion {id: $version_id})
MERGE (c:Chunk {id: row.id})
SET c.ordinal = row.ordinal, c.kind = row.kind, c.text = row.text,
    c.char_start = row.char_start, c.char_end = row.char_end,
    c.page = row.page, c.sheet = row.sheet, c.slide = row.slide,
    c.qdrant_point_id = row.qdrant_point_id, c.version_id = $version_id
MERGE (v)-[:HAS_CHUNK]->(c)
"""

_ATTACH_CHUNKS = """
UNWIND $rows AS row
MATCH (s:Section {id: row.section_id})
MATCH (c:Chunk {id: row.chunk_id})
MERGE (s)-[:HAS_CHUNK]->(c)
"""

# Reading order. Both directions are stored rather than one traversed backwards:
# `chunk_neighbours` is on the hot path of every answer that needs surrounding
# context, and an undirected match over a graph this dense is measurably worse
# than two indexed edges.
_LINK_CHUNKS = """
UNWIND $rows AS row
MATCH (a:Chunk {id: row.previous_id})
MATCH (b:Chunk {id: row.next_id})
MERGE (a)-[:NEXT]->(b)
MERGE (b)-[:PREVIOUS]->(a)
"""

_MERGE_CITATIONS = """
UNWIND $rows AS row
MATCH (c:Chunk {id: row.chunk_id})
MERGE (cit:Citation {id: row.id})
SET cit.locator = row.locator, cit.page = row.page,
    cit.section_title = row.section_title, cit.version_id = $version_id
MERGE (c)-[:CITES]->(cit)
"""

#: Drop citations of this version's chunks that this pass did not produce.
#:
#: `citation_id` is keyed on `(chunk, locator)` and the locator embeds the
#: document's title, so a re-projection under a changed title mints a *second*
#: Citation per chunk instead of updating the first. Found by rebuilding a
#: document whose surviving duplicate had a different title: three citations
#: became six, and the stale three still answered `citations_for_chunks`.
#:
#: Scoped to the chunks just projected, never `MATCH (cit:Citation)` globally:
#: a citation belongs to a chunk, and a sweep over the label would delete
#: another version's while it was mid-projection.
_PRUNE_CITATIONS = """
UNWIND $rows AS row
MATCH (c:Chunk {id: row.chunk_id})-[:CITES]->(cit:Citation)
WHERE cit.id <> row.id
DETACH DELETE cit
"""

_ACTIVATE = """
MATCH (d:Document {id: $document_id})-[:HAS_VERSION]->(old:DocumentVersion)
SET old.active = false
WITH d
MATCH (d)-[:HAS_VERSION]->(v:DocumentVersion {id: $version_id})
SET v.active = true
RETURN v.id AS id
"""


def project_structure(graph: Graph, version: VersionNode) -> dict[str, int]:
    """Write the document, its version, sections, chunks and citations.

    Returns counts rather than nothing so the run record can show what landed —
    "projected" with no numbers is indistinguishable from "projected nothing",
    which is exactly the failure an empty extraction produces.
    """
    now = _now()
    graph.write(
        _MERGE_DOCUMENT,
        {
            "document_id": version.document,
            "version_id": version.version,
            "library_id": version.library,
            "source_key": version.source_key,
            "content_sha256": version.content_sha256,
            "title": version.title,
            "author": version.author,
            "format": version.fmt,
            "indexed_at": version.indexed_at,
            "now": now,
        },
    )

    by_path = {
        s.path: section_id(version.version, s.path) for s in version.sections
    }
    section_rows = [
        {
            "id": by_path[s.path],
            "title": s.title,
            "path": ".".join(str(i) for i in s.path),
            "level": s.level,
            "ordinal": ordinal,
            "char_start": s.char_start,
            "char_end": s.char_end,
        }
        for ordinal, s in enumerate(version.sections)
    ]
    for batch in _batched(section_rows):
        graph.write(_MERGE_SECTIONS, {"version_id": version.version, "rows": list(batch)})

    nesting = [
        {"parent_id": by_path[s.path[:-1]], "child_id": by_path[s.path]}
        for s in version.sections
        if len(s.path) > 1 and s.path[:-1] in by_path
    ]
    for batch in _batched(nesting):
        graph.write(_NEST_SECTIONS, {"rows": list(batch)})

    chunk_ids = [chunk_id(version.version, c.ordinal) for c in version.chunks]
    chunk_rows = [
        {
            "id": cid,
            "ordinal": c.ordinal,
            "kind": c.kind,
            "text": c.text,
            "char_start": c.char_start,
            "char_end": c.char_end,
            "page": c.page,
            "sheet": c.sheet,
            "slide": c.slide,
            "qdrant_point_id": c.qdrant_point_id,
        }
        for cid, c in zip(chunk_ids, version.chunks)
    ]
    for batch in _batched(chunk_rows):
        graph.write(_MERGE_CHUNKS, {"version_id": version.version, "rows": list(batch)})

    attach = [
        {"section_id": by_path[c.section_path], "chunk_id": cid}
        for cid, c in zip(chunk_ids, version.chunks)
        if c.section_path is not None and c.section_path in by_path
    ]
    for batch in _batched(attach):
        graph.write(_ATTACH_CHUNKS, {"rows": list(batch)})

    order = [
        {"previous_id": a, "next_id": b} for a, b in zip(chunk_ids, chunk_ids[1:])
    ]
    for batch in _batched(order):
        graph.write(_LINK_CHUNKS, {"rows": list(batch)})

    citations = [
        {
            "chunk_id": cid,
            "id": citation_id(cid, locator),
            "locator": locator,
            "page": c.page,
            "section_title": _title_for(version, c),
        }
        for cid, c in zip(chunk_ids, version.chunks)
        if (locator := _locator(version, c))
    ]
    for batch in _batched(citations):
        graph.write(_MERGE_CITATIONS, {"version_id": version.version, "rows": list(batch)})
    # After the merge, not before: pruning first would leave a chunk with no
    # citation at all for the width of the transaction, and an answer built in
    # that window would have nothing to cite.
    for batch in _batched(citations):
        graph.write(_PRUNE_CITATIONS, {"rows": list(batch)})

    return {
        "sections": len(section_rows),
        "chunks": len(chunk_rows),
        "citations": len(citations),
    }


def _title_for(version: VersionNode, chunk: ChunkNode) -> str | None:
    if chunk.section_path is None:
        return None
    for s in version.sections:
        if s.path == chunk.section_path:
            return s.title
    return None


def _locator(version: VersionNode, chunk: ChunkNode) -> str:
    """A human-readable pointer the UI can open, and the answer can print.

    Built from whichever positional facts the extractor supplied, because they
    differ by format: a PDF has pages, a spreadsheet has sheets, a deck has
    slides, and a plain text file has only the byte range that every format
    carries.
    """
    parts = [version.title]
    if chunk.page is not None:
        parts.append(f"p. {chunk.page}")
    if chunk.sheet:
        parts.append(f"hoja {chunk.sheet}")
    if chunk.slide is not None:
        parts.append(f"diapositiva {chunk.slide}")
    if title := _title_for(version, chunk):
        parts.append(title)
    parts.append(f"[{chunk.char_start}:{chunk.char_end}]")
    return " · ".join(parts)


def activate(graph: Graph, version: VersionNode) -> None:
    """Make this the version questions see, and only once everything landed.

    The previous version stays in the graph. It is deactivated, not deleted, so
    a citation already shown to the user keeps resolving and a bad re-index can
    be rolled back by flipping the flag rather than re-running the pipeline.
    """
    graph.write(
        _ACTIVATE, {"document_id": version.document, "version_id": version.version}
    )


# ---------------------------------------------------------------------------
# Semantics
# ---------------------------------------------------------------------------

_MERGE_CONCEPTS = """
UNWIND $rows AS row
MERGE (k:Concept {id: row.id})
  ON CREATE SET k.created_at = $now, k.name = row.name
SET k.canonical = row.canonical, k.type = row.type,
    k.synonyms = row.synonyms,
    k.description_raw = coalesce(k.description_raw, []) +
        [d IN row.descriptions WHERE NOT d IN coalesce(k.description_raw, [])]
"""

#: Written by the condensation step, separately from the accumulation above. A
#: `Concept` therefore carries both: the raw readings each chunk contributed, and
#: the one description a person reads. Keeping the raw list is what lets the
#: condensation be re-run after a prompt change without re-extracting anything.
_SET_CONCEPT_DESCRIPTION = """
UNWIND $rows AS row
MATCH (k:Concept {id: row.id})
SET k.description = row.description
"""

_READ_CONCEPT_DESCRIPTIONS = """
MATCH (k:Concept)
WHERE k.id IN $ids
RETURN k.id AS id, k.name AS name,
       coalesce(k.description_raw, []) AS raw,
       k.description AS description
"""

# `coalesce` on the quote, not a plain assignment. A claim's id is keyed on its
# chunk and its text, so a quote once verified against that chunk stays valid for
# that id forever — and a rebuild replaying an artifact written before quotes
# existed must not null out a span the graph already has. The write is
# monotonic: a quote can be gained or replaced, never lost.
_MERGE_CLAIMS = """
UNWIND $rows AS row
MATCH (c:Chunk {id: row.source_chunk_id})
MERGE (cl:Claim {id: row.id})
SET cl.text = row.text, cl.confidence = row.confidence,
    cl.source_chunk_id = row.source_chunk_id,
    cl.quote = coalesce(row.quote, cl.quote),
    cl.quote_char_start = coalesce(row.quote_char_start, cl.quote_char_start),
    cl.quote_char_end = coalesce(row.quote_char_end, cl.quote_char_end),
    cl.status = coalesce(row.status, cl.status)
MERGE (cl)-[:DERIVED_FROM]->(c)
"""


def project_concepts(
    graph: Graph, concepts: Sequence[dict[str, Any]], *, now: str | None = None
) -> int:
    """Upsert concepts. Names collide across documents on purpose — see schema.

    ``descriptions`` are *appended* to whatever the concept already carries,
    because a concept is shared across documents and each one contributes its own
    reading. The append filters against what is already there, which is what
    keeps this idempotent: ``SET list = list + new`` is the one write in this
    module that a Temporal retry would double.
    """
    rows = [
        {
            "id": concept_id(c["name"]),
            "name": c["name"],
            "canonical": canonical_concept(c["name"]),
            "type": c.get("type"),
            "synonyms": list(c.get("synonyms") or []),
            # `.get`, like the claim fields: a `semantics.json` written before
            # descriptions existed must keep replaying.
            "descriptions": [
                d for d in (c.get("descriptions") or []) if str(d).strip()
            ],
        }
        for c in concepts
    ]
    for batch in _batched(rows):
        graph.write(_MERGE_CONCEPTS, {"rows": list(batch), "now": now or _now()})
    return len(rows)


def read_concept_descriptions(
    graph: Graph, concept_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """What each concept has accumulated, for the condensation step to weigh.

    Read here rather than in the activity so this module stays the only place
    that knows the shape of a `Concept`.
    """
    if not concept_ids:
        return []
    return graph.write(_READ_CONCEPT_DESCRIPTIONS, {"ids": list(concept_ids)})


def set_concept_descriptions(graph: Graph, rows: Sequence[dict[str, Any]]) -> int:
    """Write the description a person reads, leaving the raw list untouched."""
    payload = [{"id": r["id"], "description": r["description"]} for r in rows]
    for batch in _batched(payload):
        graph.write(_SET_CONCEPT_DESCRIPTION, {"rows": list(batch)})
    return len(payload)


def project_claims(graph: Graph, claims: Sequence[dict[str, Any]]) -> int:
    """Upsert claims, quote included when the extractor could verify one.

    Every field beyond the original three is read with ``.get``, because this is
    the function a rebuild replays an old ``semantics.json`` through. An artifact
    written before quotes existed has to keep projecting exactly as it did.
    """
    rows = [
        {
            "id": claim_id(c["source_chunk_id"], c["text"]),
            "text": c["text"],
            "confidence": float(c.get("confidence", 0.0)),
            "source_chunk_id": c["source_chunk_id"],
            "quote": c.get("quote"),
            "quote_char_start": c.get("quote_char_start"),
            "quote_char_end": c.get("quote_char_end"),
            "status": c.get("status"),
        }
        for c in claims
    ]
    for batch in _batched(rows):
        graph.write(_MERGE_CLAIMS, {"rows": list(batch)})
    return len(rows)


def project_semantic_edges(graph: Graph, edges: Sequence[SemanticEdge]) -> int:
    """Write model-proposed relations, one statement per relationship type.

    Cypher cannot parameterise a relationship type, and the alternative —
    interpolating ``edge.type`` into the query string — is the one place in this
    module where a bad value would become syntax. Grouping by type and checking
    each against :data:`SEMANTIC_EDGES` first means the interpolated token can
    only ever be one of five literals defined in this repository.
    """
    written = 0
    by_type: dict[str, list[SemanticEdge]] = {}
    for edge in edges:
        if edge.type not in SEMANTIC_EDGES:
            raise ValueError(
                f"{edge.type!r} is not a semantic edge type; "
                f"allowed: {sorted(SEMANTIC_EDGES)}"
            )
        by_type.setdefault(edge.type, []).append(edge)

    for edge_type, group in by_type.items():
        assert edge_type in SEMANTIC_EDGES  # the interpolation below rests on this
        cypher = f"""
        UNWIND $rows AS row
        MATCH (a {{id: row.source_id}})
        MATCH (b {{id: row.target_id}})
        MERGE (a)-[r:{edge_type}]->(b)
        SET r += row.props
        """
        rows = [
            {
                "source_id": e.source_id,
                "target_id": e.target_id,
                "props": e.properties(),
            }
            for e in group
        ]
        for batch in _batched(rows):
            graph.write(cypher, {"rows": list(batch)})
            written += len(batch)
    return written


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------
#
# Removal lives here, beside projection, and not in `queries.py`. That module is
# the *read* surface: a registry of templates a planner may name by id, which
# exists because Memgraph does not enforce read-only — a `CREATE` inside a
# `default_access_mode="READ"` session succeeds on 3.12.0, so the guarantee has
# to be structural rather than delegated to the database. A template id is a
# string a model can return. **Nothing destructive may ever become a template.**
# These functions are literals, called only by server-side code that already
# knows what it is deleting.


@dataclass(frozen=True)
class Removed:
    """What a removal actually destroyed, per label."""

    versions: int = 0
    sections: int = 0
    chunks: int = 0
    citations: int = 0
    claims: int = 0
    concepts_collected: int = 0
    documents: int = 0

    def merge(self, other: "Removed") -> "Removed":
        return Removed(
            versions=self.versions + other.versions,
            sections=self.sections + other.sections,
            chunks=self.chunks + other.chunks,
            citations=self.citations + other.citations,
            claims=self.claims + other.claims,
            concepts_collected=self.concepts_collected + other.concepts_collected,
            documents=self.documents + other.documents,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "versions": self.versions,
            "sections": self.sections,
            "chunks": self.chunks,
            "citations": self.citations,
            "claims": self.claims,
            "concepts_collected": self.concepts_collected,
            "documents": self.documents,
        }


_COUNT_VERSION_SUBGRAPH = """
MATCH (v:DocumentVersion {id: $version_id})
OPTIONAL MATCH (v)-[:HAS_CHUNK]->(c:Chunk)
OPTIONAL MATCH (v)-[:HAS_SECTION]->(s:Section)
OPTIONAL MATCH (c)-[:CITES]->(cit:Citation)
OPTIONAL MATCH (cl:Claim)-[:DERIVED_FROM]->(c)
RETURN count(DISTINCT v) AS versions, count(DISTINCT c) AS chunks,
       count(DISTINCT s) AS sections, count(DISTINCT cit) AS citations,
       count(DISTINCT cl) AS claims
"""

#: Concepts this version mentions, read *before* anything is deleted.
#:
#: Orphan collection is scoped to these rather than run over the whole label,
#: and the scoping is load-bearing rather than an optimisation. `project_concepts`
#: MERGEs concepts *before* `project_semantic_edges` attaches them, so a
#: concurrent extraction leaves a legitimately-new Concept with no edges for the
#: width of that window — and an unscoped `MATCH (k:Concept) WHERE NOT (k)--()`
#: would delete another run's work mid-flight. It also means removal never scans
#: the full Concept label.
#: Two ways in, and following only the first left concepts behind. A chunk
#: reaches a concept by `MENTIONS`, but `extract_semantics` also names a concept
#: as a *claim's* subject — `concepts.setdefault(about, …)` — and that one is
#: attached by `ABOUT` from the Claim, with no `MENTIONS` edge from any chunk at
#: all. Collecting on `MENTIONS` alone therefore left every claim-only concept
#: orphaned in the graph after its document was removed. Found by counting the
#: label after a removal, not by reading this query.
_CANDIDATE_CONCEPTS = """
MATCH (v:DocumentVersion {id: $version_id})-[:HAS_CHUNK]->(c:Chunk)
OPTIONAL MATCH (c)-[:MENTIONS]->(mentioned:Concept)
OPTIONAL MATCH (c)<-[:DERIVED_FROM]-(:Claim)-[:ABOUT]->(claimed:Concept)
// The third path, and it is not optional in practice: a concept reached only
// through a claim's `INVOLVES` would survive its last supporting chunk exactly
// the way claim-only concepts did before `ABOUT` was added here.
OPTIONAL MATCH (c)<-[:DERIVED_FROM]-(:Claim)-[:INVOLVES]->(involved:Concept)
WITH collect(DISTINCT mentioned.id) + collect(DISTINCT claimed.id)
     + collect(DISTINCT involved.id) AS ids
RETURN [i IN ids WHERE i IS NOT NULL] AS ids
"""

#: Claims and citations first, then chunks, then sections, then the version.
#: Order is irrelevant inside one transaction — `DETACH DELETE` drops the edges
#: either way — but stating it in dependency order is what makes the statement
#: list readable against the schema.
_DELETE_VERSION: tuple[str, ...] = (
    """
    MATCH (:DocumentVersion {id: $version_id})-[:HAS_CHUNK]->(c:Chunk)-[:CITES]->(cit:Citation)
    DETACH DELETE cit
    """,
    """
    MATCH (cl:Claim)-[:DERIVED_FROM]->(:Chunk)<-[:HAS_CHUNK]-(:DocumentVersion {id: $version_id})
    DETACH DELETE cl
    """,
    """
    MATCH (:DocumentVersion {id: $version_id})-[:HAS_CHUNK]->(c:Chunk)
    DETACH DELETE c
    """,
    """
    MATCH (:DocumentVersion {id: $version_id})-[:HAS_SECTION]->(s:Section)
    DETACH DELETE s
    """,
    """
    MATCH (v:DocumentVersion {id: $version_id})
    DETACH DELETE v
    """,
)

#: Run last and in the same transaction, so the graph is never observable in a
#: state where a concept's last supporting chunk is gone but the concept is not.
_COLLECT_ORPHAN_CONCEPTS = """
MATCH (k:Concept) WHERE k.id IN $candidates AND NOT (k)--()
DETACH DELETE k
"""

_SURVIVING_CONCEPTS = """
MATCH (k:Concept) WHERE k.id IN $candidates
RETURN count(k) AS surviving
"""


def remove_version(graph: Graph, version: str) -> Removed:
    """Delete one version's whole subgraph, leaving shared Concepts alone.

    Section, Chunk, Citation and Claim ids are all derived from the version or
    from one of its chunks (`schema.section_id`, `chunk_id`, `citation_id`,
    `claim_id`), so none of them can be reachable from another document and all
    are safe to delete outright.

    `Concept` is the one label deliberately shared — `project_concepts` merges by
    canonical name, which is what makes `related_documents` work at all — so it
    is never deleted for belonging to this version, only collected when this
    removal left it supporting nothing. That is hygiene rather than correctness:
    a later `MERGE` onto a surviving orphan would be perfectly correct, and an
    orphan is already unreachable from `concepts_in_version` and
    `related_documents`, both of which traverse `MENTIONS` from a Chunk.
    """
    counts = graph.write(_COUNT_VERSION_SUBGRAPH, {"version_id": version})
    # An aggregate over an empty match still returns one row, of zeros — so
    # "did the query return anything" is not the existence test, and using it as
    # one reported a removal of 1 version for an id that was never in the graph.
    # Removal is retried after a partial failure, which makes the second pass
    # over an id already gone the *normal* case, not an edge one.
    row = counts[0] if counts else None
    if row is None or int(row["versions"] or 0) == 0:
        return Removed()

    candidates = graph.write(_CANDIDATE_CONCEPTS, {"version_id": version})
    # Deduplicated here rather than in Cypher: the two `collect(DISTINCT …)`
    # lists are each distinct but are concatenated, so a concept that is both
    # mentioned by a chunk and the subject of a claim appears twice. Left as-is
    # it doubled the reported `concepts_collected` — the count is subtracted
    # from a set of survivors, so a duplicated candidate counts as two.
    ids: list[str] = sorted(set(candidates[0]["ids"])) if candidates else []

    graph.write_many(
        [(stmt, {"version_id": version}) for stmt in _DELETE_VERSION]
        + [(_COLLECT_ORPHAN_CONCEPTS, {"candidates": ids})]
    )

    surviving = 0
    if ids:
        rows = graph.write(_SURVIVING_CONCEPTS, {"candidates": ids})
        surviving = int(rows[0]["surviving"]) if rows else 0

    return Removed(
        versions=1,
        sections=int(row["sections"] or 0),
        chunks=int(row["chunks"] or 0),
        citations=int(row["citations"] or 0),
        claims=int(row["claims"] or 0),
        concepts_collected=len(ids) - surviving,
    )


#: `OPTIONAL MATCH` plus `count` rather than a pattern comprehension: the plain
#: form is supported everywhere and reads the same, and `count(other)` is 0 for
#: the unmatched row because `count` ignores nulls.
_VERSIONS_OF_DOCUMENT = """
MATCH (:Document {id: $document_id})-[:HAS_VERSION]->(v:DocumentVersion)
OPTIONAL MATCH (other:Document)-[:HAS_VERSION]->(v)
WHERE other.id <> $document_id
RETURN v.id AS id, count(other) AS others
"""

_DELETE_DOCUMENT = """
MATCH (d:Document {id: $document_id})
DETACH DELETE d
"""


def remove_document(graph: Graph, document: str) -> Removed:
    """Delete a document and every version no other document still holds.

    The check is made here rather than trusted from the caller because the graph
    is where `HAS_VERSION` lives. Byte-identical files at two paths share one
    `DocumentVersion` on purpose — that is the fix for `docagent.doc_id_for()`
    hashing the path — so deleting a document must be able to leave a version
    standing, with only its edge to this document removed.
    """
    rows = graph.write(_VERSIONS_OF_DOCUMENT, {"document_id": document})
    total = Removed()
    for row in rows:
        if int(row["others"] or 0) == 0:
            total = total.merge(remove_version(graph, str(row["id"])))

    graph.write(_DELETE_DOCUMENT, {"document_id": document})
    return total.merge(Removed(documents=1))
