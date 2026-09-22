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
from datetime import date
from typing import Any

from .. import scripture
from ..config import Settings
from ..graph import Graph, GraphError
from ..graph.schema import canonical_concept
from ..providers import Provider
from ..providers.gemini import RETRIEVAL_QUERY
from .effort import BUDGETS, DEFAULT_EFFORT, Budget, budget_for, resolve_top_k
from .types import Evidence, EvidenceClaim, Plan, Question

log = logging.getLogger(__name__)

#: Payload keys a caller may filter on. An allowlist because filters arrive from
#: the API and a free-form key would let a caller probe payload internals.
#:
#: **`tenant_id` is deliberately absent, and must stay absent.** It is not a
#: narrowing a caller may request; it is the scope the caller is confined to,
#: and it is written over whatever arrived just below. Adding it here would turn
#: the one filter that decides whose corpus is searched into one a request can
#: name.
ALLOWED_FILTERS = frozenset(
    {"document_id", "version_id", "kind", "library_id", "source_name"}
)

#: `date(1970, 1, 1).toordinal()`: `recorded_day` on a point is days since the
#: epoch, and a bound here is turned into the same integer. One constant on
#: each side, both named, because a filter that computed the day differently
#: from the writer would narrow to nothing and report it as `off_corpus`.
EPOCH_ORDINAL = 719163


def narrowings(question: Question) -> dict[str, Any]:
    """The payload filters beyond the scope, from the question's own fields.

    Three shapes, chosen by value for `Qdrant._filter`: an equality for the
    source, a `range` on the recording day, and a `match any` on whichever
    scripture list the query normalised to. A date that does not parse or a
    reference the table cannot vouch for narrows on nothing rather than on a
    guess — the field's pattern already refused a malformed date at the route,
    so what reaches here is well-formed or empty.
    """
    out: dict[str, Any] = {}
    if question.source_name:
        out["source_name"] = question.source_name
    bounds: dict[str, int] = {}
    if question.recorded_from:
        bounds["gte"] = _day(question.recorded_from)
    if question.recorded_to:
        bounds["lte"] = _day(question.recorded_to)
    if bounds:
        out["recorded_day"] = bounds
    if question.scripture:
        matched = scripture.normalise_query(question.scripture)
        if matched is not None:
            field_name, value = matched
            out[field_name] = [value]
    return out


def _day(iso: str) -> int:
    return date.fromisoformat(iso).toordinal() - EPOCH_ORDINAL

#: Cosine floor on the dense leg. Inherited from the engine, where it was tuned:
#: `min_score` may only ever be applied to the dense prefetch, never to the
#: fused output (invariant #8), or lexical matches sail past it.
#:
#: **The one retrieval knob `effort` deliberately does not scale**, and it stays
#: a module constant to say so. It is the topicality gate: a sweep on a real
#: index measured `min_score = 0.50` scoring best of everything tried and being
#: wrong, because that index's noise floor is 0.5153 — what a *wrong* chunk
#: scores. A level that lowered it would win its own metric by admitting exactly
#: what the floor was measured to exclude.
MIN_SCORE = 0.60

#: Chunks allowed from any one section. Measured in the engine: without a cap an
#: on-topic query spent 7 of 10 slots on near-identical chunks of one section —
#: fewer distinct sections than a nonsense query returned.
#:
#: **The second knob `effort` does not scale**, and for a different reason from
#: `MIN_SCORE`. It is not a volume control: `diversify` backfills in score order
#: when the cap leaves it short, so a wider `top_k` fills either way and raising
#: this would only buy back the near-duplicates the cap was measured to remove.
#: It also already has a claimant — a profile's `retrieval` block records a
#: measured, per-version value, and the recorded fix is for `search` to read it.
PER_SECTION = 2

