"""The composition graph: one target chapter, researched, written and checked.

`research → draft → verify → (revise | settle)`, run once per chapter inside its
own Temporal activity. One activity per chapter rather than one for the whole
work, and that is the design's most important decision: the recorded failure is
a single `async def` activity that held the worker's loop for 45 minutes and was
then retried in full, **$9.4539 of a $10.017265 run spent twice**. A chapter is
the unit that retries, the unit that charges, and the unit a person can see move.

Sequential, never fanned out. Each chapter is written against what the previous
ones actually wrote, which a parallel fan-out cannot see — and the drift that
produces is measured in this repository from the other direction: **207 of
12,196 concepts carry a display name that is not the majority spelling**, because
independent passes over one corpus disagree with themselves about how to say a
word. A forty-chapter work written in forty independent calls is that failure at
full scale, and unlike a concept label nobody can repair it without re-paying.

**`MAX_REVISIONS = 1`, against planning's `MAX_REFINE = 3`.** The asymmetry is
the engine's own: a planning call is bounded and small, and a composition call is
the bill.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from ..answering import answer as answer_mod
from ..answering.types import Evidence
from ..providers import Provider
from ..providers.adapter import TruncatedResponse
from . import research as research_mod
from .calling import MAX_OUTPUT_TOKENS, generate_json
from .genres import Genre
from .rules import compose_transform_system
from .types import (
    MAX_ESTABLISHED,
    TAIL_CHARS,
    ChapterPlan,
    CitedSource,
    ComposedChapter,
    Continuity,
    TransformPlan,
)

log = logging.getLogger(__name__)

STAGE_COMPOSE = "transform-compose"

#: One revision, at most. A second is a third full-price call for a chapter that
#: has already had two chances, and the evidence does not improve between them.
MAX_REVISIONS = 1

#: How much of a chapter has to be dropped for verification before it is worth
#: paying to write again. Below this the chapter stands with its losses recorded;
#: above it, the draft rested so heavily on citations that did not survive that
#: what is left is unlikely to read as a chapter at all.
REVISE_THRESHOLD = 0.34

#: How much of each evidence chunk reaches the prompt. The same order as the
#: answering path gives a chunk, because it is the same kind of reading.
EVIDENCE_CHARS = 1_600

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "paragraphs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    # Empty means "this paragraph rests on the source document
                    # alone", which is the ordinary case and must stay cheap to
                    # say. A paragraph resting on the library names its
                    # fragments here, and `verify` is what holds it to that.
                    "chunk_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "chunk_ids"],
            },
        },
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "form": {"type": "string"},
                },
                "required": ["term", "form"],
            },
        },
        "threads": {"type": "array", "items": {"type": "string"}},
        # Last, deliberately, and for the reason `answer.SCHEMA` and
        # `synthesis.SCHEMA` both put theirs last: nothing can verify an
        # envelope before it closes, and a schema that offered the citations
        # first would invite something to try.
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string"},
                    "supports": {"type": "string"},
                },
                "required": ["chunk_id", "supports"],
            },
        },
    },
    "required": ["title", "paragraphs", "citations"],
}


def _last(a, b):
    return b if b is not None else a


def _add(a, b):
    return (a or []) + (b or [])


def _sum(a, b):
    return int(a or 0) + int(b or 0)


class ChapterState(TypedDict, total=False):
    evidence: Annotated[list, _last]
    queries_made: Annotated[int, _last]
    off_corpus: Annotated[int, _last]
    supported: Annotated[int, _last]
    raw: Annotated[object, _last]
    body: Annotated[str, _last]
    title: Annotated[str, _last]
    cited: Annotated[list, _last]
    #: Accumulated across drafts, not replaced by the last one. A chapter whose
    #: first draft invented three citations and whose second did not still had a
    #: model invent three citations, and the run's report is the only place that
    #: can say so. `_last` here would report a corrected run as a clean one,
    #: which is the same shape as an unmeasured count rendering as a zero.
    removed: Annotated[list, _add]
    invented: Annotated[int, _sum]
    #: What the *latest* verify dropped and kept, which is what the routing
    #: decision is about. Separate from the accumulators above precisely so that
    #: re-reading them on a second pass cannot re-trigger a revision.
    dropped_now: Annotated[int, _last]
    kept_now: Annotated[int, _last]
    revisions: Annotated[int, _last]
    feedback: Annotated[list, _last]
    terms: Annotated[list, _last]
    threads: Annotated[list, _last]
    chapter: Annotated[object, _last]
    continuity: Annotated[object, _last]
    spend: Annotated[list, _add]
    log: Annotated[list, _add]


@dataclass
class ChapterDeps:
    """What one chapter is written from. Read by every node, changed by none."""

    settings: object
    provider: Provider
    genre: Genre
    mode: str
    plan: TransformPlan
    chapter: ChapterPlan
    source_text: str
    incoming: Continuity
    purposes: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    allowance: int = 0
    library_id: str = ""
    tenant_id: str = ""
    #: The source document's own version, excluded from its own research so a
    #: work cannot cite itself as an outside corroboration.
    version_id: str = ""


async def n_research(state: ChapterState, deps: ChapterDeps) -> dict:
    """Ask the library, within the allowance this chapter was handed.

    The allowance is an input and `queries_made` is an output. That is what makes
    a retry safe: a second attempt replays with the identical number, and the
    workflow subtracts only what an attempt that *succeeded* reported.
    """
    if deps.allowance <= 0 or not deps.purposes:
        return {
            "evidence": [],
            "queries_made": 0,
            "log": ["research: none (no allowance or no purposes)"],
        }
    found = await asyncio.to_thread(
        research_mod.gather,
        deps.settings,
        deps.provider,
        library_id=deps.library_id,
        tenant_id=deps.tenant_id,
        exclude_version_id=deps.version_id,
        purposes=deps.purposes,
        title=deps.chapter.title,
        intent=deps.chapter.intent,
        source_text=deps.source_text,
        allowance=deps.allowance,
    )
    return {
        "evidence": found.evidence,
        "queries_made": found.queries_made,
        "off_corpus": found.off_corpus,
        "supported": found.supported,
        "spend": list(found.spend),
        "log": [
            f"research: {found.queries_made} quer(ies), "
            f"{len(found.evidence)} fragment(s), {found.off_corpus} off-corpus"
        ],
    }


def _outline_block(deps: ChapterDeps) -> str:
    rows = []
    for chapter in deps.plan.chapters:
        rows.append(
            {
                "n": chapter.ordinal,
                "title": chapter.title,
                "intent": chapter.intent,
                "this_one": chapter.ordinal == deps.chapter.ordinal,
            }
        )
    return json.dumps(rows, ensure_ascii=False)


def _continuity_block(deps: ChapterDeps) -> str:
    carried = deps.incoming
    return json.dumps(
        {
            "terms_already_used": carried.glossary,
            "already_established": carried.established,
            "threads_left_open": carried.threads,
        },
        ensure_ascii=False,
    )


def _evidence_block(evidence: list[Evidence]) -> str:
    return json.dumps(
        [
            {
                "chunk_id": e.chunk_id,
                "work": e.title,
                "where": e.breadcrumb or e.locator,
                "text": e.text[:EVIDENCE_CHARS],
            }
            for e in evidence
        ],
        ensure_ascii=False,
    )


def _references_for(deps: ChapterDeps) -> list[str]:
    """The source's own references that appear in *this* chapter's material.

    A substring test over the chapter's own source text, which is crude and is
    the right kind of crude: a reference reaches the chapter that contains it,
    and every reference reaches the closing bibliography regardless, so a miss
    here costs a footnote its place in the prose and never its existence.
    """
    return [ref for ref in deps.references if ref and ref in deps.source_text][:40]


def _draft_prompt(state: ChapterState, deps: ChapterDeps) -> str:
    parts = [
        f"Write chapter {deps.chapter.ordinal} of the work, titled "
        f"{deps.chapter.title!r}.",
        f"What this chapter is for: {deps.chapter.intent or '(not stated)'}",
        "",
        "The whole outline, so you know what comes before and after and do not "
        "repeat it. Write only the chapter marked `this_one`:",
        _outline_block(deps),
        "",
        "What the work has already established, and how it has been saying "
        "things. Keep the same words for the same things:",
        _continuity_block(deps),
    ]
    if deps.incoming.tail:
        parts += [
            "",
            "The last words of the previous chapter, so this one continues from "
            "them rather than restarting:",
            deps.incoming.tail,
        ]
    parts += [
        "",
        "THE SOURCE MATERIAL for this chapter. This is the substance; everything "
        "you write must rest on it, or on a library fragment you cite:",
        deps.source_text,
    ]
    references = _references_for(deps)
    if references:
        parts += [
            "",
            "References the source itself carries in this material. Reproduce "
            "any you use exactly as they appear, and never alter one:",
            json.dumps(references, ensure_ascii=False),
        ]
    evidence = list(state.get("evidence") or [])
    if evidence:
        parts += [
            "",
            "Fragments from other works in the same library. Supporting "
            "material, not a higher authority than the source. A paragraph "
            "resting on one names its `chunk_ids`, and every fragment you use "
            "appears in `citations`:",
            _evidence_block(evidence),
        ]
    feedback = list(state.get("feedback") or [])
    if feedback:
        parts = [
            "Your previous draft of this chapter had paragraphs that rested on "
            "fragments you were never shown, and they were removed. Write it "
            "again, and this time:",
            *(f"- {line}" for line in feedback),
            "",
            *parts,
        ]
    return "\n".join(parts)


async def n_draft(state: ChapterState, deps: ChapterDeps) -> dict:
    """One chapter of prose, in the genre, under the invariants.

    A `TruncatedResponse` is reported as what it is — the model ran out of room
    — and never as thin material. Those have opposite remedies, and the recorded
    incident is an answering call that billed **65,521 output tokens and
    $0.497373** while writing not one character, reported as "not enough
    evidence", which is a claim about the corpus the run had no basis for.
    """
    try:
        raw, spend = await generate_json(
            deps.provider,
            _draft_prompt(state, deps),
            system=compose_transform_system(deps.genre, deps.mode, deps.purposes),
            schema=SCHEMA,
            stage=STAGE_COMPOSE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    except TruncatedResponse as e:
        raise TruncatedResponse(
            f"the model ran out of output room writing chapter "
            f"{deps.chapter.ordinal} ({e.output_tokens} output tokens, no "
            "closed envelope): the chapter's source material is too long for "
            "one call and the plan should divide it",
            output_tokens=e.output_tokens,
        ) from e
    return {
        "raw": raw,
        "spend": [spend],
        "log": [f"draft {int(state.get('revisions') or 0) + 1}: written"],
    }


async def n_verify(state: ChapterState, deps: ChapterDeps) -> dict:
    """Keep only what a citation supports, and record precisely what was dropped.

    `answer._verify` is **reused, never reimplemented** — it is what checks that
    a cited chunk was retrieved *and* has a locator, which is what makes a
    citation openable. Its input keys are Spanish because every other caller's
    schema is; this schema is English because these prompts are, so the two rows
    are mapped here rather than either side being bent.

    A paragraph naming `chunk_ids` of which none survived is **removed from the
    prose and kept in the run's report**. Removed, because unlike a synthesis a
    novel has nowhere to demote a claim to; kept, because a dropped claim that
    leaves the work quietly shorter is its own failure, and a reader of the
    report can see exactly what the model wanted to say and could not support.
    """
    raw = state.get("raw")
    if not isinstance(raw, dict):
        return {
            "body": "",
            "cited": [],
            "dropped_now": 0,
            "kept_now": 0,
            "log": ["verify: nothing to check"],
        }

    evidence: list[Evidence] = list(state.get("evidence") or [])
    claimed = [
        {"chunk_id": str(row.get("chunk_id") or ""),
         "afirmacion": str(row.get("supports") or "")}
        for row in (raw.get("citations") or [])
        if isinstance(row, dict)
    ]
    citations, invented = answer_mod._verify(claimed, evidence)
    kept_ids = {c.chunk_id for c in citations}

    body: list[str] = []
    removed: list[str] = []
    used: set[str] = set()
    for row in raw.get("paragraphs") or []:
        if not isinstance(row, dict):
            continue
        # `_clean` strips any chunk id the model wrote into the prose. The
        # prompt forbids it and mostly complies, and "mostly" is not a property
        # a reader-facing string can rest on.
        text = answer_mod._clean(str(row.get("text") or ""))
        if not text:
            continue
        ids = [str(i) for i in (row.get("chunk_ids") or []) if str(i)]
        if ids and not (set(ids) & kept_ids):
            removed.append(text)
            continue
        used.update(set(ids) & kept_ids)
        body.append(text)

    # Only the citations a surviving paragraph actually rests on reach the
    # bibliography. A citation the model listed for a paragraph that was then
    # removed describes nothing in the finished work, and printing it would put
    # a source in the closing chapter that the work never uses.
    sources: list[CitedSource] = research_mod.sources_of(
        [c for c in citations if c.chunk_id in used], evidence
    )

    title = str(raw.get("title") or "").strip() or deps.chapter.title
    return {
        "body": "\n\n".join(body).strip(),
        "title": title,
        "cited": sources,
        "removed": removed,
        "invented": len(invented),
        "dropped_now": len(removed),
        "kept_now": len(body),
        "terms": [r for r in (raw.get("terms") or []) if isinstance(r, dict)],
        "threads": [str(t).strip() for t in (raw.get("threads") or []) if str(t).strip()],
        "log": [
            f"verify: {len(body)} paragraph(s) kept, {len(removed)} removed, "
            f"{len(sources)} citation(s), {len(invented)} invented"
        ],
    }


def e_verify(state: ChapterState) -> Literal["revise", "settle"]:
    removed = int(state.get("dropped_now") or 0)
    kept = int(state.get("kept_now") or 0)
    total = removed + kept
    if not total or int(state.get("revisions") or 0) >= MAX_REVISIONS:
        return "settle"
    if removed / total > REVISE_THRESHOLD:
        return "revise"
    return "settle"


async def n_revise(state: ChapterState, deps: ChapterDeps) -> dict:
    """Ask again, naming what was dropped. Once."""
    removed = list(state.get("removed") or [])[-8:]
    feedback = [
        "Cite only fragments that appear in the list you were given, by their "
        "exact `chunk_id`.",
        "A paragraph that rests on the source document alone needs no "
        "`chunk_ids` at all — leave the list empty rather than inventing one.",
    ]
    if removed:
        feedback.append(
            "These paragraphs were removed because nothing you cited for them "
            f"checked out: {json.dumps(removed[:4], ensure_ascii=False)}"
        )
    return {
        "revisions": int(state.get("revisions") or 0) + 1,
        "feedback": feedback,
        "log": [f"revise: {len(removed)} paragraph(s) had no surviving citation"],
    }


async def n_settle(state: ChapterState, deps: ChapterDeps) -> dict:
    """Build the chapter and what it hands the next one.

    The glossary keeps its **first** rendering of a term rather than its latest,
    which is the opposite of the usual "newest wins" and is right here: the point
    of a glossary is that a term does not drift, so the entry to protect is the
    one already on the page. It is also defensible where the graph's own
    first-writer-wins is not — chapters are written in order, so the first writer
    of a term is the place in the work where the term is introduced, while
    `_MERGE_CONCEPTS` takes whichever document happened to project first.
    """
    body = str(state.get("body") or "")
    sources: list[CitedSource] = list(state.get("cited") or [])

    glossary = dict(deps.incoming.glossary)
    for row in state.get("terms") or []:
        term = str(row.get("term") or "").strip()
        form = str(row.get("form") or "").strip()
        if term and form and term not in glossary:
            glossary[term] = form

    established = list(deps.incoming.established)
    established.append(
        f"{deps.chapter.ordinal}. {deps.chapter.title}"
        + (f" — {deps.chapter.intent}" if deps.chapter.intent else "")
    )

    continuity = Continuity(
        glossary=glossary,
        established=established[-MAX_ESTABLISHED:],
        threads=list(state.get("threads") or []),
        tail=body[-TAIL_CHARS:],
        cited=list(deps.incoming.cited) + [s.chunk_id for s in sources],
    ).trimmed()

    chapter = ComposedChapter(
        ordinal=deps.chapter.ordinal,
        title=str(state.get("title") or deps.chapter.title),
        body=body,
        cited=sources,
    )
    return {
        "chapter": chapter,
        "continuity": continuity,
        "log": [f"settle: {len(body)} characters"],
    }


def build_chapter_graph(deps: ChapterDeps):
    def bind(fn):
        async def node(state: ChapterState) -> dict:
            return await fn(state, deps)

        node.__name__ = fn.__name__
        return node

    g: StateGraph = StateGraph(ChapterState)
    g.add_node("research", bind(n_research))
    g.add_node("draft", bind(n_draft))
    g.add_node("verify", bind(n_verify))
    g.add_node("revise", bind(n_revise))
    g.add_node("settle", bind(n_settle))

    g.add_edge(START, "research")
    g.add_edge("research", "draft")
    g.add_edge("draft", "verify")
    g.add_conditional_edges("verify", e_verify, ["revise", "settle"])
    g.add_edge("revise", "draft")
    g.add_edge("settle", END)

    return g.compile()


@dataclass
class Composed:
    """What one run of the graph produced, flattened for the activity."""

    chapter: ComposedChapter
    continuity: Continuity
    queries_made: int = 0
    off_corpus: int = 0
    removed: list[str] = field(default_factory=list)
    invented: int = 0
    revisions: int = 0
    spend: list = field(default_factory=list)


async def compose(deps: ChapterDeps, on_node=None) -> Composed:
    """Run the graph for one chapter.

    ``on_node`` is called with each node's name as it completes, and exists so
    the activity can heartbeat with something a person can read. It is a
    callback rather than a stream mode because the activity is not streaming to
    anybody — it is telling Temporal it is alive.
    """
    graph = build_chapter_graph(deps)
    state: ChapterState = {"revisions": 0, "spend": [], "log": []}

    # Two stream modes at once. `updates` names the node that just finished, for
    # the heartbeat; `values` carries the state **with the reducers applied**,
    # which is the only reading that may be kept. Merging the `updates` deltas by
    # hand would bypass `_add` and leave `spend` holding the last node's rows
    # instead of every node's — a ledger that under-reports with nothing failing
    # anywhere, which is precisely the class of defect this feature is built to
    # avoid producing.
    async for mode, chunk in graph.astream(
        dict(state), stream_mode=["updates", "values"]
    ):
        if mode == "updates":
            if on_node is not None:
                for name in (chunk or {}):
                    on_node(name)
        elif isinstance(chunk, dict):
            state = chunk

    chapter = state.get("chapter")
    continuity = state.get("continuity")
    if not isinstance(chapter, ComposedChapter) or not isinstance(
        continuity, Continuity
    ):
        raise RuntimeError(
            f"the composition graph settled nothing for chapter "
            f"{deps.chapter.ordinal}"
        )
    for line in state.get("log") or []:
        log.info("chapter %d: %s", deps.chapter.ordinal, line)
    return Composed(
        chapter=chapter,
        continuity=continuity,
        queries_made=int(state.get("queries_made") or 0),
        off_corpus=int(state.get("off_corpus") or 0),
        removed=list(state.get("removed") or []),
        invented=int(state.get("invented") or 0),
        revisions=int(state.get("revisions") or 0),
        spend=list(state.get("spend") or []),
    )
