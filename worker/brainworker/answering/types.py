"""What a question and its answer look like on the wire.

Every field here is either shown to a person or checked by code before it is.
There is no field a model fills in that is trusted without verification: the
citations are re-checked against the evidence actually retrieved, and an answer
that cites nothing is not returned as an answer at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..pipeline import Spend


@dataclass
class Question:
    library_id: str
    text: str
    #: How many chunks reach the answering model. Small on purpose: the model
    #: must read all of them, and a larger set buys recall at the cost of the
    #: attention that makes citations accurate.
    top_k: int = 8
    #: Payload equality filters, e.g. `{"document_id": "doc_…"}` to ask about
    #: one book. Validated against an allowlist before reaching Qdrant.
    filters: dict[str, str] = field(default_factory=dict)
    #: Semantic edges below this may not support an answer.
    confidence_floor: float = 0.6


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

    @property
    def grounded(self) -> bool:
        return self.state == "answered" and bool(self.citations)