#: The two knobs that *do* move with the level, at the default level.
#:
#: Derived rather than restated, so there is one definition and no pair to drift
#: apart. They keep their names because `scripts/probe_retrieval.py` imports them
#: from here and because they read as what they are at the point of use;
#: `effort.py` carries the measurement behind each.
CANDIDATE_LIMIT = BUDGETS[DEFAULT_EFFORT].candidate_limit
CLAIMS_PER_CHUNK = BUDGETS[DEFAULT_EFFORT].claims_per_chunk


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
    settings: Settings,
    provider: Provider,
    question: Question,
    plan: Plan,
    spend: "list | None" = None,
    supported: "list | None" = None,
) -> list[Evidence]:
    """Retrieve, expand, and return the evidence an answer may rest on.

    ``supported`` is a second out-parameter, for the same reason and by the same
    convention: it receives how many chunks cleared the *dense* floor, which is
    what `effort.effective_style_level` needs to tell a narrow question from a
    broad one. It costs nothing — the topicality gate was already making that
    search and throwing away everything but whether it was empty.

    ``spend`` is an out-parameter rather than a second return value, so the eight
    existing call sites are untouched. It exists because embedding the question
    is a real charge that nothing was recording: `Answer.spend` carried planning
    and answering and not this, so even a fixed ledger would have under-reported
    every question by the cost of its own query vector. Small — four orders of
    magnitude under the answering call — and a stage that spends without a row is
    exactly how the ledger came to be missing every question ever asked.
    """
    from docagent.qdrant import Excluded, Qdrant, SearchOpts, diversify

    # RETRIEVAL_QUERY, not RETRIEVAL_DOCUMENT. The model embeds questions and
    # passages asymmetrically on purpose (invariant #5) and using one task for
    # both measurably degrades retrieval.
    #
    # **Cached, and the reason is latency rather than money.** This one call is
    # the whole of `search` that can be slow: measured 2026-09-06 inside the
    # worker, everything else in this function — the dense probe, the hybrid
    # search, every graph read in `_expand` — totals **9 ms**, while ten
    # consecutive embeddings of one question took 0.4 s to **18.8 s**, one of
    # them logging a `provider_quota` retry and the slowest logging nothing at
    # all. The quota is a *per-minute* bucket, so a burst of questions makes
    # each one wait on the ones before it.
    #
    # The charge is four orders of magnitude under the answering call, which is
    # exactly why nothing had bothered: `ask-embedding` costs about $0.000004
    # and is nonetheless the dominant latency risk in retrieval. Cost said
    # nothing about that; a stage event did.
    #
    # `CachedEmbedder` rather than a second cache of its own — it is the same
    # class indexing uses, over the same `docagent.embedcache` keyed on
    # (model, width, task, text), reading the same directory. A question's
    # vector is content-addressed exactly like a chunk's, and asking the same
    # question twice should not roll the same dice twice.
    #
    # One consequence, stated because it is not nothing: the cache stores
    # float32, so a repeat question searches with a rounded vector where a first
    # ask searches with whatever the API returned. Qdrant stores and compares
    # float32 regardless, so the document side has always been rounded and the
    # ranking cannot move measurably — but the two asks are not bit-identical,
    # and anything comparing scores across them should know that.
    from ..providers import CachedEmbedder

    embedder = CachedEmbedder(
        provider,
        settings.paths.embed_cache,
        model=provider.settings.embedding_model,
        dimensions=provider.settings.embedding_dimensions,
        # One text, so concurrency has nothing to do here. `Provider.embed`
        # sends one request per text regardless — batching silently drops
        # (invariant on the collection), which is why throughput comes from
        # concurrency at all — and a pool for a single item is a thread nobody
        # needs.
        workers=1,
    )
    embedded = embedder.embed_many([question.text], task_type=RETRIEVAL_QUERY)[0]
    vector = embedded.values
    if spend is not None:
        from ..activities.ingest import price_for
        from ..pipeline import Spend

        # **From the embedder, not from the returned vector.** A cache hit hands
        # back an `Embedding` carrying the token count the *original* call cost,
        # which is the right thing for reporting what a vector was worth and the
        # wrong thing to bill: reading it here would book a charge on every
        # repeat question for tokens nobody spent, and the ledger would claim
        # money that was never taken. `embedder.usage` accumulates misses only,
        # so it is zero on a hit.
        tokens = embedder.usage.input_tokens
        # The row is written either way. A stage that ran for nothing and a
        # stage that did not run are different facts, and `cache_hits` exists on
        # the embedder for the same reason — "cheap because cached" and "cheap
        # because small" are not the same thing.
        spend.append(
            Spend(
                stage="ask-embedding",
                model=provider.settings.embedding_model,
                input_tokens=tokens,
                output_tokens=0,
                usd=price_for(provider.settings.embedding_model, tokens, 0),
            )
        )

    filters: dict[str, Any] = {
        k: v for k, v in question.filters.items() if k in ALLOWED_FILTERS
    }
    filters.update(narrowings(question))
    filters["library_id"] = question.library_id
    # Assigned after the allowlist, so a caller who found a way to smuggle the
    # key in still loses it here. Two guards for one property, because this is
    # the property.
    filters["tenant_id"] = question.tenant_id
    # A chunk somebody hid is hidden from *every* question, so this is scope
    # and not a narrowing a caller may request — the same argument
    # `ALLOWED_FILTERS` makes about `tenant_id`, and the same two guards: it is
    # absent from that allowlist and assigned here regardless.
    #
    # **An exclusion, never `enabled: true`.** A positive flag has to be
    # present on every point to mean anything, so adopting one would hide every
    # point written before it — and a corpus that vanishes from retrieval while
    # every log line reads as healthy is the worst failure this product has.
    # Absence means visible, which is what all 8,050 existing points say.
    filters["disabled"] = Excluded(True)

    # Resolved once, here, and passed down. Every number below moves with the
    # level except `MIN_SCORE`, which is the floor and is deliberately fixed.
    budget = budget_for(question.effort)
    top_k = resolve_top_k(question.top_k, budget)

    with Qdrant(settings.qdrant_url, settings.qdrant_collection) as q:
        # The gate, run directly rather than through `topicality_gate`, which
        # builds its own `SearchOpts(limit=1, …)` and answers only "was it
        # empty". The question is identical — dense only, same floor, same
        # filters, one round trip and no tokens (invariant #9) — and asking for
        # the whole width instead of one row also answers "how much of this
        # corpus actually clears the floor", which is the only signal that can
        # tell a narrow question from a broad one. Every on-corpus question
        # fills `top_k` after RRF fusion, however narrow, because the fused
        # output carries no floor (invariant #8); this is what does not.
        #
        # `min_score` stays on the constant. Whether a question is about this
        # corpus is a fact about the corpus, not about how hard the asker looked.
        probe = q.search(
            vector,
            SearchOpts(
                limit=top_k, min_score=MIN_SCORE, dense_only=True,
                query_text=question.text, filters=filters,
            ),
        )
        on_topic = bool(probe)
        if supported is not None:
            supported.append(len(probe))

        hits = q.search(
            vector,
            SearchOpts(
                limit=budget.candidate_limit, min_score=MIN_SCORE,
                query_text=question.text, filters=filters,
                prefetch_limit=budget.prefetch_limit,
            ),
        )

    # The cross-encoder sits exactly here: after RRF has decided *which*
    # `candidate_limit` chunks are in play and before `diversify` decides which
    # `top_k` of them the model reads. Measured before it was built (see
    # `providers/ranking.py`): +0.094 recall@4 and +0.066 recall@8 on 640
    # questions, no book worse, and nothing at `thorough`, which is why the
    # level decides. What it reorders is the fused list and only the order —
    # a chunk RRF never reached is still unreachable, which is what keeps
    # `topicality_gate`'s verdict and `off_corpus` meaning what they mean.
    #
    # A ranking failure is not a retrieval failure. The fused order is what
    # the product served for months; losing 0.07 of recall on one question
    # is a worse trade than refusing it, so the error is logged, the charge is
    # not booked (nothing was billed), and the question proceeds.
    #
    # `on_topic` first: an off-corpus question is refused three lines down
    # with its nearest fragments as examples, and reordering examples is a
    # billed call for nothing — the first live question after this shipped
    # was exactly that, $0.001 on a refusal.
    if on_topic and budget.rerank and provider.settings.rerank_model and hits:
        hits = _rerank(provider, question, hits, spend)

    # `PER_SECTION` is a diversity policy rather than a volume one and stays off
    # the ladder — `diversify` backfills in score order when the cap leaves it
    # short, so a wider `top_k` is filled either way. See `effort.py`.
    ranked = diversify(hits, PER_SECTION, top_k)
    evidence = [_evidence(h.payload, h.score, "vector") for h in ranked]

    if not on_topic:
        # Nearby results are still shown — "nothing found" with no examples is
        # indistinguishable from a broken index.
        raise OffCorpus(nearby=evidence[:3])

    return _expand(settings, question, plan, evidence, budget, top_k)


