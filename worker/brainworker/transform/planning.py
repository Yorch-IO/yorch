"""The planning graph: what genre is this, and what shall the new work look like.

A LangGraph `StateGraph`, and it is the same shape the engine's own graph uses
for the same job — `docagent/graph.py`'s propose → validate → (adopt | refine |
fallback) loop, which exists because rule learning has exactly this structure: a
model proposes, deterministic code judges, and after a bounded number of tries
the built-in answer is adopted rather than the work being refused.

**It runs entirely inside one Temporal activity and never crosses that
boundary.** No graph object, no state dict and no checkpointer is a workflow's
business, and `workflows/transform.py` does not import this module. That is the
same separation `evaluate.py` records — a function was moved there *"so the
Temporal worker can pick a candidate without importing this module, which would
pull in LangGraph and the whole node set for one function."*

**No checkpointer.** The engine's CLI compiles with `SqliteSaver` because it has
no other durability; here Temporal is that, at a granularity that already
matches the bill. A second durable record of one run's progress, which Temporal
does not know about, is the shape this codebase refuses elsewhere — and its
failure mode would be resuming from a step Temporal thinks still has to run.

**Every node is `async` and every call goes through `asyncio.to_thread`.** Not a
precaution: the recorded cost of a synchronous network call inside an `async
def` activity was a worker frozen for 97 minutes and a stage billed twice.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from ..providers import Provider
from . import outline
from .budget import budget_for
from .calling import PLAN_MAX_OUTPUT_TOKENS, generate_json
from .genres import Genre
from .reading import Passage
from .rules import compose_transform_system
from .types import ChapterPlan, SourceChapter, TransformPlan

log = logging.getLogger(__name__)

#: How many proposals may be refined before the source's own chapters are
#: adopted. Three, the same as `docagent/graph.py`'s `MAX_REFINE`, and for the
#: same reason: each attempt is a paid call and a fourth has never been observed
#: to succeed where three failed.
MAX_REFINE = 3

STAGE_GENRE = "transform-genre"
STAGE_PLAN = "transform-plan"

DETECT_SYSTEM = """\
You are identifying what literary genre a document already belongs to, so that \
it can be recast into a different one.

Answer with the genre the document is, not one from any list: it may be a \
manual, a transcript, minutes, a textbook, a report, a set of letters, a \
reference work, a sermon, a novel, or something with no settled name, in which \
case describe it in a few words. Judge from how the document is organised, whom \
it addresses and what it is trying to do — not from its subject matter, which \
tells you nothing about genre.

Give a confidence between 0 and 1. A document that is plainly two things at once \
gets a low one, and that is a useful answer.

