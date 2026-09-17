"""What a question and its answer look like on the wire.

Every field here is either shown to a person or checked by code before it is.
There is no field a model fills in that is trusted without verification: the
citations are re-checked against the evidence actually retrieved, and an answer
that cites nothing is not returned as an answer at all.
"""

from __future__ import annotations

from ..graph.schema import LEGACY_TENANT_ID
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import Field

from ..pipeline import Spend
from .effort import DEFAULT_EFFORT

#: `YYYY-MM-DD` or empty. A pattern on the field rather than a hand-raised 400,
#: for the reason `effort` is a `Literal`: FastAPI refuses it with its own
#: 422-and-a-list, which is the shape the paid plane's filter reproduces for
#: `class-validator`, so both planes answer a bad date identically.
ISO_DAY = r"^(\d{4}-\d{2}-\d{2})?$"
IsoDay = Annotated[str, Field(pattern=ISO_DAY)]


@dataclass
class Question:
    library_id: str
    text: str
    #: How many chunks reach the answering model, or None to let `effort`
    #: decide — which is what every request from the desktop app says.
    #:
    #: Small on purpose at every level: the model must read all of them, and a
    #: larger set buys recall at the cost of the attention that makes citations
    #: accurate. An explicit value still wins, because the API has accepted one
    #: since before levels existed, but it is clamped rather than trusted —
    #: `effort.resolve_top_k` is where both rules live.
    top_k: int | None = None
    #: Payload equality filters, e.g. `{"document_id": "doc_…"}` to ask about
    #: one book. Validated against an allowlist before reaching Qdrant.
    filters: dict[str, str] = field(default_factory=dict)
    #: Semantic edges below this may not support an answer.
    confidence_floor: float = 0.6
    #: Whose corpus to search. Never taken from the planner and never from a
    #: filter the caller supplied — the control plane sets it from the
    #: authenticated request, and `_validate` overwrites whatever a model put
    #: there.
    tenant_id: str = LEGACY_TENANT_ID
    #: How much evidence and reasoning this question may spend, as a level name
    #: rather than a set of numbers — see `effort.py` for what each one means and
    #: for why the numbers stay on this side of the wire. Appended last so the
    #: field order both control planes mirror is unchanged.
    #:
    #: **Spelled as a `Literal` so FastAPI refuses an unknown level itself**,
    #: rather than hand-raising a 400 with a `kind`. The paid plane validates the
    #: same field with `class-validator`, whose failures its exception filter
    #: renders as FastAPI's *own* 422-with-a-list shape — deliberately, so the
    #: two planes answer a bad request identically. A hand-rolled 400 here would
    #: have made one plane answer 400-with-an-object and the other
    #: 422-with-a-list for the same input. `effort.budget_for` stays total behind
    #: this as the second guard, the same two-guards-for-one-property shape as
    #: `ALLOWED_FILTERS` and the forced `tenant_id`.
    #:
    #: The values are restated here rather than built from `EFFORT_LEVELS`
    #: because `Literal` needs them at type-check time;
    #: `test_effort.py::test_the_literal_and_the_level_tuple_cannot_drift` is
    #: what keeps the two copies equal.
    effort: Literal["brief", "standard", "thorough"] = DEFAULT_EFFORT
    #: Narrowings a recording corpus makes askable, appended after `effort` so
    #: the field order both planes mirror is unchanged. All optional and all
    #: empty by default, which is "no filter". Each is a hard cut over what the
    #: dense floor already admits, so `off_corpus` fires more readily under one
    #: — and the reason must render as the reason.
    #:
    #: `recorded_from`/`recorded_to` are inclusive `YYYY-MM-DD` bounds on the
    #: document's recording date; `scripture` is a reference or a chapter
    #: (`Juan 3:16`, `Romanos 8`) normalised by `scripture.normalise_query`,
    #: matched where the reference was *spoken*; `source_name` is the feed or
    #: folder the document came from, an equality like `filters` carries.
    recorded_from: IsoDay = ""
    recorded_to: IsoDay = ""
    scripture: str = ""
    source_name: str = ""


