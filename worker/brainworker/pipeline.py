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
    #: What to call the library, if it does not exist yet.
    #:
    #: `library_id` is chosen by the client and arrives as a plain string, so it
    #: is an identifier and not a name. `ensure_library` was being handed it
    #: twice — id as id and id as name — which is why every picker in the
    #: product reads `lib_teologia` for a library seeded as «Teología», and why
    #: both rows in this installation's catalog are named after themselves.
    #:
    #: Empty falls back to the id, so a caller that sends nothing gets exactly
    #: today's behaviour: that is what makes this additive rather than a change
    #: of contract. Appended at the end for the reason `reindex` gives above —
    #: Temporal maps payloads by arity, and a field inserted in the middle is
    #: how `extract_text` once produced `'dict' object has no attribute
    #: 'source_path'` three frames from its cause.
    library_name: str = ""
    #: What kind of run this is, when the default is wrong.
    #:
    #: `register_document` writes `reindex` or `index`, which is right for every
    #: caller that stages a file. A video is neither, and `run.kind` is not
    #: bookkeeping: it is how a client knows which gate shape to expect, because
    #: `/runs/{id}/gate` returns a `GateReport` and `/runs/{id}/video-gate`
    #: returns a `VideoGateReport`, and a video run listed as `index` would be
    #: polled at the first and silently decoded into the wrong type.
    #:
    #: Empty means "decide from `reindex`", so every existing caller is
    #: unchanged. Appended last, for the arity reason above.
    run_kind: str = ""


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
    #: Try to improve retrieval, and measure whether it worked.
    #:
    #: Off by default and bounded to a single chunking candidate. The free half —
    #: `min_score`, `per_section`, dense-only — changes nothing in the index and
    #: is tried exhaustively; the paid half re-cuts the document, which means
    #: embedding every chunk again, ~100 minutes for a 600-chunk book against the
    #: measured per-minute quota. So the gate prices that second pass explicitly
    #: rather than letting a loop discover it.
    #:
    #: Turning this on also raises the eval sample to `EVAL_SAMPLE_TUNING`,
    #: because at 40 questions the bootstrap margin cannot resolve the effect
    #: tuning is looking for and the round would honestly refuse everything.
    tune: bool = False
    #: Condense each concept's accumulated descriptions into one a person reads.
    #: Paid — one bounded call per concept whose descriptions grew past a
    #: threshold — so it belongs behind the gate like every other stage that
    #: spends. Off by default: extracting the descriptions is free, and turning
    #: them into one sentence is the part that costs.
    condense_descriptions: bool = False
    #: Package what was indexed as an EPUB a person can read on a device.
    #:
    #: Appended and defaulted, which is what makes it safe without a
    #: `workflow.patched`: a history in flight — a gate parked for up to seven
    #: days — decodes a payload that never carried the field as `False`, so the
    #: conditional command is never issued and the replay's command sequence is
    #: unchanged. The one patch in this codebase guards an activity inserted at
    #: the *head* of a workflow, which is the case that cannot be defaulted away.
    #:
    #: Nearly free: the packaging costs nothing, and the one call it can make —
    #: a title and an author for a document the catalog has neither for — runs
    #: once per document ever.
    build_epub: bool = False


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
    #: Which kind of warning this is, because the two have different force.
    #:
    #: ``collision`` says two documents share a structural fingerprint, which is
    #: ordinary — it is the whole point of a family profile, and the second
    #: document of a family is cheaper than the first because of it.
    #:
    #: ``heading_disagreement`` says the inherited rules and the built-in ones
    #: read a *different number of chapters* in this document. That is the one
    #: automatic signal available for a collision between unrelated families, and
    #: the metrics provably cannot see it: the eval questions are generated from
    #: the very chunks the wrong rules produced. So it is the one that withholds
    #: activation.
    #:
    #: Defaulted rather than required, because this is a Temporal payload: a
    #: workflow that started before the field existed must keep deserialising.
    kind: str = "collision"


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

    A fourth value, ``tuned``, is this repository's own: a tuning round's
    candidate rules. It is not ``default`` — that is the load-bearing part,
    because `chunk_final` applies a decision's rules only when the source is not
    ``default``, so a candidate labelled that way would be silently ignored and
    the round would measure the very chunking it was trying to change.
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
    #: Whose profile directory this document's rules are read from and written
    #: to. Carried here rather than added as an activity parameter, because
    #: Temporal maps payloads onto parameters by arity and an absent *field*
    #: takes its default where a changed arity breaks every history in flight.
    #:
    #: It exists because a profile is not anonymous: it carries `header_patterns`
    #: derived from a book's running header, which is often its title. `CLAUDE.md`
    #: listed profiles as the one open gap of the three tenancy does not cover,
    #: and this is the field that closes it — the engine now takes an explicit
    #: root, so a `chdir` no longer has to serve every organisation at once.
    tenant_id: str = LEGACY_TENANT_ID
    #: Whether a paragraph of this text is a single unbroken line.
    #:
    #: True for a transcript, where a paragraph is one timed group of caption
    #: cues and **its index is what carries its timestamp**. `correct_text`
    #: collapses any blank line the model returns inside one before re-joining,
    #: because `"\n\n".join` would otherwise turn one paragraph into two and
    #: shift every timestamp after it — silently, because nothing downstream can
    #: tell a shifted table from a good one.
    #:
    #: False for a document, and that is not laziness: a book's paragraph
    #: legitimately contains single newlines — verse, numbered review-question
    #: blocks — and collapsing those would damage the corpus this engine was
    #: measured on. Appended last, for the arity reason above.
    single_line_paragraphs: bool = False


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
    #: What chunking noticed and could not fix, in the caller's language.
    #:
    #: `Transcribed` and `Preview` both carry one of these and `Chunked` did
    #: not, so `chunk_transcript` built its warnings and dropped them on the
    #: way out — including the one that says a paid correction was *not* the
    #: stream indexed. That reached the worker's stderr and nothing else, and
    #: a container replaced three minutes after a run took the only copy with
    #: it. Defaulting to empty is what keeps replay safe: a `Chunked` decoded
    #: from a history written before this field existed has no warnings, so
    #: the workflow's conditional event is not scheduled and the command
    #: sequence is unchanged.
    warnings: list[str] = field(default_factory=list)


