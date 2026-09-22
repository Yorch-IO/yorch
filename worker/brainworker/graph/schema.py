"""Labels, relationship types, and the identifier rules that make projection idempotent.

Memgraph holds a *projection*, never the source of truth — Postgres does. That
makes re-projection an operation the pipeline must be free to repeat after any
partial failure, which in turn means every node id has to be a pure function of
the content it names. Nothing here reads a sequence or a clock.

**Document identity is the path; version identity is the content.** This is the
one asymmetry worth stating outright, because it fixes an inherited defect:
``docagent.doc_id_for()`` hashes the *path*, so two byte-identical files index
twice and then compete against each other in ranking. Here, one
``DocumentVersion`` exists per distinct sha256 no matter how many paths point at
it, so the expensive per-content work — correction, chunking, embedding,
semantic extraction — is done once and the duplicate arrives as a second
``HAS_VERSION`` edge rather than a second set of vectors.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# ---------------------------------------------------------------------------
# Labels and relationship types
# ---------------------------------------------------------------------------

#: Every node label the projection may create. The Cypher validator checks
#: planner-supplied labels against this set, so a label absent here cannot be
#: reached by a question no matter what the planner returns.
LABELS: frozenset[str] = frozenset(
    {
        "Library",
        "SourceFolder",
        "Document",
        "DocumentVersion",
        "Section",
        "Chunk",
        "Concept",
        "Claim",
        "Citation",
    }
)

#: Edges the pipeline derives from structure alone. They carry no confidence
#: because there is nothing probabilistic about them: either the parser found a
#: section inside a document or it did not.
DETERMINISTIC_EDGES: frozenset[str] = frozenset(
    {
        "CONTAINS",
        "HAS_VERSION",
        "HAS_SECTION",
        "HAS_CHUNK",
        "CITES",
        "NEXT",
        "PREVIOUS",
        "DERIVED_FROM",
    }
)

#: Edges a language model proposed. Every one of these must carry the four
#: provenance properties in :data:`SEMANTIC_EDGE_PROPERTIES`; an answer may not
#: rest on one whose ``confidence`` is below the configured threshold.
SEMANTIC_EDGES: frozenset[str] = frozenset(
    #: `INVOLVES` is the second concept a claim brings into relation with the one
    #: it is `ABOUT`. It is what gives the graph a concept-to-concept path at all:
    #: until it existed the only route between two concepts was a chunk that
    #: mentioned both, which is co-occurrence, not a relation anybody stated.
    #:
    #: Ported from graphrag's claim `subject`/`object`, and deliberately hung off
    #: the `Claim` rather than drawn between the concepts directly — an edge
    #: cannot carry the verified quote, and a relation nobody can check is worse
    #: than no relation.
    #:
    #: `SUPPORTS`, `CONTRADICTS` and `RELATED_TO` are declared and have never been
    #: projected by anything. They are vocabulary, not a contract.
    {"MENTIONS", "ABOUT", "INVOLVES", "SUPPORTS", "CONTRADICTS", "RELATED_TO"}
)

EDGES: frozenset[str] = DETERMINISTIC_EDGES | SEMANTIC_EDGES

#: Required on every semantic edge. Without ``source_chunk_id`` a relation
#: cannot be shown to the user as evidence, which makes it unusable for an
#: answer that must cite; without ``extractor_model`` a bad extraction cannot be
#: attributed and re-run when the model changes.
SEMANTIC_EDGE_PROPERTIES: frozenset[str] = frozenset(
    {"confidence", "extractor_model", "created_at", "source_chunk_id"}
)

#: Below this, a semantic edge is stored but may not support an answer. It is a
#: chosen starting value with no measured baseline behind it — the same caveat
#: the engine's recall@5 target of 0.85 carries — and belongs in a tuning loop
#: once there is an eval set for relation extraction.
DEFAULT_CONFIDENCE_FLOOR = 0.6


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

_ID_DIGEST_BYTES = 12  # 24 hex characters: collision-free at any corpus size a
                       # personal library reaches, and short enough to read in a
                       # Cypher result or a UI tooltip.


def _digest(*parts: str) -> str:
    """A stable short digest over the given parts.

    The separator is a NUL byte rather than a printable character so no
    combination of inputs can produce the same joined string as a different
    combination — ``("a:b", "c")`` and ``("a", "b:c")`` must not collide.
    """
    h = hashlib.blake2s(digest_size=_ID_DIGEST_BYTES)
    h.update("\0".join(parts).encode("utf-8"))
    return h.hexdigest()


def library_id(name: str) -> str:
    return f"lib_{_digest(name)}"


def source_folder_id(library: str, path: str) -> str:
    return f"fld_{_digest(library, path)}"


def document_id(library: str, source_key: str) -> str:
    """Identity of a document *slot* in a library: where it came from.

    ``source_key`` is the library-relative path, not the absolute one, so moving
    a whole library directory does not orphan every document in it.
    """
    return f"doc_{_digest(library, source_key)}"


#: The tenant every row minted before tenancy existed belongs to. Matches the
#: column default in `20260826120000_tenancy` and the constant the NestJS plane
#: uses.
LEGACY_TENANT_ID = "tnt_000000000000000000000001"


def tenant_id(slug: str) -> str:
    """Identity of an organisation, derived from its slug.

    Same shape as every other id here (`_digest`, 24 hex), which is what lets
    `tnt` join the `_ID` regex in `graph/queries.py` rather than needing a second
    form of identifier. Derived rather than random so that seeding the same
    organisation twice — a script re-run, a second environment built from the
    same list — produces the same row instead of a duplicate, and so an operator
    can compute the id of a slug without reading the database.

    The slug is the identity, so it must be settled before anything is indexed:
    renaming an organisation is free, re-slugging it is not, because every
    `ver_` and `con_` under it is salted with this value.

    `LEGACY_TENANT_ID` is deliberately **not** this function's output for any
    slug — it is a literal, minted before this existed, and `_salt` recognises
    it by value.
    """
    return f"tnt_{_digest(slug)}"


def _salt(tenant: str) -> tuple[str, ...]:
    """The tenant's contribution to a derived id, and why it is conditional.

    Every id below is a digest of its inputs, so adding a tenant to the inputs
    changes the id. For a corpus minted before tenancy existed that would
    invalidate every `ver_`, `sec_`, `chk_`, `clm_` and `cit_` in the graph and
    every point id in Qdrant at once — and the projections could only be rebuilt
    from artifacts. **Measured on this installation on 2026-08-26: 69 indexed
    versions, all 69 with a `chunks` artifact recorded, and only 31 with the
    file still on disk.** The other 38 would have needed the full pipeline
    re-run, correction included, against source files that in many cases are no
    longer recorded either.

    So the legacy tenant keeps the derivation it already has. That is not a
    special case bolted on: it is what a schema migration does when it preserves
    existing keys, and it is the difference between a rename and losing 38
    books' worth of paid work. Every tenant minted since salts normally, so no
    two tenants can collide — legacy's inputs are a strict prefix of nobody's.
    """
    return () if tenant == LEGACY_TENANT_ID else (tenant,)


def version_id(content_sha256: str, tenant: str) -> str:
    """Identity of one exact byte sequence *within one tenant*.

    Not salted with the document, deliberately: two identical files in the same
    library must resolve to one version so their chunks are embedded once. It
    *is* salted with the tenant, because the opposite is worse — two customers
    importing the same PDF computed the same id, and `document_version.id` is a
    primary key, so the second import collided on it before the widened
    `UNIQUE (tenant_id, content_sha256)` could even be reached.
    """
    if not _SHA256.fullmatch(content_sha256):
        raise ValueError(f"not a sha256 hex digest: {content_sha256!r}")
    salt = _salt(tenant)
    if not salt:
        return f"ver_{content_sha256[:24]}"
    return f"ver_{_digest(tenant, content_sha256)}"


def section_id(version: str, path: tuple[int, ...]) -> str:
    """Identity of a section by its position in the hierarchy.

    Keyed on the ordinal path (``(1, 2, 3)`` for the third child of the second
    child of the first top-level section) rather than the title, because titles
    repeat — "Introducción" appears once per part in most of the corpus — and a
    title-keyed id would silently merge them into one node.
    """
    return f"sec_{_digest(version, '.'.join(str(i) for i in path))}"


def chunk_id(version: str, index: int) -> str:
    if index < 0:
        raise ValueError(f"chunk index must not be negative: {index}")
    return f"chk_{_digest(version, str(index))}"


def concept_id(name: str, tenant: str) -> str:
    """Identity of a concept, canonicalised so the graph converges — per tenant.

    Concepts are the one node type meant to be shared across documents, and that
    sharing is what makes graph traversal worth having: the same idea written
    "Justificación por la fe", "justificacion por la fe" and "JUSTIFICACIÓN POR
    LA FE" has to land on one node.

    Shared across *tenants* is a different thing, and it is a leak rather than a
    feature. `Concept.description_raw` accumulates a description per chunk that
    mentions it, so one node would carry two customers' text; and the degree a
    reader uses to judge whether a concept joins anything would count another
    customer's books. Filtering the traversal cannot fix either — the content is
    already on the node.
    """
    salt = _salt(tenant)
    return f"con_{_digest(*salt, canonical_concept(name))}"


def claim_id(source_chunk: str, text: str) -> str:
    return f"clm_{_digest(source_chunk, _collapse_space(text))}"


def citation_id(chunk: str, locator: str) -> str:
    return f"cit_{_digest(chunk, locator)}"


_SHA256 = re.compile(r"[0-9a-f]{64}")
_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _collapse_space(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def canonical_concept(name: str) -> str:
    """Fold a concept name to its comparison key.

    Accents are stripped for matching only. The *display* name keeps them: this
    corpus is Spanish, and showing the user "justificacion" would be wrong even
    though it is the right thing to hash.
    """
    folded = unicodedata.normalize("NFKD", name.casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _collapse_space(_PUNCT.sub(" ", folded))


def may_merge_concepts(a: str, b: str) -> bool:
    """Whether two canonical concept names may ever be treated as one.

    **Ported as a rule, not as a feature.** RAGFlow resolves near-duplicate
    entities with an LLM behind a cheap lexical gate
    (`rag/graphrag/entity_resolution.py`), and the most transferable thing in
    it is a four-line veto: never merge a pair whose 2-gram symmetric
    difference contains a digit. It is what stops "GPT-3" becoming "GPT-4".

    Measured on this corpus before anything was built with it, over the 13,005
    distinct canonicals in the 41 semantics artifacts: a gate at RAGFlow's own
    threshold proposes **14,812** pairs and this veto refuses **804** of them —
    and every one of those is a scripture reference. `1 corintios 1 7` against
    `1 corintios 11 3`, `1 pedro 2 2` against `1 pedro 3 7`: one edit apart, a
    verse apart, and merging them would silently reattribute a quotation in a
    theology corpus.

    **Nothing merges concepts today**, and that is a decision the same
    measurement settled. The 14,008 pairs the veto *allows* are overwhelmingly
    noise at that threshold — `aborigenes` against `abortistas`, `abu talib`
    against `australia` — and tightening to a single edit leaves 358 pairs of
    which a large share are `adriana`/`adriano`, `alejandra`/`alejandro`,
    `adulterio`/`adultero`: different people and different concepts, one letter
    apart. So the resolution pass is not built, the number that would justify
    it is written down, and this guard exists so that anything which *does*
    merge two concepts later cannot skip it.
    """
    if a == b:
        return True
    diff = _grams(a) ^ _grams(b)
    return not any(ch.isdigit() for gram in diff for ch in gram)


def _grams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


# ---------------------------------------------------------------------------
# Indexes and constraints
# ---------------------------------------------------------------------------

#: Run on every startup. Memgraph treats both as idempotent, and stating them in
#: code rather than in a one-time migration means a graph rebuilt from scratch
#: after a failed projection comes back with the same guarantees.
#:
#: The uniqueness constraints are what make ``MERGE`` on ``id`` safe under the
#: concurrent activity retries Temporal will produce: without them two retries
#: of the same activity can both fail to find a node and both create one.
SCHEMA_STATEMENTS: tuple[str, ...] = tuple(
    [f"CREATE INDEX ON :{label}(id);" for label in sorted(LABELS)]
    + [
        f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.id IS UNIQUE;"
        for label in sorted(LABELS)
    ]
    + [
        # Lookups that traversal templates make on every question.
        "CREATE INDEX ON :Concept(canonical);",
        "CREATE INDEX ON :DocumentVersion(content_sha256);",
        "CREATE INDEX ON :Document(library_id);",
        "CREATE INDEX ON :Chunk(version_id);",
    ]
)
