"""Finding candidate chunks: hybrid vector search, then graph expansion.

Order matters and is not arbitrary. Vector search decides whether the question
is about this corpus at all — that is what `topicality_gate` measures — and the
graph then adds context around what was found. Running the graph first would let
a confidently-wrong concept match pull in chunks for a question the corpus does
not cover, and the answer would cite real documents for a subject they never
discuss.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..graph import Graph, GraphError
from ..graph.schema import canonical_concept
from ..providers import Provider
from ..providers.gemini import RETRIEVAL_QUERY
from .types import Evidence, EvidenceClaim, Plan, Question

log = logging.getLogger(__name__)

#: Payload keys a caller may filter on. An allowlist because filters arrive from
#: the API and a free-form key would let a caller probe payload internals.
ALLOWED_FILTERS = frozenset({"document_id", "version_id", "kind", "library_id"})

#: Cosine floor on the dense leg. Inherited from the engine, where it was tuned:
#: `min_score` may only ever be applied to the dense prefetch, never to the
#: fused output (invariant #8), or lexical matches sail past it.
MIN_SCORE = 0.60

#: How many candidates to fuse before diversifying down to `top_k`.
CANDIDATE_LIMIT = 40

#: Chunks allowed from any one section. Measured in the engine: without a cap an
#: on-topic query spent 7 of 10 slots on near-identical chunks of one section —
#: fewer distinct sections than a nonsense query returned.
PER_SECTION = 2

#: Claims attached to any one chunk. The answering model has to read all of them
#: alongside the chunk's own text, and they compete with it for the attention
#: that makes a citation accurate — the same reasoning behind `Question.top_k`
#: being small. The template orders by confidence, so a cap keeps the best ones.
CLAIMS_PER_CHUNK = 3


class OffCorpus(Exception):
    """Nothing cleared the dense floor: the question is not about this library.

    Distinct from "found nothing relevant enough to answer with", because the
    remedy differs — one is a question for a different corpus, the other is a
    gap in this one.
    """

    def __init__(self, nearby: list[Evidence]) -> None:
        super().__init__("no chunk cleared the topicality floor")
        self.nearby = nearby


def search(
    settings: Settings, provider: Provider, question: Question, plan: Plan
) -> list[Evidence]:
    """Retrieve, expand, and return the evidence an answer may rest on."""
    from docagent.qdrant import Qdrant, SearchOpts, diversify

    # RETRIEVAL_QUERY, not RETRIEVAL_DOCUMENT. The model embeds questions and
    # passages asymmetrically on purpose (invariant #5) and using one task for
    # both measurably degrades retrieval.
    vector = provider.embed([question.text], task=RETRIEVAL_QUERY)[0].values

    filters = {
        k: v for k, v in question.filters.items() if k in ALLOWED_FILTERS
    }
    filters["library_id"] = question.library_id

    with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
        gate_opts = SearchOpts(
            limit=CANDIDATE_LIMIT, min_score=MIN_SCORE,
            dense_only=True, query_text=question.text, filters=filters,
        )
        on_topic = q.topicality_gate(vector, gate_opts)

        hits = q.search(
            vector,
            SearchOpts(
                limit=CANDIDATE_LIMIT, min_score=MIN_SCORE,
                query_text=question.text, filters=filters,
            ),
        )

    ranked = diversify(hits, PER_SECTION, question.top_k)
    evidence = [_evidence(h.payload, h.score, "vector") for h in ranked]

    if not on_topic:
        # Nearby results are still shown — "nothing found" with no examples is
        # indistinguishable from a broken index.
        raise OffCorpus(nearby=evidence[:3])

    return _expand(settings, question, plan, evidence)


def _expand(
    settings: Settings, question: Question, plan: Plan, evidence: list[Evidence]
) -> list[Evidence]:
    """Add graph context around what vector search already found.

    Best-effort: a graph that is down degrades the answer's context, and that is
    strictly better than refusing to answer a question the vector index could
    have answered on its own.
    """
    found = {e.chunk_id for e in evidence}
    added: list[Evidence] = []
    kept = evidence

    try:
        with Graph(settings.memgraph_url) as graph:
            # What the planner's decision gates is the *expansion* — the extra
            # chunks. It does not gate the rest of this block, and it used to:
            # returning early when the planner chose no traversal skipped
            # `_attach_citations`, and `answer._verify` drops any citation whose
            # chunk has no locator. A question the vector index had answered
            # perfectly well came back as "no verifiable citation".
            if plan.concepts:
                added += _by_concept(graph, question, plan, found)
            if plan.template_id is not None:
                added += _by_template(graph, plan, found, question.library_id)
            # Graph hits go after vector hits: the vector score is a similarity
            # to the actual question, while a graph hit is only topically
            # adjacent. Truncated before the two lookups below rather than after,
            # so neither pays for evidence that will not reach the prompt.
            kept = evidence + added[: max(0, question.top_k - len(evidence))]
            _attach_citations(graph, kept)
            _attach_claims(graph, kept, question.confidence_floor)
    except GraphError as e:
        log.warning("graph expansion unavailable, answering from vectors: %s", e)
        return evidence

    return kept


def _by_concept(graph: Graph, question: Question, plan: Plan, found: set[str]) -> list[Evidence]:
    rows = graph.query(
        "concept_by_name",
        {"canonical_names": [canonical_concept(c) for c in plan.concepts]},
    )
    if not rows:
        return []

    chunks = graph.query(
        "chunks_for_concepts",
        {
            "concept_ids": [r["id"] for r in rows],
            # Concepts are shared across documents by design, so the concept ids
            # above are library-agnostic and this is what keeps the chunks they
            # reach in scope.
            "library_id": question.library_id,
            "confidence_floor": question.confidence_floor,
            "limit": question.top_k,
        },
    )
    return _hydrate(
        graph, [r["id"] for r in chunks if r["id"] not in found], "graph", question.library_id
    )


def _by_template(graph: Graph, plan: Plan, found: set[str], library_id: str) -> list[Evidence]:
    """Turn whatever a template returned into chunks an answer can rest on.

    A row names a chunk in one of two ways, and both are read: templates over
    chunks put the id in `id`, while templates over *claims* put the claim's id
    there and name its chunk in `source_chunk_id`. Reading only `id` meant a
    claim template could be offered to the planner, chosen, and contribute
    nothing — `claims_between_concepts` is the graph's only concept-to-concept
    path, and it returns claims.

    The ids come from the graph, never from a model, and `_hydrate` scopes them
    to the asking library regardless.
    """
    assert plan.template_id is not None
    rows = graph.query(plan.template_id, dict(plan.params))
    ids: list[str] = []
    for r in rows:
        for key in ("id", "source_chunk_id"):
            candidate = str(r.data.get(key) or "")
            if candidate.startswith("chk_") and candidate not in found:
                found.add(candidate)
                ids.append(candidate)
                break
    return _hydrate(graph, ids, "graph", library_id)


def _hydrate(
    graph: Graph, chunk_ids: list[str], source: str, library_id: str
) -> list[Evidence]:
    """Fetch the text of chunks the graph named, within one library.

    The graph stores chunk text as well as Qdrant does, deliberately: an answer
    must be assemblable from the graph's own citation path even when a vector
    index has been rebuilt and its point ids have moved.

    **This is the choke point for the library scope, and it is deliberately
    belt-and-braces.** `chunks_for_concepts` already filters, but every path that
    turns a graph result into `Evidence` goes through here — including
    `_by_template`, which extracts chunk ids from whatever template the planner
    chose. Filtering once, here, means a template added later cannot reopen the
    hole by forgetting to scope itself.
    """
    if not chunk_ids:
        return []
    rows = graph.write(
        """
        MATCH (d:Document)-[:HAS_VERSION]->(v:DocumentVersion)-[:HAS_CHUNK]->(c:Chunk)
        WHERE c.id IN $ids AND d.library_id = $library_id
        RETURN c.id AS chunk_id, c.text AS text, c.kind AS kind,
               c.page AS page, v.id AS version_id, v.title AS title,
               d.id AS document_id
        """,
        # A required match, where this used to be OPTIONAL. A chunk with no
        # owning Document cannot be attributed to a library or shown a source,
        # and dropping it is the safe direction: an answer is not allowed to
        # cite something it cannot name the origin of.
        {"ids": chunk_ids, "library_id": library_id},
    )
    return [
        Evidence(
            chunk_id=r["chunk_id"],
            version_id=r["version_id"] or "",
            document_id=r["document_id"] or "",
            title=r["title"] or "",
            breadcrumb="",
            text=r["text"] or "",
            kind=r["kind"] or "cuerpo",
            score=0.0,
            source=source,
            page=r["page"],
        )
        for r in rows
    ]


def _attach_citations(graph: Graph, evidence: list[Evidence]) -> None:
    """Fill in each chunk's verifiable locator.

    Done in one query rather than per chunk: the answer step refuses any
    citation whose chunk has no locator, so a per-chunk round trip would put the
    graph on the critical path once per result.
    """
    ids = [e.chunk_id for e in evidence if e.chunk_id]
    if not ids:
        return
    rows = graph.query("citations_for_chunks", {"chunk_ids": ids, "limit": len(ids)})
    by_chunk = {r["chunk_id"]: r for r in rows}
    for e in evidence:
        if (row := by_chunk.get(e.chunk_id)) is not None:
            e.locator = row["locator"] or ""
            e.page = row["page"] if row["page"] is not None else e.page
            if row["section_title"]:
                e.breadcrumb = e.breadcrumb or row["section_title"]


def _attach_claims(graph: Graph, evidence: list[Evidence], floor: float) -> None:
    """Attach what a model read out of each chunk, above the confidence floor.

    One query for the whole set, like the citations and for the same reason.

    These are *proposals*, and they stay labelled as such all the way into the
    prompt: `answer.SYSTEM` tells the model that the chunk's own text wins any
    disagreement with a claim. Without that rule this is model output re-entering
    the context as though it were a source, which is the failure that makes a
    grounded-looking answer ungrounded.
    """
    ids = [e.chunk_id for e in evidence if e.chunk_id]
    if not ids:
        return
    rows = graph.query(
        "claims_for_chunks",
        {
            "chunk_ids": ids,
            "confidence_floor": floor,
            "limit": len(ids) * CLAIMS_PER_CHUNK,
        },
    )
    by_chunk: dict[str, list[EvidenceClaim]] = {}
    for r in rows:
        # The row limit is global and the ordering is by confidence, so one
        # heavily annotated chunk could otherwise spend the whole budget.
        bucket = by_chunk.setdefault(r["chunk_id"], [])
        if len(bucket) >= CLAIMS_PER_CHUNK:
            continue
        bucket.append(
            EvidenceClaim(
                text=r["text"] or "",
                confidence=float(r["confidence"] or 0.0),
                status=r["status"] or "sin_estado",
                quote=r["quote"] or "",
                concept=r["concept"] or "",
            )
        )
    for e in evidence:
        e.claims = by_chunk.get(e.chunk_id, [])


def _evidence(payload: dict, score: float, source: str) -> Evidence:
    return Evidence(
        chunk_id=str(payload.get("chunk_id", "")),
        version_id=str(payload.get("version_id", "")),
        document_id=str(payload.get("document_id", "")),
        title=str(payload.get("source_title", "")),
        breadcrumb=str(payload.get("breadcrumb", "")),
        text=str(payload.get("text", "")),
        kind=str(payload.get("kind", "cuerpo")),
        score=score,
        source=source,
    )