@dataclass
class Indexed:
    collection: str
    points: int
    dimensions: int
    spend: Spend


@dataclass
class EvalSet:
    """Synthetic questions generated from the chunks they must find.

    They leak vocabulary to the lexical leg by construction — a question written
    *from* a passage shares its wording — which is why `Scores` carries the
    dense-only figure beside the hybrid one rather than reporting a single number
    that flatters the index.

    `reused` distinguishes "this family had already paid for its questions" from
    "this run generated them". Regenerating them each time would measure the
    questions instead of the change, which is the whole reason the engine keeps
    them in the profile.
    """

    items: ArtifactRef
    questions: int
    sample: int
    spend: Spend
    reused: bool = False


@dataclass
class Scores:
    """What the index that was just written can actually be asked.

    The product has never had this figure for any of its documents; the engine's
    CLI has it for thirty. Every number here is measured against the collection
    the run wrote, not estimated.
    """

    recall_at_1: float = 0.0
    recall_at_5: float = 0.0
    mrr_at_10: float = 0.0
    #: The same questions with the lexical leg switched off. The *gap* between
    #: this and `recall_at_5` is the eval set's own vocabulary leakage, so a
    #: hybrid figure quoted alone is not interpretable.
    recall_at_5_dense_only: float = 0.0
    #: The best dense score an off-topic query achieves — what "no match" looks
    #: like in this collection. A recall figure without it says nothing about
    #: whether the index can tell a miss from a hit.
    noise_floor: float = 0.0
    chunks: int = 0
    eval_questions: int = 0
    #: Bootstrap margin on the objective. Below it, a difference is noise: with
    #: σ ≈ 0.358 it takes about 80 questions to resolve a +0.040 MRR effect, and
    #: reporting a smaller one as real is the failure this guards against.
    margin: float = 0.0
    leakage: str = ""
    report: ArtifactRef | None = None
    #: Embedding the questions costs money. Small, and not zero.
    spend: Spend | None = None


@dataclass
class TuneOutcome:
    """What one tuning round concluded.

    `kind` is `retrieval` when a free knob won and was adopted, `chunking` when
    the free knobs are exhausted and the next candidate costs a full re-embed,
    and `none` when there is nothing left worth trying.

    `baseline_objective` and `margin` travel together because the candidate has
    to be judged against the margin computed *before* it ran. A margin derived
    from the candidate's own run moves with it, and the comparison would then be
    between two things that both changed — which is how three tuning rounds once
    drifted 0.729 → 0.762 → 0.700 while each of them had reverted.
    """

    kind: str = "none"
    label: str = ""
    baseline_objective: float = 0.0
    margin: float = 0.0
    #: The rules to try, as a decision `chunk_final` can be handed unchanged.
    #: Present only for `kind == "chunking"`, and *proposed*, not adopted.
    candidate: "ProfileDecision | None" = None
    notes: list[str] = field(default_factory=list)
    report: ArtifactRef | None = None


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
    #: Measured retrieval quality, when the run was asked to measure it. None
    #: means "not asked", never "zero" — the two are different answers and a
    #: surface showing 0.00 recall for a run that never evaluated would send
    #: somebody to fix an index that is fine.
    scores: Scores | None = None