def _rerank(provider: Provider, question: Question, hits: list, spend: "list | None") -> list:
    """Reorder the fused candidates by the ranking model's score.

    The reranker reads `text`, never `embed_text`, and its score replaces the
    *order* — the RRF score on each hit is left as it was, because a reciprocal
    rank and a relevance probability are not the same number and
    `Evidence.score` has always carried the former.
    """
    from ..providers import ProviderError
    from ..providers.ranking import usd_for

    texts = [h.payload.get("text") or "" for h in hits]
    try:
        ranked = provider.rank(question.text, texts)
    except ProviderError as e:
        log.warning("reranking skipped, fused order kept (%s): %s", e.kind, e)
        return hits
    order = sorted(range(len(hits)), key=lambda i: -ranked.scores[i])
    if spend is not None:
        from ..pipeline import Spend

        # No tokens: this API bills per query of up to a hundred records, so
        # the token columns would be a lie in either direction. The dollar
        # figure is the list price, sourced in `providers/ranking.py`.
        spend.append(
            Spend(
                stage="ask-rerank",
                model=provider.settings.rerank_model,
                input_tokens=0,
                output_tokens=0,
                usd=usd_for(ranked.records),
            )
        )
    return [hits[i] for i in order]


def _expand(
    settings: Settings,
    question: Question,
    plan: Plan,
    evidence: list[Evidence],
    budget: Budget,
    top_k: int,
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
                added += _by_concept(graph, question, plan, found, top_k)
            if plan.template_id is not None:
                added += _by_template(graph, plan, found, question)
            # Graph hits go after vector hits: the vector score is a similarity
            # to the actual question, while a graph hit is only topically
            # adjacent. Truncated before the two lookups below rather than after,
            # so neither pays for evidence that will not reach the prompt.
            kept = evidence + added[: max(0, top_k - len(evidence))]
            _attach_citations(graph, kept, question.tenant_id)
            _attach_claims(
                graph,
                kept,
                question.confidence_floor,
                question.tenant_id,
                budget.claims_per_chunk,
            )
    except GraphError as e:
        log.warning("graph expansion unavailable, answering from vectors: %s", e)
        return evidence

    return kept


def _by_concept(
    graph: Graph, question: Question, plan: Plan, found: set[str], top_k: int
) -> list[Evidence]:
    rows = graph.query(
        "concept_by_name",
        {
            "canonical_names": [canonical_concept(c) for c in plan.concepts],
            "tenant_id": question.tenant_id,
        },
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
            "tenant_id": question.tenant_id,
            "confidence_floor": question.confidence_floor,
            "limit": top_k,
        },
    )
    return _hydrate(
        graph,
        [r["id"] for r in chunks if r["id"] not in found],
        "graph",
        question.library_id,
        question.tenant_id,
    )


