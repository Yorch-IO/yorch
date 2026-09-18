"""Payload types for recasting a document into another literary genre.

Everything here crosses a Temporal boundary, so the two rules `pipeline.py`
states apply to every field: it must be small, because bulk content travels as
an :class:`~brainworker.artifacts.ArtifactRef` and never inline, and it must be
safe to persist for the namespace's whole retention period.

:class:`Continuity` is the exception that proves the rule, and it is worth
reading before anything else here. It accumulates, and it carries `tail` — the
previous chapter's last words, **verbatim**. That is customer prose, and the
paragraph above forbids it in a payload for a reason stronger than the size cap:
workflow history is persisted for the namespace's whole retention period. So
`Continuity` never crosses the boundary. It is the row shape of the
`transform_continuity` artifact, and what the workflow carries is the
:class:`~brainworker.artifacts.ArtifactRef`.

It is bounded anyway. `trimmed()` enforces every cap and
`tests/transform/test_continuity.py` asserts the serialised size of a record
carried through a synthetic 200-chapter run — because the cost of an unbounded
one is not an error but a workflow that dies at chapter forty of sixty, having
paid for all forty.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from ..artifacts import ArtifactRef
from ..pipeline import Estimate, Spend

#: How faithfully the source material may be treated. On the wire as a name,
#: never as a set of knobs, for the reason `effort.py` records about its own
#: levels: a client that sent the numbers could ask for anything, and the paid
#: plane would have to police a table it does not own.
MODES: tuple[str, ...] = ("faithful", "adaptive")
DEFAULT_MODE = "faithful"

#: Why the library is consulted at all. Each one changes which queries the
#: research node makes and what it keeps; none of them changes the relevance
#: mechanism, which is `answering.retrieve.search` unchanged.
PURPOSES: tuple[str, ...] = (
    "context",
    "verification",
    "completion",
    "contradiction",
)

#: Longest a chapter's own source material may be before the planner has to
#: split it. Equal to `CORRECTION_BATCH_CHARS`, and for the same reason: it is
#: the size of input this product has measured a single bounded generation call
#: to handle without the envelope running out of room.
MAX_CHAPTER_SOURCE_CHARS = 24_000

#: A ceiling on the outline, so a proposal cannot turn a book into a thousand
#: one-paragraph chapters, each of which is a paid call.
MAX_CHAPTERS = 60

#: How much source material an Adaptive plan may leave unassigned. Condensing is
#: what Adaptive mode is *for*; discarding a quarter of a document is not, and
#: the difference has to be a number or it is not a rule.
MAX_UNCOVERED_FRACTION = 0.25

MAX_GLOSSARY = 120
MAX_ESTABLISHED = 80
MAX_THREADS = 24
MAX_CITED = 400
#: The previous chapter's last words, verbatim. A summary cannot tell the next
#: chapter what sentence it is continuing from, and the seam is exactly where
#: continuity breaks.
TAIL_CHARS = 1200

#: The ceiling `tests/transform/test_continuity.py` asserts the serialised
#: record stays under. Two orders of magnitude below Temporal's ~2 MB payload
#: cap, because the rule is that bulk data never enters a payload and a record
#: that had to be measured against the cap would already be the wrong shape.
MAX_CONTINUITY_BYTES = 64 * 1024


@dataclass
class TransformRequest:
    """What the user asked for: one indexed version, one genre, one mode."""

    library_id: str
    document_id: str
    version_id: str
    genre: str
    #: **Required, with no default**, unlike `Question.tenant_id`.
    #:
    #: A tenant default on a *field* is what put a paying organisation's
    #: `Document`, `DocumentVersion`, five `Section`s and thirteen `Chunk`s into
    #: the legacy tenant's graph under unsalted ids, with nothing failing
    #: anywhere — retrieval returned the right chunks, with an empty locator and
    #: no claims, which reads as a graph nobody has projected yet rather than as
    #: a leak. `Question` carries that default because it predates the rule.
    #: This dataclass does not predate anything.
    tenant_id: str
    mode: str = DEFAULT_MODE
    #: Only the purposes enabled for *this* transformation. Empty means the
    #: library is not consulted at all, which is a legitimate request and not a
    #: degraded one.
    purposes: list[str] = field(default_factory=list)
    #: Skip both gates. Off by default because the alternative — spending unless
    #: told not to — is the wrong way round for money.
    auto_approve: bool = False
    #: What the run row is labelled with in the import queue, where `title` is
    #: the document's and there is no document being created here.
    label: str = ""


@dataclass
class TransformOptions:
    """Switches decided at the request, and re-affirmed at the gate.

    `review_plan` defaults to **True**, unlike `StageOptions.review_correction`,
    and the difference is deliberate. A correction diff is a check on work
    already done; the outline *determines the entire work*, and the quote made
    against it is the first honest one — the quote before planning is arithmetic
    over a projected chapter count and says so.
    """

    review_plan: bool = True
    #: Consult the library at all. Distinct from an empty `purposes`: this is
    #: the switch a gate can turn off after seeing the budget, and the field a
    #: future caller can leave alone.
    research: bool = True


@dataclass
class SourceSpan:
    """A contiguous run of source chunks, inclusive at both ends."""

    first: int
    last: int


@dataclass
class SourceChapter:
    """One chapter of the source, as the chunker detected it.

    `title` is empty for a document whose chapters were never detected, and that
    is reported rather than papered over: the chunker *consumes* a heading
    paragraph, so an empty title means the index genuinely holds one untitled
    block, which is what every breadcrumb on it already says.
    """

    title: str
    first: int
    last: int
    chars: int


@dataclass
class SourceReading:
    """Everything the free `reading` stage learned, and nothing expensive."""

    source_run_id: str
    chunks: ArtifactRef | None
    chapters: list[SourceChapter] = field(default_factory=list)
    chunk_count: int = 0
    characters: int = 0
    #: How many references the free sweep found. The references **themselves**
    #: are not here, and that is deliberate: at the sweep's own caps they come to
    #: a quarter of a megabyte of customer prose, which is the one thing
    #: `pipeline.py` forbids a payload to carry. Every activity that needs them
    #: re-derives them from `chunks` with `reading.references_of`, which is a
    #: file read of a few milliseconds and is deterministic, so two activities
    #: cannot disagree about what the source cited. The excerpt the genre
    #: analysis reads is re-derived the same way.
    reference_count: int = 0
    title: str = ""
    author: str = ""
    language: str = "es"


@dataclass
class ProbeReading:
    """What the library has to say about this document, before anything is spent.

    `supported` is the count of chunks belonging to *other* versions that
    cleared `retrieve.MIN_SCORE`, summed over the sampled chapters. It is the
    existing relevance mechanism's own number — the same one
    `effort.effective_style_level` uses to tell a narrow question from a broad
    one — and the research budget is a function of it and of nothing else.
    """

    supported: int = 0
    sampled: int = 0
    per_chapter: list[int] = field(default_factory=list)
    budget: int = 0
    spend: list[Spend] = field(default_factory=list)


@dataclass
class ChapterPlan:
    """One target chapter, and the source material it is made from.

    `sources` is what makes the plan checkable at all: without it there is no
    way to ask whether the transformation preserved the document's ideas, and
    "faithful" would be a word in a prompt rather than a property of a plan.
    """

    ordinal: int
    title: str
    intent: str = ""
    sources: list[SourceSpan] = field(default_factory=list)
    chars: int = 0


@dataclass
class TransformPlan:
    """The outline the whole work is composed against.

    `source_genre` is recorded here and reaches no gate report and no page. The
    brief is explicit that the transformation performs its analysis silently;
    what it does not ask for is that the analysis be unrecorded, and a reading
    whose instrument is unrecorded cannot be compared with the next one.
    """

    genre: str
    mode: str
    #: What the finished work is called. Proposed by the planner, because a
    #: transformation into another genre is a different work and the source's
    #: filename stem is rarely its title — the recorded case is a library of 74
    #: books rendering as `01_RetoDeDios_INT-S`. Empty falls back to the
    #: document's catalog title, which is what `fill_document_metadata` may
    #: already have corrected.
    title: str = ""
    source_genre: str = ""
    source_genre_confidence: float = 0.0
    chapters: list[ChapterPlan] = field(default_factory=list)
    #: What the free quote was priced against, and what `validate` **enforces**.
    #:
    #: This is the design's answer to quoting a job whose call count is only
    #: known after planning. The estimate does not predict the chapter count; it
    #: names a ceiling, and a proposal exceeding it is refused and refined
    #: against that as feedback. Same move `channel/estimate.py` makes when it
    #: quotes the longest candidate rather than the likeliest: "choosing the
    #: shortest would quote a bill the run cannot come in under".
    target_chapters: int = 0
    max_chapters: int = 0
    #: Source chunk indices no chapter claims. Empty is required in Faithful
    #: mode and bounded by `MAX_UNCOVERED_FRACTION` in Adaptive.
    uncovered: list[int] = field(default_factory=list)
    uncovered_fraction: float = 0.0
    budget: int = 0
    #: The outline came from the fallback rather than from a validated proposal.
    #: Not a failure — after `MAX_REFINE` attempts, adopting the source's own
    #: chapters is better than refusing to transform a document whose only fault
    #: is being ordinary.
    fallback: bool = False
    attempts: int = 0
    notes: list[str] = field(default_factory=list)
    spend: list[Spend] = field(default_factory=list)


@dataclass
class Continuity:
    """What one chapter hands the next, so the work reads as one work.

    Bounded by construction. `trimmed()` is called at every settle and the
    caps are the fields' own, because a record that grew with the document
    would be bulk data in the one place this codebase has a standing rule
    against — and the failure would be a workflow that dies at chapter forty of
    a sixty-chapter book, having paid for all forty.
    """

    #: How a term was rendered, so chapter 12 says it the way chapter 2 did.
    glossary: dict[str, str] = field(default_factory=dict)
    #: One-line facts already stated, so they are not re-introduced as new.
    established: list[str] = field(default_factory=list)
    #: Narrative threads opened and not yet closed. Matters most for Novel,
    #: Chronicle and History, and costs the others a field they leave empty.
    threads: list[str] = field(default_factory=list)
    #: The previous chapter's last `TAIL_CHARS`, verbatim.
    tail: str = ""
    #: Chunk ids already cited, so the bibliography can be assembled without a
    #: second pass over every chapter's prose.
    cited: list[str] = field(default_factory=list)

    def trimmed(self) -> "Continuity":
        """Every cap applied. Newest wins, because later chapters are nearer.

        The glossary keeps its *first* renderings rather than its last, which is
        the opposite choice and the right one: the point of a glossary is that a
        term does not drift, so the entry to protect is the one already on the
        page.
        """
        glossary = dict(list(self.glossary.items())[:MAX_GLOSSARY])
        return Continuity(
            glossary=glossary,
            established=self.established[-MAX_ESTABLISHED:],
            threads=self.threads[-MAX_THREADS:],
            tail=self.tail[-TAIL_CHARS:],
            cited=self.cited[-MAX_CITED:],
        )

    def size(self) -> int:
        """Serialised bytes, so the bound can be asserted rather than assumed."""
        return len(json.dumps(asdict(self), ensure_ascii=False).encode("utf-8"))


@dataclass
class CitedSource:
    """One library chunk that survived verification, with what names it.

    Every field comes from the catalog or the graph — `Evidence` carries them
    off the Qdrant payload and `retrieve._attach_citations` fills the locator —
    and none of them comes from the model. That is what makes the bibliography
    unfabricatable rather than merely well-instructed.
    """

    chunk_id: str
    document_id: str = ""
    version_id: str = ""
    title: str = ""
    locator: str = ""
    claim: str = ""


@dataclass
class ComposedChapter:
    """One finished chapter. Travels as a row of `draft.jsonl`, never inline."""

    ordinal: int
    title: str
    body: str
    cited: list[CitedSource] = field(default_factory=list)


@dataclass
class ChapterRequest:
    """One chapter's whole input, as a single payload object.

    One object rather than eight positional arguments, because Temporal maps
    payloads onto parameters **by arity**: adding a parameter to
    `evaluate_index` once made the converter give up and pass raw dicts, and the
    stage died on `'builtin_function_or_method' object has no attribute 'path'`
    three frames from its cause.
    """

    run_id: str
    request: "TransformRequest"
    plan: "TransformPlan"
    reading: "SourceReading"
    ordinal: int
    #: What this chapter may spend on research. An **input**, so a retried
    #: attempt replays with the identical number and cannot overrun the run's
    #: budget; the workflow subtracts only what a successful attempt reports.
    allowance: int = 0
    #: The work so far, and what the previous chapter handed on. `None` on the
    #: first chapter.
    draft: ArtifactRef | None = None
    continuity: ArtifactRef | None = None
    report: ArtifactRef | None = None


@dataclass
class ChapterOutcome:
    """What `compose_chapter` hands back to the workflow.

    The prose is not here: it is in `draft`, the artifact this activity rewrote.
    What crosses the boundary is a reference, a bounded continuity record, and
    counts — which is the same division `IngestRequest` makes and for the same
    reason.
    """

    draft: ArtifactRef
    #: The continuity file as it stands after this chapter, **as a reference**.
    #: `Continuity.tail` is verbatim customer prose and may not be persisted in
    #: workflow history; see this module's own docstring.
    continuity: ArtifactRef
    #: The per-chapter record, including the text of every paragraph dropped for
    #: resting on a citation that did not check out. Prose again, so again a
    #: reference: a dropped claim must not be lost, and it must not be persisted
    #: in workflow history either.
    report: ArtifactRef
    #: Research queries this chapter actually spent, out of the allowance it was
    #: handed. The allowance is an *input* so that a retried activity replays
    #: with the identical number; this is the *output* the workflow decrements by.
    queries_made: int = 0
    #: Paragraphs dropped for resting only on citations that did not verify.
    #: Their text is in the run's report — a dropped claim that leaves the work
    #: quietly shorter is its own failure.
    removed: int = 0
    #: Chunk ids the model cited that it was never shown.
    invented: int = 0
    cited: int = 0
    revisions: int = 0
    spend: list[Spend] = field(default_factory=list)


@dataclass
class TransformGateReport:
    """The first gate: a quote made from what is knowable for free.

    The chapter count here is a **projection** from the source's own chapter
    count and the genre's ratio, and the report says so. The recorded harm is a
    quote that misleads at the moment somebody decides — measured once at 7.1x
    on a re-import — so a projected figure that is not labelled as one is worse
    than no figure.
    """

    genre: str
    mode: str
    purposes: list[str] = field(default_factory=list)
    source_title: str = ""
    source_chapters: int = 0
    characters: int = 0
    projected_chapters: int = 0
    research_budget: int = 0
    supported: int = 0
    projection: bool = True
    estimate: Estimate | None = None


@dataclass
class TransformPlanReport:
    """The second gate: the outline, and the first quote anybody should act on.

    Its own type and its own query, never the first report reassigned. In
    `IngestWorkflow` `self._report` is assigned once before the first gate and
    never cleared, so the second gate serves the first one's pre-correction
    preview — seen in the real window telling a reader "Nothing has been paid
    for yet" over a run that had spent $0.58.
    """

    genre: str
    mode: str
    chapters: list[ChapterPlan] = field(default_factory=list)
    uncovered_fraction: float = 0.0
    research_budget: int = 0
    fallback: bool = False
    notes: list[str] = field(default_factory=list)
    spent_so_far: float | None = None
    estimate: Estimate | None = None


@dataclass
class TransformApproval:
    """What a person answered at a gate.

    `options` is optional and `None` means *keep what the run was started with*.
    That default is the fix for a recorded live defect in the other direction:
    the Angular gate sent `profileOptions(choice)` alone while `IngestWorkflow`
    reads `approval.options`, so **every stage a person unticked before pressing
    Import was silently turned back on at the gate**, and every stage they
    ticked on was silently dropped. Nothing failed; the run simply did something
    other than what was asked. Making the server keep the run's own options when
    a client sends none means a client that forgets to merge loses nothing.
    """

    approved: bool
    options: "TransformOptions | None" = None
    reason: str = ""


@dataclass
class TransformResult:
    """What the run produced, for the caller and for the run row."""

    state: str = "running"
    run_id: str = ""
    document: ArtifactRef | None = None
    report: ArtifactRef | None = None
    chapters: int = 0
    characters: int = 0
    cited_documents: int = 0
    removed: int = 0
    invented: int = 0
    queries_made: int = 0
    spend: list[Spend] = field(default_factory=list)
    total_usd: float | None = None
    #: Why, when `state` is not `succeeded`. The only thing that says *which*
    #: refusal this is — the defect `_settle` already had once, where four facts
    #: with four different remedies all rendered as one line.
    reason: str = ""