Say nothing about the subject matter itself."""

DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "genre": {"type": "string"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["genre", "confidence"],
}

PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "intent": {"type": "string"},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "first": {"type": "integer"},
                                "last": {"type": "integer"},
                            },
                            "required": ["first", "last"],
                        },
                    },
                },
                "required": ["title", "intent", "sources"],
            },
        },
    },
    "required": ["title", "chapters"],
}


def _last(a, b):
    return b if b is not None else a


def _add(a, b):
    return (a or []) + (b or [])


class PlanState(TypedDict, total=False):
    """What the loop carries. Inputs live on `PlanDeps` and never change."""

    source_genre: Annotated[str, _last]
    source_genre_confidence: Annotated[float, _last]
    title: Annotated[str, _last]
    proposal: Annotated[object, _last]
    chapters: Annotated[list, _last]
    complaints: Annotated[list, _last]
    attempts: Annotated[int, _last]
    fallback: Annotated[bool, _last]
    plan: Annotated[object, _last]
    spend: Annotated[list, _add]
    log: Annotated[list, _add]


@dataclass
class PlanDeps:
    """Everything the nodes read and none of them change.

    On a `Deps` object rather than in the state, which is where
    `docagent/graph.py` puts its inputs. The difference is deliberate: that
    graph's inputs are scalars a checkpoint can carry, and these include the
    whole document's passages. Keeping them out of the state keeps the state
    small enough to log, and makes it obvious that a node cannot rewrite the
    source under a later node's feet.
    """

    provider: Provider
    genre: Genre
    mode: str
    purposes: list[str] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    source_chapters: list[SourceChapter] = field(default_factory=list)
    excerpt: str = ""
    document_title: str = ""
    target_chapters: int = 1
    max_chapters: int = 1
    supported: int = 0


async def n_detect(state: PlanState, deps: PlanDeps) -> dict:
    """What the source already is. Recorded, and never printed.

    The brief asks that the analysis be performed silently, and it is: this
    reaches the plan artifact and the run trail, and no gate report and no page.
    What it is *for* is the proposal below, which writes a better outline for
    knowing whether it is recasting a transcript or a treatise.

    A failure is not fatal. An unknown source genre costs the proposal one piece
    of context; refusing to transform a document because a classification call
    failed would be the expensive answer to a cheap problem.
    """
    if not deps.excerpt.strip():
        return {"source_genre": "", "log": ["detect: nothing to read"]}
    try:
        raw, spend = await generate_json(
            deps.provider,
            deps.excerpt,
            system=DETECT_SYSTEM,
            schema=DETECT_SCHEMA,
            stage=STAGE_GENRE,
            max_output_tokens=PLAN_MAX_OUTPUT_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 — a plan beats no plan
        log.warning("could not detect the source genre: %s", e)
        return {"source_genre": "", "log": [f"detect failed: {e}"]}

    genre = str((raw or {}).get("genre") or "").strip() if isinstance(raw, dict) else ""
    try:
        confidence = float((raw or {}).get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "source_genre": genre,
        "source_genre_confidence": max(0.0, min(1.0, confidence)),
        "spend": [spend],
        "log": [f"detect: {genre!r} at {confidence:.2f}"],
    }


def _source_table(deps: PlanDeps) -> str:
    return json.dumps(
        [
            {
                "first": c.first,
                "last": c.last,
                "characters": c.chars,
                "title": c.title,
            }
            for c in deps.source_chapters
        ],
        ensure_ascii=False,
    )


def _propose_prompt(state: PlanState, deps: PlanDeps) -> str:
    parts = [
        deps.genre.outline_hint.strip(),
        "",
        f"Propose at most {deps.max_chapters} chapters. About "
        f"{deps.target_chapters} is what this work was quoted for.",
        "",
        "The source document's own divisions, as passage index ranges. Every "
        "chapter you propose must name the passages it is made from, using "
        "these indices:",
        _source_table(deps),
        "",
        "The opening of the source document:",
        deps.excerpt,
    ]
    if state.get("source_genre"):
        parts.insert(
            1,
            f"\nThe source document appears to be: {state['source_genre']}. "
            "Recast it; do not summarise it.",
        )
    complaints = state.get("complaints") or []
    if complaints:
        parts = [
            "Your previous outline was refused. Fix every one of these and "
            "propose again:",
            *(f"- {c}" for c in complaints),
            "",
            *parts,
        ]
    return "\n".join(parts)


async def n_propose(state: PlanState, deps: PlanDeps) -> dict:
    """One outline, or none. A refused call routes to the fallback, not to a crash.

    The genre's own system prompt is composed above this, through
    `compose_transform_system`, so an outline is planned under the same
    invariants the prose will be written under — including rule 4, which is why
    a chapter title comes back in the source document's language.
    """
    attempts = int(state.get("attempts") or 0) + 1
    try:
        raw, spend = await generate_json(
            deps.provider,
            _propose_prompt(state, deps),
            system=compose_transform_system(deps.genre, deps.mode, deps.purposes),
            schema=PROPOSE_SCHEMA,
            stage=STAGE_PLAN,
            max_output_tokens=PLAN_MAX_OUTPUT_TOKENS,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("outline proposal %d failed: %s", attempts, e)
        return {
            "attempts": MAX_REFINE,
            "proposal": None,
            "chapters": [],
            "complaints": [f"the proposal could not be read: {e}"],
            "log": [f"propose {attempts}: failed ({e})"],
        }

    title = ""
    if isinstance(raw, dict):
        title = str(raw.get("title") or "").strip()
    chapters = outline.chapters_from(raw, len(deps.passages))
    return {
        "attempts": attempts,
        "proposal": raw,
        "title": title,
        "chapters": chapters,
        "spend": [spend],
        "log": [f"propose {attempts}: {len(chapters)} chapters"],
    }


async def n_validate(state: PlanState, deps: PlanDeps) -> dict:
    """Deterministic, free, and the only thing holding the model to the document."""
    chapters: list[ChapterPlan] = list(state.get("chapters") or [])
    complaints = outline.validate(
        chapters,
        genre=deps.genre,
        mode=deps.mode,
        chunk_count=len(deps.passages),
        max_chapters=deps.max_chapters,
        passages=deps.passages,
    )
    return {
        "complaints": complaints,
        "log": [f"validate: {len(complaints)} complaint(s)"],
    }


def e_validation(state: PlanState) -> Literal["adopt", "propose", "fallback"]:
    if not state.get("complaints"):
        return "adopt"
    if int(state.get("attempts") or 0) < MAX_REFINE:
        return "propose"
    return "fallback"


async def n_adopt(state: PlanState, deps: PlanDeps) -> dict:
    return {"fallback": False, "log": ["adopt: the proposal validated"]}


async def n_fallback(state: PlanState, deps: PlanDeps) -> dict:
    """The source's own chapters, when no proposal validated.

    Covers the document by construction, which is the property that matters
    most, and the genre still governs every sentence written against it. The run
    records `fallback: true` so the outcome is legible rather than merely
    survivable.
    """
    chapters = outline.fallback(
        deps.source_chapters, deps.passages, max_chapters=deps.max_chapters
    )
    return {
        "chapters": chapters,
        "fallback": True,
        "title": state.get("title") or deps.document_title,
        "log": [f"fallback: {len(chapters)} chapters from the source's own"],
    }


async def n_budget(state: PlanState, deps: PlanDeps) -> dict:
    """Freeze the plan, and turn the probe's measurement into a query allowance."""
    chapters: list[ChapterPlan] = list(state.get("chapters") or [])
    outline.measure(chapters, deps.passages)
    for ordinal, chapter in enumerate(chapters, start=1):
        chapter.ordinal = ordinal
    uncovered, fraction = outline.coverage(chapters, len(deps.passages))
    plan = TransformPlan(
        genre=deps.genre.name,
        mode=deps.mode,
        title=(state.get("title") or deps.document_title or "").strip(),
        source_genre=str(state.get("source_genre") or ""),
        source_genre_confidence=float(state.get("source_genre_confidence") or 0.0),
        chapters=chapters,
        target_chapters=deps.target_chapters,
        max_chapters=deps.max_chapters,
        uncovered=uncovered[:200],
        uncovered_fraction=fraction,
        budget=budget_for(deps.supported, deps.purposes, len(chapters)),
        fallback=bool(state.get("fallback")),
        attempts=int(state.get("attempts") or 0),
        notes=list(state.get("complaints") or []) if state.get("fallback") else [],
        spend=list(state.get("spend") or []),
    )
    return {"plan": plan, "log": [f"budget: {plan.budget} research queries"]}