def _by_template(graph: Graph, plan: Plan, found: set[str], question: Question) -> list[Evidence]:
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
    return _hydrate(graph, ids, "graph", question.library_id, question.tenant_id)


def _hydrate(
    graph: Graph, chunk_ids: list[str], source: str, library_id: str, tenant_id: str
) -> list[Evidence]:
    """Fetch the text of chunks the graph named, within one library.

    The graph stores chunk text as well as Qdrant does, deliberately: an answer
    must be assemblable from the graph's own citation path even when a vector
    index has been rebuilt and its point ids have moved.

    **This is the choke point for the library and the organisation scope, and it
    is deliberately belt-and-braces.** `chunks_for_concepts` already filters, and
    the registry now refuses to load a template that does not, but every path
    that turns a graph result into `Evidence` goes through here — including
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
          AND d.tenant_id = $tenant_id
        RETURN c.id AS chunk_id, c.text AS text, c.kind AS kind,
               c.page AS page, v.id AS version_id, v.title AS title,
               d.id AS document_id
        """,
        # A required match, where this used to be OPTIONAL. A chunk with no
        # owning Document cannot be attributed to a library or shown a source,
        # and dropping it is the safe direction: an answer is not allowed to
        # cite something it cannot name the origin of.
        {"ids": chunk_ids, "library_id": library_id, "tenant_id": tenant_id},
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


def _attach_citations(graph: Graph, evidence: list[Evidence], tenant_id: str) -> None:
    """Fill in each chunk's verifiable locator.

    Done in one query rather than per chunk: the answer step refuses any
    citation whose chunk has no locator, so a per-chunk round trip would put the
    graph on the critical path once per result.
    """
    ids = [e.chunk_id for e in evidence if e.chunk_id]
    if not ids:
        return
    rows = graph.query(
        "citations_for_chunks",
        {"chunk_ids": ids, "tenant_id": tenant_id, "limit": len(ids)},
    )
    by_chunk = {r["chunk_id"]: r for r in rows}
    for e in evidence:
        if (row := by_chunk.get(e.chunk_id)) is not None:
            e.locator = row["locator"] or ""
            e.page = row["page"] if row["page"] is not None else e.page
            if row["section_title"]:
                e.breadcrumb = e.breadcrumb or row["section_title"]


def _attach_claims(
    graph: Graph,
    evidence: list[Evidence],
    floor: float,
    tenant_id: str,
    per_chunk: int = CLAIMS_PER_CHUNK,
) -> None:
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
            "tenant_id": tenant_id,
            "confidence_floor": floor,
            "limit": len(ids) * per_chunk,
        },
    )
    by_chunk: dict[str, list[EvidenceClaim]] = {}
    for r in rows:
        # The row limit is global and the ordering is by confidence, so one
        # heavily annotated chunk could otherwise spend the whole budget.
        bucket = by_chunk.setdefault(r["chunk_id"], [])
        if len(bucket) >= per_chunk:
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