@dataclass
class Plan:
    """What the planner decided. Never contains Cypher."""

    intent: str
    #: A template id from the fixed registry, or None when the planner judged
    #: that no graph traversal helps and vector search alone should answer.
    template_id: str | None
    params: dict[str, object] = field(default_factory=dict)
    #: Canonical concept names the question mentions, for resolving to nodes.
    concepts: list[str] = field(default_factory=list)
    rationale: str = ""
    spend: Spend | None = None


@dataclass
class EvidenceClaim:
    """A model's reading of one chunk, offered to the answer as a *hint*.

    Not evidence in the sense `Evidence` is: a chunk's text is what the document
    says, while this is what a model said the document says. It reaches the
    answering prompt because a claim that survived the confidence floor is a
    useful pointer into a long chunk — and it reaches it clearly labelled, with
    the rule that the chunk's text wins any disagreement. Without that rule this
    would be model output re-entering the context as if it were a source.
    """

    text: str
    confidence: float
    #: `afirma`, `niega`, `atribuido`, or `sin_estado`. A claim the document
    #: reports rather than asserts must not be read as one it holds.
    status: str = "sin_estado"
    #: The document's own words, when extraction could verify them.
    quote: str = ""
    concept: str = ""


@dataclass
class Evidence:
    """One retrieved chunk, with where it came from and how to check it."""

    chunk_id: str
    version_id: str
    document_id: str
    title: str
    breadcrumb: str
    text: str
    kind: str
    score: float
    #: "vector", "graph", or "both" — shown to the user, because an answer
    #: resting on a model-proposed edge has different standing from one resting
    #: on the document's own structure.
    source: str = "vector"
    locator: str = ""
    page: int | None = None
    #: What a model read out of this chunk, above the confidence floor. Empty
    #: whenever the graph is unreachable: an answer from vector search alone is
    #: strictly better than no answer.
    claims: list[EvidenceClaim] = field(default_factory=list)


@dataclass
class Citation:
    chunk_id: str
    locator: str
    claim: str
    page: int | None = None
    section_title: str | None = None


@dataclass
class Answer:
    #: "answered" | "insufficient_evidence" | "off_corpus"
    state: str
    text: str = ""
    citations: list[Citation] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    #: Why the answer is not an answer, when it is not one. Always populated for
    #: a non-"answered" state, because "no result" with no explanation sends the
    #: user to re-ask the same question in different words.
    reason: str = ""
    plan: Plan | None = None
    spend: list[Spend] = field(default_factory=list)
    #: The level this answer was produced at, echoed back.
    #:
    #: Not redundant with what the client sent: an answer is collected by a
    #: second request that may reach a different machine, and two answers to the
    #: same question are otherwise incomparable with nothing recording why they
    #: differ. Empty only for an `Answer` built before the level was resolved.
    effort: str = ""
    #: The level whose *style* actually wrote this answer, which is `effort`
    #: unless the corpus supplied too little to justify it — a `thorough`
    #: question that retrieved five chunks is answered in `brief`'s voice, so
    #: that developing every point does not become padding nothing can check.
    #:
    #: Reported rather than kept internal: a question asked at the widest level
    #: and answered in three sentences otherwise looks like the control is
    #: broken, when the honest answer is that there were only five fragments.
    #:
    #: **A declared field, and that is the whole point of it being here.** It was
    #: briefly set as a loose attribute by `service.ask` instead, which Python
    #: allows and which worked on the answered path — while `asdict()` silently
    #: dropped it from every API response, because `asdict` serialises declared
    #: fields and nothing else. The symptom was not an error anywhere; it was a
    #: field the UI could never see.
    style_effort: str = ""

    @property
    def grounded(self) -> bool:
        return self.state == "answered" and bool(self.citations)