def build_planning_graph(deps: PlanDeps):
    """Compile the graph, with `deps` bound into every node.

    `bind` is `docagent/graph.py`'s, down to preserving `__name__` so a trace
    names the function rather than a closure.
    """

    def bind(fn):
        async def node(state: PlanState) -> dict:
            return await fn(state, deps)

        node.__name__ = fn.__name__
        return node

    g: StateGraph = StateGraph(PlanState)
    g.add_node("detect", bind(n_detect))
    g.add_node("propose", bind(n_propose))
    g.add_node("validate", bind(n_validate))
    g.add_node("adopt", bind(n_adopt))
    g.add_node("fallback", bind(n_fallback))
    g.add_node("budget", bind(n_budget))

    g.add_edge(START, "detect")
    g.add_edge("detect", "propose")
    g.add_edge("propose", "validate")
    g.add_conditional_edges("validate", e_validation, ["adopt", "propose", "fallback"])
    g.add_edge("adopt", "budget")
    g.add_edge("fallback", "budget")
    g.add_edge("budget", END)

    # No checkpointer. See the module docstring: Temporal is the durability, and
    # a second one it does not know about is a resume from a step it thinks has
    # still to run.
    return g.compile()


async def plan(deps: PlanDeps) -> TransformPlan:
    """Run the graph and return the frozen plan."""
    graph = build_planning_graph(deps)
    state = await graph.ainvoke({"attempts": 0, "spend": [], "log": []})
    result = state.get("plan")
    if isinstance(result, TransformPlan):
        for line in state.get("log") or []:
            log.info("plan: %s", line)
        return result
    # Unreachable by the graph's own edges — `budget` is the only terminal node
    # and it always writes a plan — but a graph that silently returned nothing
    # would be an activity that succeeded having done nothing, which is the one
    # outcome this pipeline has learned to refuse rather than to log.
    raise RuntimeError("the planning graph produced no plan")
