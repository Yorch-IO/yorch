"""Payload types shared by the ingest workflow and its activities.

Everything here crosses a Temporal boundary, so two rules apply to every field.
It must be small — bulk content travels as an :class:`~brainworker.artifacts.ArtifactRef`
and never inline — and it must be safe to persist forever, because workflow
history is written to Postgres and kept for the namespace's whole retention
period. No credential, no document text, no embedding vector appears in any
dataclass below.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .artifacts import ArtifactRef
from .graph.schema import LEGACY_TENANT_ID

#: Formats V1 accepts. The engine's extractor registry is the authority; this
#: list exists so the API can refuse an unsupported file *before* a workflow
#: starts, which is a better error than one surfacing three activities deep.
SUPPORTED_FORMATS: frozenset[str] = frozenset(
    {".pdf", ".txt", ".md", ".docx", ".pptx", ".xlsx", ".xlsm", ".csv"}
)


@dataclass
class IngestRequest:
    """What the user asked for. Carries a project id, never a credential."""

    library_id: str
    #: Absolute path as the *worker* sees it — inside the container this is
    #: under /workspace. The API translates before starting the workflow.
    source_path: str
    #: Library-relative key, which is what document identity is derived from, so
    #: moving a library root does not orphan every document in it.
    source_key: str
    title: str = ""
    author: str | None = None
    folder_id: str | None = None
    #: When false the run stops after the free preview and never spends. This is
    #: the default because the alternative — spending unless told not to — is
    #: the wrong way round for money.
    auto_approve: bool = False
    #: Deliberately re-running a document that is already indexed.
    #:
    #: Without it the workflow short-circuits on `already_indexed`, which is
    #: right for an import — identical bytes at a new path must link rather than
    #: embed twice — and wrong for a user who asked for this on purpose. Added
    #: as a defaulted *field* rather than a new activity parameter: Temporal maps
    #: payloads onto activity parameters by arity, so changing an arity breaks
    #: every history in flight, while an absent field simply takes its default.
    reindex: bool = False
    #: Whose corpus this becomes. Everything derived from the run carries it:
    #: the catalog rows, the Qdrant payloads, the graph nodes — and the *ids*,
    #: since `version_id` and `concept_id` are salted with it.
    #:
    #: A defaulted field rather than a required one, for the reason `reindex`
    #: gives above and for one of its own: the free, self-managed plane is
    #: single-tenant by construction and *is* the legacy tenant, so naming it
    #: there would be ceremony. The paid plane always sets it explicitly.
    tenant_id: str = LEGACY_TENANT_ID


@dataclass
class StageOptions:
    """Which paid stages the user approved, decided at the gate.

    Every stage is individually switchable because they cost wildly different
    amounts. From a real measured run (`docaget/costo.json`): correction
    $0.0334, eval-set generation $0.0131, embedding $0.0033. Correction
    dominates, which is why it is the thing the gate sits in front of and why
    turning it off alone removes most of the bill.
    """

    correct: bool = True
    embed: bool = True
    extract_semantics: bool = True
    generate_evalset: bool = False
    #: Learn a document-family profile when none was found for this fingerprint.
    #: Paid — one bounded generation call per attempt — which is why it sits
    #: behind the gate like every other stage that spends. Reusing an existing
    #: profile costs nothing and needs no switch.
    learn_profile: bool = True
    #: Decline an inherited profile. The fingerprint is structural, so two
    #: unrelated families sharing a layout collide exactly and the second
    #: document is chunked with the first one's heading rules. Retrieval metrics
    #: cannot see that — the synthetic questions are generated from the very
    #: chunks the wrong rules produced — so the escape hatch is a person's, and
    #: this is it.
    ignore_profile: bool = False
    #: Show a second gate with the correction diff before the remaining spend.
    review_correction: bool = False
    #: Condense each concept's accumulated descriptions into one a person reads.
    #: Paid — one bounded call per concept whose descriptions grew past a
    #: threshold — so it belongs behind the gate like every other stage that
    #: spends. Off by default: extracting the descriptions is free, and turning
    #: them into one sentence is the part that costs.
    condense_descriptions: bool = False


@dataclass
class Spend:
    """What one paid stage actually consumed, as the API reported it."""

    stage: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float | None = None


@dataclass
class ProfileWarning:
    """A structural fingerprint shared by two unrelated document families."""

    profile_id: str
    collides_with: str
    #: Topical overlap between this document and the one the profile was learned
    #: from, 0.0–1.0. A *low* value is the dangerous case: same structure,
    #: unrelated subject matter, so the wrong heading rules get applied.
    similarity: float
    detail: str


@dataclass
class ProfileRules:
    """The learned rules, flattened to exactly what gets applied.

    `docagent.profiles.Profile` is deliberately *not* the payload type. It also
    carries an eval set (88 questions on a real run), the scores those questions
    produced and a tuning history — none of which any activity applies, and all
    of which would then persist in workflow history for the namespace's whole
    retention period. The profile itself stays on disk in `workspace/profiles/`;
    these fifteen scalars are what crosses the boundary.

    Split by where each rule lands: `header_patterns` and the two page-geometry
    values are `DocRules` and apply at **extraction**; the rest are `ChunkRules`
    and apply at **chunking**. That split is why a profile carrying header
    patterns forces a second extraction pass and one without it does not.
    """

    header_patterns: list[str] = field(default_factory=list)
    footer_cutoff: float = 0.06
    line_gap_factor: float = 1.5

    target_chars: int = 1200
    hard_cap_chars: int = 2000
    overlap_chars: int = 150
    min_chunk_chars: int = 40
    max_embed_chars: int = 2600
    question_chars_per_mark: int = 300
    heading_l1_max: int = 40
    heading_l2_max: int = 120
    heading_l1_pattern: str | None = None
    heading_l2_pattern: str | None = None

    question_pattern: str | None = None
    footnote_pattern: str | None = None

    @property
    def needs_reextraction(self) -> bool:
        """Whether applying these rules changes what the extractor produces.

        Only `header_patterns` does. `rules.to_rules()` sets no other `DocRules`
        field, so for every profile that did not learn a header pattern the
        second extraction pass is skipped — and skipping it matters, because
        re-extracting a 900-page PDF costs minutes of CPU for no change.
        """
        return bool(self.header_patterns)


@dataclass
class ProfileDecision:
    """Which rules this document is being processed with, and where they came
    from.

    `source` mirrors the engine's own vocabulary: ``reused`` (an existing
    profile matched this family's fingerprint, free), ``learned`` (one was
    proposed and validated for it, paid) or ``default`` (the built-in rules,
    which were themselves measured on a real book — a fallback, not a gap).
    """

    fingerprint: str
    source: str = "default"
    slug: str = ""
    learned_from: str = ""
    revisions: int = 0
    rules: ProfileRules = field(default_factory=ProfileRules)
    warnings: list[ProfileWarning] = field(default_factory=list)
    #: Rule names that survived validation, for the gate to show. Empty for a
    #: reused or default profile, which learned nothing on this run.
    adopted: list[str] = field(default_factory=list)
    #: What learning cost. None when nothing was learned — including when
    #: learning was attempted and fell back, which still spent and still reports.
    spend: Spend | None = None


@dataclass
class Staged:
    """The file, identified. Produced before anything reads its content."""

    content_sha256: str
    byte_size: int
    fmt: str
    extractor: str
    title: str


@dataclass
class Registered:
    document_id: str
    version_id: str
    #: False when these exact bytes were already registered under another path.
    #: Not an error — it is the outcome that stops a duplicate being embedded
    #: twice and then competing against itself in ranking.
    created: bool
    #: Set when the identical content is already fully indexed, in which case
    #: the run links the new path and stops without spending anything.
    already_indexed: bool
    #: Carried forward so the paid stages can derive tenant-salted ids without a
    #: new activity parameter — Temporal maps payloads onto parameters by arity,
    #: and an absent field takes its default where a changed arity breaks every
    #: history in flight.
    tenant_id: str = LEGACY_TENANT_ID


@dataclass
class Extraction:
    """What extraction produced, both halves by reference.

    The evidence travels alongside the text because the profile-collision check
    needs it and re-extracting a 900-page PDF to get it back would cost minutes
    for something already computed.
    """

    text: ArtifactRef
    evidence: ArtifactRef
    extractor: str
    structured: bool = False
    #: Library-relative key of the document this came from. Carried so a learned
    #: profile can record *what* it was learned from without an absolute path —
    #: `Profile.learned_from` ends up in a file that outlives the run, and an
    #: absolute container path there would be meaningless on the host.
    source_key: str = ""


@dataclass
class ChunkKindCount:
    kind: str
    count: int


@dataclass
class Preview:
    """What the free gate shows. Costs nothing to produce.

    `chunks_are_final` is the field that must not be quietly dropped by any UI
    rendering this. Correction runs *before* chunking because it changes the
    text's length, which would invalidate every `char_span` — so when correction
    is enabled the previewed chunks are not the chunks that get indexed.
    """

    text: ArtifactRef
    chunks: ArtifactRef
    chunk_count: int
    kinds: list[ChunkKindCount]
    characters: int
    chunks_are_final: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class StageEstimate:
    stage: str
    model: str
    input_tokens: int
    output_tokens: int
    #: None means "token counts are known, price is not". Zero would be a lie.
    usd: float | None
    #: The upper end of the range, for stages whose figure is a *mean* over a
    #: corpus with a measured spread rather than a ceiling.
    #:
    #: This exists because one number could not satisfy both of the estimate's
    #: invariants at once: covering the worst real document meant exceeding twice
    #: the smallest one, and "never undershoot" and "never overshoot wildly" are
    #: both real harms. A range binds each invariant to a different end — the low
    #: figure stays within reach of a typical document, the high one covers the
    #: worst — instead of forcing a choice between which rule to break.
    #:
    #: Equal to `output_tokens` and `usd` wherever the point estimate is already
    #: a ceiling, so a stage with no measured spread reads as a single figure.
    output_tokens_high: int = 0
    usd_high: float | None = None


@dataclass
class Estimate:
    """Projected spend, per stage, from measured token counts.

    Prices are a third-party multiplier over counts this repository measured.
    They go stale and they can simply be wrong, so `price_source` travels with
    every estimate and any surface showing a dollar figure has to repeat it.
    """

    stages: list[StageEstimate]
    total_usd: float | None
    price_source: str
    unpriced_stages: list[str] = field(default_factory=list)
    #: The upper end of the bill. Equal to `total_usd` when no stage in this run
    #: has a measured spread, which is what lets a surface render "$X" or
    #: "$X – $Y" from the same two fields without a flag.
    total_usd_high: float | None = None


@dataclass
class Correction:
    """The result of the dominant paid stage.

    `report` is a human summary, not a status: correction is an *improvement*,
    not a precondition. A paragraph the model omitted, or whose correction lost
    a scripture reference, keeps its original text — so a run where every
    paragraph was rejected still produces usable output, and the numbers here
    are what tells the user that happened.
    """

    text: ArtifactRef
    report: ArtifactRef
    paragraphs: int
    changed: int
    rejected: int
    missing: int
    cache_hits: int
    spend: Spend


@dataclass
class Chunked:
    chunks: ArtifactRef
    count: int
    kinds: list["ChunkKindCount"] = field(default_factory=list)


@dataclass
class Indexed:
    collection: str
    points: int
    dimensions: int
    spend: Spend


@dataclass
class Semantics:
    concepts: int
    claims: int
    edges: int
    spend: Spend
    #: The condensation step's own bill, when it ran. Separate from `spend` so
    #: the ledger shows what condensing cost rather than folding it into the
    #: per-chunk extraction it is not part of.
    condense_spend: Spend | None = None
    #: How many claims carry a quote the code found in their own chunk. Defaulted
    #: rather than required, because this is a Temporal payload: a workflow that
    #: started before the field existed must keep deserialising, and a rebuild
    #: replaying an artifact written before it reports zero honestly.
    claims_verified: int = 0


@dataclass
class RebuildInputs:
    """Everything a rebuild needs, reassembled from the catalog and one old run.

    Nothing here is re-derived from the source file, which is the point: a
    rebuild must reproduce the index that was paid for, and re-extracting would
    not. Correction changes the text's length, so a re-run with correction off
    yields different `char_span`s and different chunk boundaries — a silently
    different index wearing the same version id.
    """

    request: "IngestRequest"
    staged: "Staged"
    registered: "Registered"
    chunks: ArtifactRef
    characters: int
    #: Absent for any document indexed before the `semantics` artifact existed,
    #: and for one whose extraction was declined. A rebuild then restores
    #: structure and vectors only, and says so rather than quietly thinning the
    #: graph.
    semantics: ArtifactRef | None = None
    #: Which run's artifacts these are, for the report.
    source_run_id: str = ""


@dataclass
class RebuildReport:
    """The one-question gate a rebuild shows. Only one stage here can spend."""

    document_id: str
    version_id: str
    title: str
    chunk_count: int
    characters: int
    source_run_id: str
    semantics_available: bool
    estimate: "Estimate | None" = None


@dataclass
class RebuildResult:
    run_id: str
    document_id: str
    version_id: str
    state: str
    detail: str = ""
    points: int = 0
    graph: dict[str, int] = field(default_factory=dict)
    semantics_replayed: bool = False
    spend: list[Spend] = field(default_factory=list)


@dataclass
class GateReport:
    """Everything the approval screen needs, and nothing that costs money."""

    run_id: str
    document_id: str
    version_id: str
    preview: Preview
    estimate: Estimate
    profile_warnings: list[ProfileWarning] = field(default_factory=list)
    #: Which rules this document will be chunked with, and where they came from.
    #: The gate shows it because "reused someone else's rules" and "about to pay
    #: to learn new ones" are different decisions for the user to make.
    profile: ProfileDecision | None = None


@dataclass
class IngestResult:
    run_id: str
    document_id: str
    version_id: str
    state: str
    indexed_chunks: int = 0
    projected: dict[str, int] = field(default_factory=dict)
    total_usd: float | None = None
    detail: str = ""