# --- video ------------------------------------------------------------------
#
# A video is not a file, and the two places that shows are the two dataclasses
# that would otherwise be reused. It has no path to stage and no bytes to hash
# before something has transcribed it, so `VideoRequest` carries a URL where
# `IngestRequest` carries a path, and `VideoGateReport` can carry no `Preview`
# on the path where the text does not exist yet. Everything downstream of the
# transcript — `Correction`, `Chunked`, `Indexed` — is reused unchanged.


def run_kind_of(request: "IngestRequest") -> str:
    """What `run.kind` a document import files itself under.

    One function because two callers must agree. `IngestWorkflow` opens the run
    row before its first activity and `register_document` opens it again on the
    way past; `start_run` is `ON CONFLICT (id) DO NOTHING`, so the **first** call
    is the one whose `kind` survives. Two copies of this expression would
    eventually disagree, and the symptom would be a run filed under a kind the
    client uses to decide which gate shape to expect.

    `run_kind` is the override a caller that is not staging a file uses;
    'reindex' has been in the CHECK constraint since the first migration.
    """
    return request.run_kind or ("reindex" if request.reindex else "index")


@dataclass
class RunOpen:
    """What a workflow knows about its run before its first activity has run.

    **The reason this exists is a run that failed and left no trace.** The `run`
    row was INSERTed by `register_document`, which is the *second* activity on
    both ingest paths — so a video refused by YouTube in `probe_video`, or a file
    import that dies in `stage_source`, failed against a row that did not exist.
    `finish_run` is a bare UPDATE, which affects zero rows and raises nothing, so
    the failure was recorded nowhere and the import queue — which reads the
    catalog — had nothing to show. Measured in production on 2026-09-05: one
    `VideoIngestWorkflow` had ever run there and
    `SELECT count(*) FROM run WHERE kind='video'` returned 0.

    One payload rather than six positional arguments, deliberately. Temporal maps
    payloads onto parameters **by arity**, and this repository has already paid
    for that once: `evaluate_index` grew a sixth parameter with a default and the
    five-argument call site died on `'builtin_function_or_method' object has no
    attribute 'path'`, because the converter gave up and passed raw dicts.

    `library_id` and `label` are the two things the queue needs and the join
    cannot give it before there is a document: the queue is per-library, and it
    renders `title ?? label ?? workflow_id`.
    """

    run_id: str
    workflow_id: str
    kind: str
    tenant_id: str
    library_id: str
    #: The URL for a video, the picked file's basename for an import.
    label: str = ""


