"""The only Cypher a question is ever allowed to run.

A language model chooses *which* template and supplies *typed parameters*. It
never writes Cypher, and nothing here interpolates a planner-supplied value into
a query string — parameters travel as Bolt parameters, so a value cannot become
syntax.

That alone would be enough to stop injection, so the validator below is not
there to make the planner safe: it is there to make the *templates* safe. A
template is written by a person, and a person can write ``DETACH DELETE``, omit
a ``LIMIT``, or reference a label that no longer exists after a schema change.
:func:`validate_template` runs over the registry at import time, which turns all
three from a runtime incident into a failure to start.

Sessions are opened read-only as well, but that is decoration: Memgraph 3.12.0
executes a ``CREATE`` inside a ``READ`` session without complaint, because the
Bolt access mode is a Neo4j routing hint and Memgraph's RBAC is an Enterprise
feature. ``brainworker.graph.client`` records the measurement. So this file is
the enforcement, not a second layer behind one — which is why every rule below
runs at import time rather than per query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .schema import EDGES, LABELS, SEMANTIC_EDGES

ParamType = Literal["string", "int", "float", "id", "id_list", "string_list"]

#: Clauses that write, delete, or reach outside the query. ``LOAD CSV`` and the
#: procedure-call forms are here because Memgraph ships query modules that can
#: touch the filesystem and the network — a read-only *transaction* stops the
#: writes but not an ``import_util`` call.
FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "CREATE",
    "MERGE",
    "DELETE",
    "DETACH",
    "SET",
    "REMOVE",
    "DROP",
    "LOAD CSV",
    "FOREACH",
    "CALL",
    "USING PERIODIC",
    "FREE MEMORY",
    "STORAGE MODE",
    "DUMP DATABASE",
    "REPLICA",
    "TRIGGER",
    "STREAM",
    "AUTH",
    "USER",
    "ROLE",
)

#: A hard ceiling applied on top of whatever a template asks for. The planner
#: can request fewer rows; it cannot request more. Unbounded traversal on a
#: densely connected concept graph is the failure mode that takes the whole
#: desktop app down, and it needs no hostile input to happen.
MAX_LIMIT = 200

_PARAM = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_LABEL = re.compile(r":([A-Z][A-Za-z0-9_]*)")
_EDGE = re.compile(r":([A-Z][A-Z0-9_]*)\s*[*\]]")
_COMMENT = re.compile(r"//[^\n]*")
_STRING = re.compile(r"'[^']*'|\"[^\"]*\"")


class TemplateError(ValueError):
    """A template or a set of arguments that must never reach the database."""


@dataclass(frozen=True)
class Param:
    name: str
    type: ParamType
    required: bool = True
    default: Any = None
    #: A ceiling of this parameter's own, replacing :data:`MAX_LIMIT`. Only a
    #: template the planner cannot name may raise it — see
    #: :attr:`Template.planner_visible`. The point of declaring it per
    #: parameter rather than dropping the clamp is that a server-owned overview
    #: query still states a bound; "no ceiling" is the failure mode `MAX_LIMIT`
    #: exists to prevent, and it must not become reachable by omission.
    cap: int | None = None


@dataclass(frozen=True)
class Template:
    """One approved traversal, named so the planner can choose it by id."""

    id: str
    summary: str
    cypher: str
    params: tuple[Param, ...] = ()
    #: Whether results may rest on model-proposed edges. Templates that traverse
    #: only deterministic structure are marked False, and the UI can then say
    #: "this came from the document's own table of contents" rather than "a
    #: model thought these were related".
    uses_semantic_edges: bool = False
    #: Whether :func:`catalogue` offers this template to the planner. The
    #: catalogue is "templates that answer a question", not "every read the app
    #: makes": an overview query that returns thousands of rows for a screen to
    #: draw is neither useful to a planner nor free to describe to one, since
    #: every token of the catalogue is a token not spent on the question. Hidden
    #: templates are still reached by id through :func:`get`, so the API calls
    #: them exactly as it calls any other.
    planner_visible: bool = True


def _strip_literals(cypher: str) -> str:
    """Remove comments and string literals before keyword scanning.

    Without this, a template that legitimately matches a *title* containing the
    word "create" is rejected, and — worse — a reviewer learns to trust a check
    that has false positives and stops reading its output.
    """
    return _STRING.sub("''", _COMMENT.sub("", cypher))


def validate_template(t: Template) -> None:
    body = _strip_literals(t.cypher)
    upper = body.upper()

    for keyword in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", upper):
            raise TemplateError(f"{t.id}: forbidden keyword {keyword!r}")

    for label in set(_LABEL.findall(body)) - set(_EDGE.findall(body)):
        if label not in LABELS and label not in EDGES:
            raise TemplateError(f"{t.id}: unknown label or relationship {label!r}")

    declared = {p.name for p in t.params}
    used = set(_PARAM.findall(body))
    if unknown := used - declared:
        raise TemplateError(f"{t.id}: uses undeclared parameter(s) {sorted(unknown)}")
    if unused := declared - used:
        raise TemplateError(f"{t.id}: declares unused parameter(s) {sorted(unused)}")

    if "LIMIT" not in upper:
        raise TemplateError(f"{t.id}: no LIMIT — every template must bound its result")

    # The tenant predicate is checked here rather than left to a test, for the
    # same reason the LIMIT is: a template that loads is a template that can be
    # named by id, and "somebody will notice in review" is not a boundary. A new
    # traversal that forgets the filter does not start the process.
    #
    # Checked as a *used parameter* rather than by looking for a WHERE clause:
    # where the predicate belongs differs per template — the node the traversal
    # starts from, both ends of an edge count, both concepts of a claim — and a
    # check that guessed the shape would have to be loosened until it meant
    # nothing.
    if "tenant_id" not in used:
        raise TemplateError(
            f"{t.id}: does not filter on $tenant_id — every template must be "
            "scoped to one organisation, and salted ids are not authorization: "
            "a tenant id is a value its own members hold, so an id derived from "
            "it can be recomputed by anyone who knows both halves"
        )

    # A variable-length pattern with no upper bound expands to the whole
    # connected component. `[:HAS_SECTION*1..4]` and `[:HAS_SECTION*3]` are
    # bounded; `[:HAS_SECTION*]` and `[:HAS_SECTION*2..]` are not.
    #
    # Scoped to the inside of a relationship bracket on purpose: a bare scan for
    # `*` also matches `count(*)`, and a check that rejects valid queries is one
    # a reviewer learns to override.
    for bracket in re.findall(r"\[[^\]]*\]", body):
        star = re.search(r"\*\s*(\d*)\s*(\.\.)?\s*(\d*)", bracket)
        if star is None:
            continue
        lower, dots, upper_bound = star.groups()
        bounded = bool(upper_bound) if dots else bool(lower)
        if not bounded:
            raise TemplateError(
                f"{t.id}: unbounded variable-length pattern {bracket}"
            )

    semantic_used = bool(set(_EDGE.findall(body)) & SEMANTIC_EDGES)
    if semantic_used and not t.uses_semantic_edges:
        raise TemplateError(
            f"{t.id}: traverses a model-proposed edge without declaring "
            "uses_semantic_edges — the UI could not label the provenance"
        )


def bind(t: Template, args: dict[str, Any]) -> dict[str, Any]:
    """Type-check and coerce planner arguments into Bolt parameters.

    Every rejection here is a planner that returned something unusable, which is
    an ordinary event rather than an attack: models omit fields and pass strings
    where integers belong. The caller turns this into a re-plan, not a 500.
    """
    if extra := set(args) - {p.name for p in t.params}:
        raise TemplateError(f"{t.id}: unexpected argument(s) {sorted(extra)}")

    bound: dict[str, Any] = {}
    for p in t.params:
        if p.name not in args or args[p.name] is None:
            if p.required:
                raise TemplateError(f"{t.id}: missing required argument {p.name!r}")
            bound[p.name] = p.default
            continue
        bound[p.name] = _coerce(t.id, p, args[p.name])
    return bound


#: `tnt` joined the list with tenancy: a tenant id is now a value that
#: travels in a parameter, and one that did not match this pattern would be
#: refused at the door of every template.
_ID = re.compile(r"(lib|fld|doc|ver|sec|chk|con|clm|cit|tnt)_[0-9a-f]{24}")


def _coerce(template_id: str, p: Param, value: Any) -> Any:
    def fail(why: str):
        return TemplateError(f"{template_id}: parameter {p.name!r} {why}")

    if p.type == "string":
        if not isinstance(value, str):
            raise fail(f"must be a string, got {type(value).__name__}")
        return value
    if p.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise fail(f"must be an int, got {type(value).__name__}")
        # Every int a template takes today is a row ceiling, and letting a
        # planner hallucinate 10_000 is exactly the unbounded-result case the
        # validator exists to prevent.
        return max(1, min(value, p.cap or MAX_LIMIT)) if p.name.endswith("limit") else value
    if p.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise fail(f"must be a number, got {type(value).__name__}")
        return float(value)
    if p.type == "id":
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise fail(f"is not a graph id: {value!r}")
        return value
    if p.type == "id_list":
        if not isinstance(value, list) or not all(
            isinstance(v, str) and _ID.fullmatch(v) for v in value
        ):
            raise fail("must be a list of graph ids")
        return value[: p.cap or MAX_LIMIT]
    if p.type == "string_list":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise fail("must be a list of strings")
        return value[: p.cap or MAX_LIMIT]
    raise fail(f"has unknown type {p.type!r}")  # unreachable; keeps mypy honest


#: Every template takes it, and no caller may choose it.
#:
#: A planner returns a template id plus typed parameters, and this is the one
#: parameter whose value decides *whose data comes back* — so `planner._validate`
#: overwrites it with the caller's tenant before binding, exactly as it does for
#: `library_id`. Declared required and undefaulted on purpose: a default would be
#: somebody's tenant, and the wrong somebody.
_TENANT = Param("tenant_id", "string")

_LIMIT = Param("limit", "int", required=False, default=25)
_FLOOR = Param("confidence_floor", "float", required=False, default=0.6)

TEMPLATES: tuple[Template, ...] = (
    Template(
        id="document_outline",
        summary="The table of contents of one document version.",
        cypher="""
            MATCH (v:DocumentVersion {id: $version_id})-[:HAS_SECTION]->(s:Section)
            WHERE v.tenant_id = $tenant_id
            RETURN s.id AS id, s.title AS title, s.path AS path, s.level AS level
            ORDER BY s.path
            LIMIT $limit
        """,
        params=(Param("version_id", "id"), _TENANT, _LIMIT),
    ),
    Template(
        id="section_chunks",
        summary="Chunks under a section, in reading order, for citation lookup.",
        # `text` is returned because a reading surface needs the words, not just
        # the offsets — the Explore screen is the first caller that shows a chunk
        # rather than merely locating one. Callers that only need the locator can
        # ignore it; the LIMIT clamp is what bounds the payload.
        cypher="""
            MATCH (s:Section {id: $section_id})-[:HAS_CHUNK]->(c:Chunk)
            WHERE s.tenant_id = $tenant_id
            RETURN c.id AS id, c.kind AS kind, c.ordinal AS ordinal,
                   c.page AS page, c.char_start AS char_start, c.char_end AS char_end,
                   c.text AS text
            ORDER BY c.ordinal
            LIMIT $limit
        """,
        params=(Param("section_id", "id"), _TENANT, _LIMIT),
    ),
    Template(
        id="chunk_neighbours",
        summary="The chunks immediately before and after one chunk, for context.",
        cypher="""
            MATCH (c:Chunk {id: $chunk_id})
            WHERE c.tenant_id = $tenant_id
            OPTIONAL MATCH (c)-[:NEXT]->(after:Chunk)
            OPTIONAL MATCH (c)<-[:NEXT]-(before:Chunk)
            RETURN before.id AS before_id, before.text AS before_text,
                   c.id AS id, c.text AS text, c.kind AS kind,
                   c.char_start AS char_start, c.char_end AS char_end,
                   after.id AS after_id, after.text AS after_text
            LIMIT $limit
        """,
        params=(Param("chunk_id", "id"), _TENANT, _LIMIT),
    ),
    Template(
        id="concepts_in_version",
        summary="Concepts a document version mentions, most-supported first.",
        cypher="""
            MATCH (v:DocumentVersion {id: $version_id})-[:HAS_CHUNK]->(c:Chunk)
                  -[m:MENTIONS]->(k:Concept)
            WHERE m.confidence >= $confidence_floor
              AND v.tenant_id = $tenant_id
            RETURN k.id AS id, k.name AS name, k.type AS type,
                   k.description AS description,
                   count(c) AS mentions, max(m.confidence) AS confidence
            ORDER BY mentions DESC
            LIMIT $limit
        """,
        params=(Param("version_id", "id"), _TENANT, _FLOOR, _LIMIT),
        uses_semantic_edges=True,
    ),
    Template(
        id="chunks_for_concepts",
        summary=(
            "Candidate chunks in one library that mention any of the planner's "
            "concepts."
        ),
        # **Scoped to a library, and that is not cosmetic.** Concepts are shared
        # across documents on purpose — `project_concepts` merges by canonical
        # name so two books discussing "felicidad" reach the same node — which
        # means an unscoped traversal from a concept reaches every library that
        # has ever mentioned it. The vector leg filters on `library_id`
        # (`retrieve.search`), so without this the graph leg was the one way a
        # question asked of one library could pull evidence out of another, and
        # the citation guard would have passed it: that guard checks a cited
        # chunk was among the evidence *given*, not that the evidence was in
        # scope.
        #
        # The traversal still starts from the concept ids, which are indexed and
        # selective; the library is an added constraint rather than the entry
        # point.
        cypher="""
            MATCH (k:Concept)<-[m:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(:DocumentVersion)
                  <-[:HAS_VERSION]-(d:Document)
            WHERE k.id IN $concept_ids
              AND m.confidence >= $confidence_floor
              AND d.library_id = $library_id
              AND d.tenant_id = $tenant_id
            RETURN c.id AS id, c.version_id AS version_id,
                   max(m.confidence) AS confidence
            ORDER BY confidence DESC
            LIMIT $limit
        """,
        params=(
            Param("concept_ids", "id_list"),
            # Never taken from the planner. `planner._validate` overwrites it
            # with the caller's library the same way it overwrites
            # `confidence_floor`: a model asking to search a different library is
            # asking for something it must not get.
            Param("library_id", "string"),
            # Overwritten the same way, and for a sharper reason: a library is
            # one shelf inside one organisation, this is the organisation.
            _TENANT,
            _FLOOR,
            _LIMIT,
        ),
        uses_semantic_edges=True,
    ),
    Template(
        id="concept_by_name",
        summary="Resolve a user's wording to canonical concept nodes.",
        cypher="""
            MATCH (k:Concept)
            WHERE k.canonical IN $canonical_names
              AND k.tenant_id = $tenant_id
            RETURN k.id AS id, k.name AS name, k.type AS type
            LIMIT $limit
        """,
        params=(Param("canonical_names", "string_list"), _TENANT, _LIMIT),
    ),
    Template(
        id="related_documents",
        summary="Other versions sharing concepts with this one, and which ones they are.",
        # The owning `Document` comes back too, because a version id is not
        # something a person can act on: the Explore screen needs a document to
        # navigate to, and looking it up separately would be a second round trip
        # for an edge that is already projected.
        #
        # **The concepts themselves come back, not only how many.** A count
        # answers "these two documents share 57 concepts" and leaves the only
        # question a reader actually has — *which* 57 — unanswerable without a
        # request per document. They ride along here because the traversal that
        # counts them has already found them: `collect` instead of `count` costs
        # nothing extra on the server and removes a whole round trip per
        # selection from the client.
        #
        # Capped at 200 per document. A cap is needed because this is the one
        # template whose payload grows with the *product* of two documents'
        # concept sets; 200 is far above anything this corpus produces (the
        # widest overlap measured is 69), so in practice the list is complete.
        # `shared_concepts` stays the true count, so a client can tell that a
        # list is truncated by comparing its length against it — which is the
        # honest way to say "showing 200 of 240" rather than quietly showing 200.
        cypher="""
            MATCH (v:DocumentVersion {id: $version_id})-[:HAS_CHUNK]->(:Chunk)
                  -[m1:MENTIONS]->(k:Concept)<-[m2:MENTIONS]-(:Chunk)
                  <-[:HAS_CHUNK]-(other:DocumentVersion)
            WHERE other.id <> $version_id
              AND m1.confidence >= $confidence_floor
              AND m2.confidence >= $confidence_floor
              AND v.tenant_id = $tenant_id
              AND other.tenant_id = $tenant_id
            OPTIONAL MATCH (d:Document)-[:HAS_VERSION]->(other)
            WITH other, d, collect(DISTINCT k) AS shared
            RETURN other.id AS id, other.title AS title, d.id AS document_id,
                   size(shared) AS shared_concepts,
                   [c IN shared | c.id][..200] AS shared_concept_ids,
                   [c IN shared | c.name][..200] AS shared_concept_names
            ORDER BY shared_concepts DESC
            LIMIT $limit
        """,
        params=(Param("version_id", "id"), _TENANT, _FLOOR, _LIMIT),
        uses_semantic_edges=True,
    ),
    Template(
        id="claims_about_concept",
        summary="Claims supporting or contradicting a concept, with their evidence.",
        # The `DERIVED_FROM` hop is not decoration: it is the difference between
        # a claim a person can check and one that only looks checkable. Matching
        # on `ABOUT` alone returned claims whose chunk had been deleted — 214 of
        # them across 155 concepts on 2026-08-21, common ones like "Jesús" and
        # "Pablo" among them — each carrying a `source_chunk_id` the UI offers as
        # "check the source" and which resolves to nothing. A relation nobody can
        # verify is worse than no relation, because it still looks like evidence.
        #
        # This also makes the read surface agree with `claims_for_chunks`, which
        # reaches claims through the chunk and so never had the hole.
        cypher="""
            MATCH (k:Concept {id: $concept_id})<-[a:ABOUT]-(cl:Claim)
            MATCH (cl)-[:DERIVED_FROM]->(:Chunk)
            WHERE a.confidence >= $confidence_floor
              AND k.tenant_id = $tenant_id
            RETURN cl.id AS id, cl.text AS text, cl.confidence AS confidence,
                   cl.source_chunk_id AS source_chunk_id,
                   cl.quote AS quote,
                   cl.quote_char_start AS quote_char_start,
                   cl.quote_char_end AS quote_char_end,
                   // Coalesced here rather than left null, so every consumer
                   // gets a value and none of them has to invent a default.
                   // A claim projected before this field existed is `sin_estado`
                   // — unknown, which is not the same as asserted.
                   coalesce(cl.status, 'sin_estado') AS status
            ORDER BY cl.confidence DESC
            LIMIT $limit
        """,
        params=(Param("concept_id", "id"), _TENANT, _FLOOR, _LIMIT),
        uses_semantic_edges=True,
    ),
    Template(
        id="claims_between_concepts",
        summary="What the corpus says relating two concepts, with the quote to check it.",
        # The graph's only concept-to-concept path, and the reason `INVOLVES`
        # exists. Every other route between two concepts runs through a chunk
        # that mentioned both, which is co-occurrence — this one goes through a
        # claim a person can check against a fragment.
        #
        # Direction-free on purpose: which concept the extractor made the subject
        # is an artefact of how the sentence was written, and a reader asking
        # what relates two ideas does not know or care which was which. Both
        # orders are matched in one query rather than by a UNION, which keeps the
        # template a single statement the validator can read.
        cypher="""
            MATCH (cl:Claim)-[:DERIVED_FROM]->(:Chunk)
            MATCH (cl)-[a:ABOUT]->(k1:Concept)
            MATCH (cl)-[i:INVOLVES]->(k2:Concept)
            WHERE a.confidence >= $confidence_floor
              AND k1.tenant_id = $tenant_id
              AND k2.tenant_id = $tenant_id
              AND ((k1.id = $concept_id AND k2.id = $other_id)
                   OR (k1.id = $other_id AND k2.id = $concept_id))
            RETURN cl.id AS id, cl.text AS text, cl.confidence AS confidence,
                   cl.source_chunk_id AS source_chunk_id,
                   cl.quote AS quote,
                   coalesce(cl.status, 'sin_estado') AS status,
                   k1.name AS about, k2.name AS involves
            ORDER BY cl.confidence DESC
            LIMIT $limit
        """,
        params=(
            Param("concept_id", "id"),
            Param("other_id", "id"),
            _TENANT,
            _FLOOR,
            _LIMIT,
        ),
        uses_semantic_edges=True,
    ),
    Template(
        id="claims_for_chunks",
        summary="What a model read out of these chunks, for the answer to weigh.",
        cypher="""
            MATCH (cl:Claim)-[:DERIVED_FROM]->(c:Chunk)
            WHERE c.id IN $chunk_ids AND cl.confidence >= $confidence_floor
              AND c.tenant_id = $tenant_id
            OPTIONAL MATCH (cl)-[a:ABOUT]->(k:Concept)
            RETURN c.id AS chunk_id, cl.id AS id, cl.text AS text,
                   cl.confidence AS confidence, cl.quote AS quote,
                   coalesce(cl.status, 'sin_estado') AS status,
                   k.name AS concept
            ORDER BY cl.confidence DESC
            LIMIT $limit
        """,
        params=(Param("chunk_ids", "id_list"), _TENANT, _FLOOR, _LIMIT),
        # The claims are a model's reading of the chunk, not the chunk. Marked so
        # nothing downstream can present them as the document's own words.
        uses_semantic_edges=True,
    ),
    Template(
        id="citations_for_chunks",
        summary="Verifiable locators for a set of chunks — the last step before answering.",
        cypher="""
            MATCH (c:Chunk)-[:CITES]->(cit:Citation)
            WHERE c.id IN $chunk_ids
              AND c.tenant_id = $tenant_id
            RETURN c.id AS chunk_id, cit.id AS id, cit.locator AS locator,
                   cit.page AS page, cit.section_title AS section_title
            LIMIT $limit
        """,
        params=(Param("chunk_ids", "id_list"), _TENANT, _LIMIT),
    ),
    # -- overview reads ----------------------------------------------------
    #
    # The four below are named by the app, never by the planner. They answer
    # "what is in here?" rather than a question about a document, and two of
    # them return more rows than `MAX_LIMIT` allows — which is safe only
    # because `planner_visible=False` keeps them out of a model's reach.
    Template(
        id="graph_node_counts",
        summary="Cuántos nodos hay de cada tipo en todo el grafo.",
        # `labels(n)[0]` and not `labels(n)`: nothing here is projected with a
        # second label, and a list key would make the caller unpack a histogram
        # whose rows are all one-element lists. Bounded by the label vocabulary
        # itself — there are nine — so the LIMIT is a formality the validator
        # requires rather than a real truncation.
        cypher="""
            MATCH (n)
            WHERE n.tenant_id = $tenant_id
            RETURN labels(n)[0] AS label, count(n) AS total
            ORDER BY total DESC
            LIMIT 25
        """,
        params=(_TENANT,),
        planner_visible=False,
    ),
    Template(
        id="graph_edge_counts",
        summary="Cuántas aristas hay de cada tipo en todo el grafo.",
        cypher="""
            MATCH (a)-[r]->(b)
            WHERE a.tenant_id = $tenant_id AND b.tenant_id = $tenant_id
            RETURN type(r) AS label, count(r) AS total
            ORDER BY total DESC
            LIMIT 25
        """,
        params=(_TENANT,),
        # Set by hand: the pattern names no edge type, so the validator cannot
        # see that this histogram counts MENTIONS and ABOUT alongside HAS_CHUNK.
        # Some of what it returns is model-proposed, and the flag is what lets
        # the screen say which part.
        uses_semantic_edges=True,
        planner_visible=False,
    ),
    Template(
        id="library_documents",
        summary="Los libros activos de una biblioteca, tal como están proyectados.",
        # `v.active` rather than every version: a document holds one active
        # version and the overview draws one node per book. Without it a
        # re-indexed document would appear twice, once per version, joined to
        # nothing that says they are the same shelf entry.
        cypher="""
            MATCH (d:Document)-[:HAS_VERSION]->(v:DocumentVersion)
            WHERE d.library_id = $library_id AND v.active = true
              AND d.tenant_id = $tenant_id
            RETURN d.id AS document_id, v.id AS version_id,
                   v.title AS title, d.format AS format
            ORDER BY v.title, v.id
            LIMIT $document_limit
        """,
        params=(
            Param("library_id", "string"),
            _TENANT,
            Param("document_limit", "int", required=False, default=500, cap=1000),
        ),
        planner_visible=False,
    ),
    Template(
        id="library_mentions",
        summary="Menciones agregadas entre los libros de una biblioteca y sus conceptos.",
        # One row per (version, concept): repeated mentions collapse into a
        # single weighted edge, which is what `concepts_in_version` already does
        # for one book. `count(c)` is the weight and `max(m.confidence)` the
        # strength, so a canvas can draw both without a second request.
        #
        # `d.library_id` is required and undefaulted for the reason recorded in
        # `chunks_for_concepts` below: concepts merge by canonical name with no
        # library in the id, so anything that reaches a Concept reaches every
        # library that ever mentioned it.
        #
        # **`min_documents` is the volume control, and confidence is not.**
        # Measured on the real corpus (67 books, 2026-08-24): raising the floor
        # from 0.6 to 0.9 removes 3% of the edges, because the extractor is
        # confident about nearly everything it proposes. Degree removes 84% —
        # 10,835 concepts fall to 1,719 at `min_documents = 2`, and what goes is
        # every concept only one book mentions. Those are leaves: they cannot
        # connect two books, which is the one thing this graph is for. They stay
        # reachable through `concepts_in_version`, which is what the book's own
        # panel already calls.
        #
        # The aggregation runs twice on purpose. The first `WITH` collapses
        # chunks into one weight per (book, concept); the second counts the books
        # per concept so the filter can be applied to the *concept* while the
        # per-book weights survive in `rows` — a single pass would have to choose
        # between the two.
        #
        # `ORDER BY documents DESC, mentions DESC` is also the truncation policy:
        # if the cap is reached, what is lost is the least-connected edges rather
        # than an arbitrary slice. The tiebreakers make the order stable across
        # calls, so a re-fetch does not reshuffle a canvas — and they are the
        # *returned aliases*, not `k.id` and `v.id`. The RETURN aggregates, so by
        # then the pattern variables are out of scope and Memgraph answers
        # "Unbound variable: k" — at query time, not at import: the validator
        # reads labels and keywords, not scope.
        cypher="""
            MATCH (d:Document)-[:HAS_VERSION]->(v:DocumentVersion)
                  -[:HAS_CHUNK]->(c:Chunk)-[m:MENTIONS]->(k:Concept)
            WHERE d.library_id = $library_id
              AND d.tenant_id = $tenant_id
              AND v.active = true
              AND m.confidence >= $confidence_floor
            WITH k, v, count(c) AS weight, max(m.confidence) AS strength
            WITH k,
                 collect({version: v.id, weight: weight, strength: strength}) AS rows,
                 count(v) AS documents
            WHERE documents >= $min_documents
            UNWIND rows AS row
            RETURN row.version AS version_id, k.id AS concept_id, k.name AS name,
                   k.type AS concept_type, documents AS documents,
                   row.weight AS mentions, row.strength AS confidence
            ORDER BY documents DESC, mentions DESC, concept_id, version_id
            LIMIT $mention_limit
        """,
        params=(
            Param("library_id", "string"),
            _TENANT,
            _FLOOR,
            # 1 is "every concept", and it is reachable — the control offers it.
            # 2 is the subgraph that has edges between books at all, and was the
            # default until 2026-09-01. **3 now, because 2 was still a canvas
            # nobody could read.** Re-measured on the corpus that day: 12,823
            # concepts, 2,034 at degree ≥ 2 (the same 16% that survived the
            # 2026-08-24 measurement, so the ratio is stable here), and 858 at
            # ≥ 3. Two thousand nodes is not a picture. The step after this one
            # is ≥ 5 at 322, which starts reading as a map of general topics
            # rather than of the bridges between these particular books.
            Param("min_documents", "int", required=False, default=3),
            # **Load-bearing, not defensive, since the desktop client started
            # fetching this envelope whole.** The client holds the
            # `min_documents = 1` response in memory and derives every threshold
            # from it, which is exact only while the envelope is not cut — see
            # the `ORDER BY` note above: `documents DESC` is both the first sort
            # key and the filter key, so a threshold's rows are a *prefix* of
            # this ordering and the equivalence survives truncation, but the
            # rows that fell off the end do not.
            #
            # 20000 was set against 15,367 rows measured 2026-08-24. Re-measured
            # 2026-09-01 on the same library, now 73 books: **17,814 rows at
            # `min_documents = 1`, floor 0.5 — 89% of that cap.** One more
            # mid-sized book took it over, and the symptom would not have been a
            # refusal but a silently smaller graph at *every* threshold.
            #
            # 60000 is ~240 books at today's 247 rows per book. It is also above
            # the hard ceiling on rows at any floor: rows are distinct
            # (version, concept) pairs and the whole graph holds 27,991
            # `MENTIONS`, so lowering the floor can no longer cut this library.
            # `default` moves with `cap` because no caller has ever passed this
            # parameter — the default is the operative number, and
            # `truncated.edges` is compared against it.
            Param("mention_limit", "int", required=False, default=60000, cap=60000),
        ),
        uses_semantic_edges=True,
        planner_visible=False,
    ),
)

BY_ID: dict[str, Template] = {t.id: t for t in TEMPLATES}


def get(template_id: str) -> Template:
    try:
        return BY_ID[template_id]
    except KeyError:
        raise TemplateError(
            f"no such template {template_id!r}; allowed: {sorted(BY_ID)}"
        ) from None


def catalogue() -> list[dict[str, Any]]:
    """What the planner is shown. Deliberately not the Cypher.

    Sending the query text would invite the model to edit it, and every token of
    it is a token not spent on the question. Templates marked
    ``planner_visible=False`` are left out for the same reason: the app reads
    them by id, and a planner has no use for a query that returns a whole
    library for a canvas to draw.
    """
    return [
        {
            "id": t.id,
            "summary": t.summary,
            "params": [
                {"name": p.name, "type": p.type, "required": p.required}
                for p in t.params
            ],
        }
        for t in TEMPLATES
        if t.planner_visible
    ]


if len(BY_ID) != len(TEMPLATES):
    raise TemplateError("duplicate template id in the registry")
for _t in TEMPLATES:
    validate_template(_t)