@dataclass
class VideoRequest:
    """One video to index. The `IngestRequest` analogue, and deliberately not it.

    `library_id` is a shelf of videos, chosen by the client exactly as it is for
    a document, so `ensure_library` checks who owns it the same way.
    """

    library_id: str
    url: str
    title: str = ""
    author: str | None = None
    auto_approve: bool = False
    reindex: bool = False
    tenant_id: str = LEGACY_TENANT_ID
    library_name: str = ""
    #: Caption languages to prefer, best first. Empty means "the library's own
    #: language, then whatever the video has".
    languages: list[str] = field(default_factory=list)
    #: Which task queue runs the two activities that talk to YouTube.
    #:
    #: Empty means "the queue this workflow is on", which is exactly what the
    #: product did before this field existed — so every existing caller, every
    #: test and the whole local stack are unchanged until somebody sets it.
    #:
    #: It is set when the worker's own egress is refused. Measured 2026-09-05:
    #: `yt-dlp extract_info` succeeds from a residential IP in 2.6 s and is
    #: refused from the EC2 egress IP with "Sign in to confirm you're not a
    #: bot", while the *same* host fetches every signed caption URL at 200. So
    #: the split is narrow on purpose: only `resolve_video` and — because a
    #: media URL is bound to the address that resolved it — `fetch_audio`.
    #:
    #: It travels in the request rather than being read from settings inside the
    #: workflow, because a workflow may only decide on what its own history
    #: holds. Reading an environment variable there would make replay depend on
    #: the machine replaying it.
    fetch_queue: str = ""

    #: A `VideoInfo` the caller already resolved, or None to resolve here.
    #:
    #: **The client-side answer to the same measurement `fetch_queue` answers**,
    #: and the one that works for somebody who is not also running a second
    #: worker. `extract_info` is refused from a datacentre address and answered
    #: from a residential one, so the desktop app makes that call on the machine
    #: the person is sitting at — with a bundled `yt-dlp` — and sends the result.
    #: The pipeline then never asks YouTube who this video is at all.
    #:
    #: It travels because it is small *by construction*: the raw info dict is
    #: 1,656,277 bytes on `yq6uVBsVkeQ` and `VideoInfo` is a few kilobytes. That
    #: narrowing already existed for crossing a task queue, and turns out to be
    #: exactly what is needed for crossing a plane.
    #:
    #: It is **not trusted**. `videosource.check_resolved` refuses a record whose
    #: id does not match `url` or whose caption URL is not YouTube's — at the
    #: route, and again in `probe_video`, which is the activity that fetches it.
    resolved: VideoInfo | None = None

    #: Audio the caller already staged, as the path the worker sees, or empty.
    #:
    #: The second half of the same split, for a video with no captions at all. A
    #: `googlevideo` media URL carries the address that resolved it and answers
    #: 403 anywhere else — measured — so the download cannot be moved away from
    #: the `extract_info` that produced the URL. When the app made that call, the
    #: app is the only thing that can make this one.
    #:
    #: A **path**, not an S3 URI: putting bytes in S3 needs the instance role,
    #: which a laptop does not have and must not be given. The app uploads
    #: through the plane it is already authenticated to (`POST /videos/audio`),
    #: the file lands in that organisation's own inbox, and `stage_audio` moves
    #: it to S3 from the host that holds the role. `Paths.contains` refuses a
    #: path naming anywhere else, which is the check `stage_source` already
    #: makes for a document.
    audio_path: str = ""


@dataclass
class CaptionTrack:
    """One caption track a video offers."""

    language: str
    #: ``manual`` or ``auto``. The distinction is not cosmetic: an auto track is
    #: a machine transcript with no punctuation and rolling duplicates, and it
    #: is the only one `dedupe_rolling` may be applied to.
    kind: str
    ext: str
    name: str = ""


@dataclass
class VideoInfo:
    """What one `yt-dlp extract_info` learned, narrowed to what may cross.

    **The narrowing is the point.** The raw info dict for a real video is
    1,656,277 bytes of JSON — measured on `yq6uVBsVkeQ`, which offers 161
    automatic caption languages — so forwarding `info` would put a megabyte and a
    half into a Temporal payload on every video run. This is the handful of
    fields anything downstream actually reads.

    It exists because `extract_info` is the one call YouTube refuses from a
    datacenter IP, and it is therefore the one step that has to run somewhere
    else. `resolve_video` returns this from whatever worker can make the call;
    `probe_video` does everything else on the host that owns the workspace.

    `caption_url` travels and `audio` deliberately does not: measured
    2026-09-05, a caption URL carries `ip=0.0.0.0` and was served to a second
    host at 200, while a `googlevideo` media URL carries the resolving address
    (`ip=181.32.19.72`) and answers **403** anywhere else. So captions can be
    fetched wherever, and audio cannot — which is why `fetch_audio` runs beside
    the resolution rather than taking a URL from it.
    """

    video_id: str
    title: str
    channel: str
    duration_s: int
    upload_date: str
    #: Only ever read into `identity_basis`, and only when there are no captions.
    format_id: str = ""
    tracks: list[CaptionTrack] = field(default_factory=list)
    #: The track that will be read, or None when Amazon Transcribe must run.
    chosen: CaptionTrack | None = None
    #: The signed URL for `chosen`. Empty when there is no chosen track.
    caption_url: str = ""


@dataclass
class VideoProbe:
    """Everything free that can be learned about a video, and its identity.

    ``content_sha256`` is what `version_id` is derived from, and it is computed
    here — *before* the gate — rather than from the finished transcript, because
    that is what lets a re-import of an unchanged video short-circuit to
    `link_duplicate` **without paying to transcribe it again**. Deriving it from
    the transcript would mean discovering the duplicate only after the bill.

    The cost is that on the Transcribe path it is a proxy rather than a content
    hash: video id, duration, upload date, the chosen source, and the audio
    format id. On the caption path the caption bytes are in hand for free, so
    their digest goes in and it *is* a content hash. ``identity_basis`` carries
    the exact string that was hashed, so the digest can be re-derived by hand
    from the artifact rather than taken on trust.
    """

    video_id: str
    canonical_url: str
    source_key: str
    title: str
    channel: str
    duration_s: int
    upload_date: str
    content_sha256: str
    identity_basis: str
    tracks: list[CaptionTrack] = field(default_factory=list)
    #: The track that will be used, or None when Amazon Transcribe must run.
    chosen: CaptionTrack | None = None
    warnings: list[str] = field(default_factory=list)
    #: What this activity wrote, by reference.
    #:
    #: Returned rather than re-derived by whatever reads them next. A reference
    #: carries a sha256 and `ArtifactStore.read_bytes` verifies it, so a
    #: *fabricated* one — a path with an empty hash — fails the check it exists
    #: to pass. Probing runs before the `run` row exists and therefore cannot
    #: **record** these, which is a different thing from being unable to return
    #: them: they are a path, a hash and a size.
    probe_ref: ArtifactRef | None = None
    captions: ArtifactRef | None = None


@dataclass
class Transcribed:
    """The transcript, grouped into timed paragraphs and ready to chunk.

    ``text`` is the paragraph stream every `char_span` after this indexes;
    ``cues`` is the paragraph-index-to-time table that makes a citation a
    timestamp. They are a **matched pair** — a stale one of either is the drift
    the whole design guards against — so both are written by one activity and
    read back through their refs, whose hashes are verified.
    """

    text: ArtifactRef
    cues: ArtifactRef
    #: The evidence file, so `correct_text` gets a reference that verifies.
    evidence: ArtifactRef
    #: ``captions:es:manual`` or ``transcribe:es-ES``. On the wire so a reader
    #: can tell a human transcript from a machine one without a second lookup.
    source: str
    paragraphs: int
    characters: int
    #: Where the transcript actually ends, which is not always the duration the
    #: bill was quoted from. Reported rather than reconciled: a disagreement is
    #: worth seeing, and charging twice to resolve it is not.
    covered_s: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class AudioStaged:
    """Audio in S3, where Amazon Transcribe can read it.

    ``reused`` is what makes a retry cheap and the nightly host stop survivable:
    the object is keyed on the version, so a second attempt finds it already
    there and skips the download entirely.
    """

    s3_uri: str
    media_format: str
    bytes: int
    seconds: int
    reused: bool = False


@dataclass
class TranscriptionJob:
    """One Amazon Transcribe batch job.

    ``job_name`` is derived from the version id, never generated, so starting is
    idempotent: a Temporal retry hits `ConflictException`, which means *already
    started* and must not be charged a second time.
    """

    job_name: str
    status: str
    transcript_uri: str = ""
    failure_reason: str = ""
    language: str = ""


@dataclass
class VideoGateReport:
    """What the approval screen needs for a video, and nothing that costs money.

    Its own type rather than `GateReport`, for the reason `/runs/{id}/rebuild-gate`
    already documents: querying through the wrong workflow's typed handle decodes
    one report into the other and drops every field they do not share, without
    failing. And `GateReport.preview` is a required `Preview` holding two
    required `ArtifactRef`s — which on the Transcribe path do not exist yet.
    Fabricating them to fit the type is precisely the lie a gate exists to
    prevent, so `preview` is optional here and `None` says "there is no text to
    preview until you approve this".
    """

    run_id: str
    document_id: str
    version_id: str
    probe: VideoProbe
    estimate: Estimate
    preview: Preview | None = None
    transcript: Transcribed | None = None
    warnings: list[str] = field(default_factory=list)
    #: What the product suggests for *this* video, for the gate to start from.
    #:
    #: A suggestion rather than a rule, and it exists because the three
    #: transcript sources arrive in different shape: automatic captions carry no
    #: punctuation and are worth correcting, while a manual track and Amazon's
    #: own output are already punctuated. See `videosource.correction_default`.
    #:
    #: The approval still carries whatever the person ticked. This only decides
    #: which boxes are ticked when they first look, and it is what
    #: `auto_approve` uses when there is nobody to look at all.
    recommended: StageOptions = field(default_factory=StageOptions)


@dataclass
class VideoResult:
    run_id: str
    document_id: str
    version_id: str
    state: str
    indexed_chunks: int = 0
    projected: dict[str, int] = field(default_factory=dict)
    total_usd: float | None = None
    detail: str = ""
    #: Which half produced the text, so a reader of a finished run can tell
    #: whether anything was paid to Amazon at all.
    transcript_source: str = ""
