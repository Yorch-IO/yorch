"""Ingest activities. Everything before the gate is free and deterministic.

The split this module is organised around is cost, not convenience. Staging,
extraction, chunk preview, profile comparison and estimation touch no paid API,
so they all run before the user is asked to approve anything — a preview that
cost money to produce would defeat the point of having a gate.

Each activity is individually retryable, which forces two properties. It must be
idempotent, because Temporal will run it again after any transport failure. And
it must not carry bulk data in or out: content goes to the artifact store and an
``ArtifactRef`` travels instead.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from typing import Any

from temporalio import activity

from .. import config
from ..artifacts import ArtifactRef, ArtifactStore
from ..catalog import Catalog, RunEvent
from ..graph import Graph
from ..graph import projection as proj
from ..graph.schema import document_id as make_document_id
from ..graph.schema import version_id as make_version_id
from ..pipeline import (
    ChunkKindCount,
    Estimate,
    Extraction,
    IngestRequest,
    Preview,
    ProfileDecision,
    ProfileRules,
    ProfileWarning,
    Registered,
    RunOpen,
    run_kind_of,
    StageEstimate,
    StageOptions,
    Staged,
    SUPPORTED_FORMATS,
)

log = logging.getLogger(__name__)

_READ_BLOCK = 1 << 20

#: How long a best-effort catalog write may wait. Seconds, not tens of them.
RECORD_TIMEOUT = 3.0

#: Prices in USD per 1,000,000 tokens, keyed by **exact** model id.
#:
#: Checked 2026-08-20 against several third-party aggregators, which agree with
#: each other. **Google's own pricing page was not the source** — the Agent
#: Platform pricing page truncates rather than serving its tables to a fetch —
#: so these are second-hand, exactly like the engine's ledger, and carry the
#: same warning: the token counts are measured, the multipliers are not. Verify
#: against the project's GCP billing before trusting an absolute figure.
#:
#: Keyed by exact id rather than family prefix on purpose. A prefix match would
#: have silently applied `gemini-embedding-001`'s $0.15 to `gemini-embedding-2`,
#: and the 2.5-flash rates to every 3.x model.
#:
#: `gemini-embedding-2` is priced from listings that name
#: `gemini-embedding-2-preview`; the endpoint serves the non-preview id and no
#: separate figure was published for it. Flagged rather than hidden.
PRICES_PER_MILLION: dict[str, tuple[float, float]] = {
    # (input, output)
    "gemini-3.5-flash": (1.50, 9.00),
    # Released 2026-07-21 with a 17% output cut against 3.5-flash. Not the
    # configured default, but priced here so switching is a config change.
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-2.5-flash": (0.15, 1.25),
    "gemini-embedding-2": (0.20, 0.0),
    "gemini-embedding-001": (0.15, 0.0),
}

PRICE_SOURCE = (
    "Precios de terceros consultados el 2026-08-20 (la página oficial de Agent "
    "Platform no sirve sus tablas a una descarga). Los recuentos de tokens están "
    "medidos; los multiplicadores son de segunda mano. Contrastar con la "
    "facturación real de GCP antes de fiarse de una cifra absoluta."
)


def price_for(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Dollars for a measured token count, or None when the model has no price.

    None is not zero, and the distinction is the whole point of this function
    existing rather than a multiplication inline.
    """
    rates = PRICES_PER_MILLION.get(model)
    if rates is None:
        return None
    return input_tokens / 1e6 * rates[0] + output_tokens / 1e6 * rates[1]

#: Characters per token. Measured against `gemini-embedding-2` on a real
#: one-page Spanish document: 1514 characters reported 342 tokens, i.e. 4.43.
#: The lower 3.6 is kept deliberately so the text component errs toward
#: over-reporting, which is the only acceptable direction for a figure a user
#: approves spend against.
CHARS_PER_TOKEN = 3.6

#: Tokens a single generation call costs *before* any document text: system
#: instruction, response schema, and JSON envelope. This is not a detail — it
#: dominated the first real run. Estimating from character count alone reported
#: 420 input tokens for a correction call that actually cost 924, and 420 for a
#: semantic pass that cost 990. The overhead is per *call*, so it multiplies
#: with batching and with per-chunk extraction.
#:
#: Derived from that run (2026-08-20, one 1514-character page): correction
#: 924 - 342 = 582 for one batch; semantics (990 - 342) / 2 = 324 per chunk.
#: Rounded up. **One document is one data point** — these are the best numbers
#: available, not a characterised model, and a second document may move them.
CORRECTION_CALL_OVERHEAD = 600
#: Re-measured 2026-08-21, and it had to be: the semantic schema grew three
#: fields (`cita`, `estado`, `descripcion`) and the system prompt grew the three
#: paragraphs that explain them, all of which is per-call input. Measured over
#: one 16-chunk document — 15,388 input tokens against 4,949 of text — is
#: (15388 - 4949) / 16 = 652 per call. Rounded up.
#:
#: The old 350 was measured before those fields existed and was left in place
#: through the change, so the gate under-reported the first run that used them by
#: 22%: $0.0919 quoted against $0.1176 billed. Under-reporting is the one
#: direction this may not err in.
#:
#: **710 since 2026-08-22, and 660 was still too low.** That figure came from one
#: document; a 37-run batch put the real distribution at a 678 median, 676 mean
#: and 701 maximum, with **32 of the 37 above 660**. Combined with an output leg
#: running above its own mean, that pushed 8 of 34 documents past the *high* end
#: of the range — the one bound that may not be exceeded. Set above the observed
#: maximum rather than at the mean, because this is the leg that decides whether
#: the ceiling holds, and it is nearly deterministic: the spread across 37 runs
#: was 678 to 701, so a ceiling costs almost nothing in over-reporting.
SEMANTICS_CALL_OVERHEAD = 710

#: Correction returns text of roughly the length it was given. Measured 560
#: output tokens against 433 of input text with reasoning disabled, so 1.5 keeps
#: a margin over the 1.29 observed.
CORRECTION_OUTPUT_RATIO = 1.5

#: How much reasoning multiplies a stage's output when it is left enabled.
#:
#: Gemini 3.x bills thinking tokens at the output rate, and they dwarf the
#: visible answer. Measured on one page, 2026-08-20, `gemini-3.6-flash`:
#: correction 3184 output tokens with reasoning against 560 without (5.7x), and
#: semantic extraction 2682 against 1015 (2.6x). Rounded up, and kept per stage
#: because a single multiplier that covered correction would over-report
#: semantics by more than double.
THINKING_OUTPUT_MULTIPLIER = {
    "correction": 6.0,
    #: 2.64 rather than the 3.0 it was rounded up to, and the reason is that
    #: `SEMANTICS_OUTPUT_PER_CHUNK` no longer needs this to carry the margin.
    #: The ratio was measured as 2682/1015 = 2.64 against a base of 338 output
    #: tokens per chunk; that base is now 634, measured over 1787 chunks, and
    #: carries its own margin. Rounding up on top of it compounds two margins and
    #: puts the reasoning-on estimate past twice the measurement, which
    #: `test_the_estimate_stays_within_reach_of_the_measurement` rightly rejects.
    #: Two measured numbers, one margin.
    "semantics": 2.64,
    "evalset": 3.0,
    # Rule proposal is not measured. 6.0 borrows correction's multiplier, which
    # is the largest observed, because over-reporting an unmeasured stage is the
    # direction that cannot mislead a user into approving more than they meant.
    "profile": 6.0,
}

#: What one rule-proposal call costs, in tokens.
#:
#: Unlike every other stage this does **not** scale with the document.
#: `docagent.rules.propose` sends a bounded sample — 20 repeated lines, 25
#: numbered headings, 25 numbered paragraphs, 40 short lines, 8+8 first/last
#: lines — plus a fixed system prompt, whatever the book's length. So it is
#: modelled as a flat per-call figure.
#:
#: Not measured against a real call: stated as a projection, and deliberately
#: generous. Replace both with measured figures on the first real learning run.
PROFILE_CALL_INPUT = 2000
PROFILE_CALL_OUTPUT = 500
#: The estimate assumes every refine attempt is spent, because a proposal that
#: validates first time is the good case and a gate must not price the good case.
PROFILE_MAX_ATTEMPTS = 3

#: Semantic extraction emits a JSON block per chunk whose size tracks how much
#: the model finds, not how long the chunk is. The original model, 15% of total
#: input, under-reported it by 8x; a first correction measured 1015 output tokens
#: across three chunks with reasoning disabled — 338 each — and set this to 400.
#:
#: **400 was still too low, and it made the gate under-report.** Measured
#: 2026-08-21 over a real 31-document, 1787-chunk corpus (the `docaget/libros`
#: theology set, imported as pre-corrected text with reasoning off):
#:
#: * weighted mean **634** output tokens per chunk — 58% above the 400 assumed
#: * per document: min 362 (`FILOSOFÍA CONTEMPORANEA`), median 602,
#:   max **801** (`1. DESDE AGUSTÍN DE HIPONA…`) — 100% above the assumption
#:
#: The gate therefore quoted $0.2789 for a document that billed $0.3319, and the
#: whole corpus $7.10 against a real $10.16. Under-reporting is the one direction
#: this figure may not err in: a user who approved a smaller number than they
#: were billed has been misled.
#:
#: Set to the measured weighted mean, **not** to a figure that covers the worst
#: document — and that is a compromise forced by a conflict between two
#: invariants, recorded here rather than resolved:
#:
#: * `test_the_estimate_over_reports_every_measured_stage` demands the estimate
#:   be at least the measurement.
#: * `test_the_estimate_stays_within_reach_of_the_measurement` and
#:   `test_the_estimated_bill_over_reports_the_measured_one` demand it be at most
#:   twice the measurement — "over-reporting wildly is its own failure", because
#:   it pushes a user to decline work that was affordable.
#:
#: Both are anchored to a single 3-chunk page whose 338 output tokens per chunk
#: sit *below the minimum* of the 31 documents above. Covering the worst real
#: document (801) means exceeding 2x that page's 338, so no single global constant
#: satisfies both across a corpus with a 2.2x spread. 634 is the widest value that
#: keeps the band intact.
#:
#: The consequence is honest but not good: documents above the mean — roughly half
#: of them — are still under-reported, by up to 26% at 801. The real fix is an
#: estimate that is not one global number: a per-document-family figure learned
#: alongside the profile, or a range at the gate instead of a point. Both are
#: larger than a constant and belong to whoever owns the gate's contract. Until
#: then, say at the gate that semantic extraction is the figure most likely to
#: be exceeded.
#:
#: **Left at 634 on purpose after a 2026-08-21 measurement said 788.** That
#: document (16 chunks, `Avanzando hacia la madurez`) is one data point, and the
#: 634 above is a weighted mean over 1787 chunks — but the honest caveat is
#: sharper than "one document": *every one of those 31 documents was extracted
#: before `cita`, `estado` and `descripcion` existed*, so the mean now describes
#: a schema the pipeline no longer uses and is certainly low. It is not raised
#: here because 790 would put the estimate at 2.3x the 3-chunk page that
#: `test_the_estimate_stays_within_reach_of_the_measurement` is anchored to, and
#: trading a 16% under-report for a 130% over-report is not an improvement. The
#: fix is the one the paragraph above already names — a per-family figure or a
#: range at the gate — and it is now overdue rather than merely desirable.
SEMANTICS_OUTPUT_PER_CHUNK = 634

#: How far above the mean a document's semantic output lands, as a multiplier on
#: the figure above. This is what turns the estimate into a range, and the range
#: is what lets both of the gate's invariants hold at once: the low figure stays
#: within reach of a typical document, the high one covers the worst, and neither
#: rule has to be broken to satisfy the other.
#:
#: **1.85, re-measured 2026-08-21 over every semantics run in the catalog** — 37
#: runs, 1830 chunks, reconstructed by joining `cost_entry` to each run's own
#: `chunks.jsonl`:
#:
#:     weighted mean 640    p50 606    p90 788    p95 894    max 1175
#:
#: The multiplier is taken against `SEMANTICS_OUTPUT_PER_CHUNK` — the number it
#: actually multiplies — not against the freshly measured 640: 1175/634 = 1.853,
#: rounded up to 1.86. Computing it against the measured mean instead gave 1.85
#: and a ceiling of 1173, two tokens *under* the worst document, which is the
#: whole failure this constant exists to prevent.
#: `test_the_high_end_covers_the_worst_document_in_the_corpus` caught it.
#:
#: The mean itself is left at 634 rather than moved to 640: the difference is
#: within the noise of a 37-run sample, and 36 of those runs predate the schema's
#: three new fields, so a "re-measurement" would mix two schemas for no gain.
#:
#: The distribution is the argument for a range: half the corpus sits *below* the
#: mean and the tail runs to nearly twice it.
#:
#: It replaced 1.27, which came from an earlier reading of 31 documents whose
#: maximum was 801 — and **that ceiling moved as the corpus grew**, which is the
#: thing to remember about this number. 1.27 left 3 of 37 runs (8%) above the
#: high end, so the range still under-reported them. The query that produced it
#: is in `doc/COMPANY_BRAIN.md`.
#:
#: **1.91 since 2026-08-22**, and the prediction that it would move again was
#: right within two days: a 37-run batch produced a document at 1210 output
#: tokens per chunk, above the 1179 that 1.86 allowed. 1210/634 = 1.908. The
#: ceiling has now gone 801 → 1175 → 1210 as the corpus grew, which is the
#: argument for re-running the query after any large import rather than trusting
#: the constant.
#:
#: A figure this wide is only tolerable because it is explicitly the *high* end.
#: As a point estimate it would be the "over-reporting wildly" failure the reach
#: test exists to catch — which is exactly why that test binds the low end now.
#:
#: Only semantics has a spread. Every other stage's figure is already a ceiling —
#: correction's 1.5 output ratio over a measured 1.29, and the unmeasured stages'
#: borrowed 6.0 reasoning multiplier — so widening them would over-report twice.
#: How many questions an eval set holds, and the seed that keeps two runs of one
#: document comparable. Defined here rather than in `paid` because the estimator
#: and the activity must not be able to disagree about the call count — the
#: estimate is a promise about a bill.
EVAL_SAMPLE = 40

#: The sample a *tuning* run uses instead.
#:
#: Not a preference. The bootstrap margin a candidate has to beat is computed
#: from the baseline's own per-question reciprocal ranks, and with σ ≈ 0.358 it
#: takes roughly 80 questions to resolve the +0.040 MRR effect that restoring a
#: chunk overlap is worth. At 40 the round measures honestly and refuses almost
#: everything — which is the design working, and is also a full second embedding
#: pass spent on a question the sample could never answer. So turning tuning on
#: buys the questions that make its own comparison possible, and the gate prices
#: both halves.
EVAL_SAMPLE_TUNING = 80

EVAL_SEED = 20260726

#: Characters of *content* in one eval-set call: the chunk capped at 2,000 plus
#: up to two 300-character neighbours the question must not also answer. A
#: ceiling rather than a mean, because this stage has no `OUTPUT_SPREAD` and the
#: point estimate therefore has to be the ceiling itself.
EVALSET_INPUT_CHARS = 2_600

#: How much of each neighbouring chunk `build_evalset` sends so the question it
#: writes cannot also be answered by one of them.
EVALSET_NEIGHBOUR_CHARS = 300

#: Tokens one generated question costs, before the reasoning multiplier.
#:
#: **Measured 2026-08-31 on the first real run, and it corrected a 3.6x
#: under-report.** The guess was 90 — the length of a question plus the
#: `answerable_only_by_main` flag — which through the ×3 reasoning multiplier
#: quoted 270 tokens per call. The run billed **7,743 output tokens over 8
#: calls, i.e. 968 each**, and since output is priced at five times input the
#: whole estimate came in at $0.0338 against a real $0.0650. Under-reporting is
#: the one direction this must never fail in: a user who approved $0.03 and was
#: billed $0.07 has been misled, and the reverse has not.
#:
#: 400 × 3.0 = 1,200 per call, which covers the measurement with about 24% of
#: margin. **What is measured is the product, not the split**: the run had
#: reasoning on throughout, so how much of the 968 is the question and how much
#: is thinking was not separated, and `THINKING_OUTPUT_MULTIPLIER["evalset"]`
#: stays at its guessed 3.0 rather than being tuned to fit one document.
#:
#: One document, eight calls. A second one may move it again.
EVALSET_OUTPUT_PER_CALL = 400

#: Off-topic queries the noise floor is measured with. Duplicated from
#: `docagent.evaluate.NOISE_QUERIES` rather than imported, to keep this estimator
#: free of engine imports — `test_the_noise_query_count_has_not_drifted` fails if
#: the engine grows another one.
NOISE_QUERIES = 4

#: Tokens one query embedding costs. A synthetic question runs 100-200 characters
#: and `CHARS_PER_TOKEN` is 3.6, so this is a ceiling for both the questions and
#: the four noise queries.
EVAL_QUERY_TOKENS = 60

OUTPUT_SPREAD = {"semantics": 1.91}

#: Characters per correction call, mirroring `docagent.correct.MAX_BATCH_CHARS`.
#: Imported rather than guessed would be better, but importing the engine at
#: module scope pulls its whole dependency chain into every activity worker.
CORRECTION_BATCH_CHARS = 24_000


#: Description condensation, defined here rather than in `paid.py` because that
#: module imports this one and two copies of a threshold drift.
#:
#: Tokens of accumulated description before a concept costs a call. Below it the
#: concatenation *is* the description and is free — the pattern is
#: nano-graphrag's `_handle_entity_relation_summary`, which only pays once the
#: text gets long, and 500 is its own default.
CONDENSE_TOKENS = 500
#: Words allowed in a condensed description: it is read in a panel beside a
#: graph, not in a document.
CONDENSE_WORDS = 60
#: Contributing chunks above which a concept's description is left alone.
#: LightRAG's `FORCE_LLM_SUMMARY_ON_MERGE` in spirit: past a point another chunk
#: barely moves a description, and re-condensing on every import turns a one-off
#: cost into a recurring one.
CONDENSE_SOURCE_CAP = 12
#: Twice the threshold that trips a condensation, because accumulated text
#: overshoots before anything notices.
CONDENSE_INPUT_PER_CALL = 2 * CONDENSE_TOKENS
#: Words counted as tokens and doubled. Over-reporting a bounded output is the
#: cheap direction.
CONDENSE_OUTPUT_PER_CALL = 2 * CONDENSE_WORDS


def _settings() -> config.Settings:
    return config.load()


def _sha256_file(path: pathlib.Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while block := fh.read(_READ_BLOCK):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def _record(run_id: str, name: str, ref: ArtifactRef) -> ArtifactRef:
    """Note an artifact in the catalog as well as on disk.

    The file alone is not enough: the workflow's history expires with the
    namespace's retention period, and after that the catalog is the only record
    that the run produced anything. Failing to record must not fail the stage
    that produced it, so this swallows — an artifact the catalog forgot is
    recoverable, a re-extracted 900-page PDF is expensive.
    """
    try:
        settings = _settings()
        # Unpooled and short: this is bookkeeping. A pool retries a refused
        # connection in the background, so an unreachable catalog would cost
        # this stage the full timeout for every artifact a run produces rather
        # than one immediate error.
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            catalog.record_artifact(
                run_id,
                name=name,
                rel_path=ref.path,
                sha256=ref.sha256,
                size_bytes=ref.bytes,
            )
    except Exception as e:
        log.warning("could not record artifact %s for %s: %s", name, run_id, e)
    return ref


# ---------------------------------------------------------------------------
# Free stages
# ---------------------------------------------------------------------------


@activity.defn(name="stage_source")
async def stage_source(request: IngestRequest) -> Staged:
    """Identify the file before reading it as a document.

    Hashing first is what makes the duplicate check possible without parsing,
    and parsing is the expensive part for a 900-page PDF. It also fixes the
    version identity before any stage can fail, so a retry of a later activity
    cannot land under a different id.
    """
    from docagent.extract import extractor_name

    settings = _settings()
    path = pathlib.Path(request.source_path)

    # **The path is the request's, so it is checked before it is read.**
    #
    # Nothing else stops it. This activity does not stage a file — it is handed
    # a path and hashes whatever is there — so without this an organisation
    # could name another's inbox, or `/run/secrets/providers.env`, and have the
    # pipeline index it into their own corpus under their own tenant. The
    # containment check is against *their* tree, not the workspace: "somewhere
    # under /workspace" is exactly the check that would wave a neighbour's file
    # through.
    scope = settings.paths.for_tenant(request.tenant_id)
    if not scope.contains(path):
        raise PermissionError(
            f"{request.source_path!r} is outside this organisation's workspace "
            f"({scope.root})"
        )

    if not path.is_file():
        raise FileNotFoundError(f"no such file: {request.source_path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_FORMATS:
        raise ValueError(
            f"unsupported format {suffix!r}; V1 supports {sorted(SUPPORTED_FORMATS)}"
        )

    digest, size = _sha256_file(path)
    # **The title falls back to `source_key`, not to the staged path.**
    #
    # `path` is where the file *landed*, and in cloud mode the server named it
    # itself — `randomUUID() + suffix` — precisely so that a name arriving over
    # HTTP never becomes a path segment. So `path.stem` is by construction not a
    # human name, and using it labelled every document imported through the paid
    # plane with a UUID: seen on screen 2026-08-31 as
    # "1f9c2599-2ffe-43f3-a386-eddd928c82c5" where the book's name belonged.
    #
    # `source_key` is the library-relative name a person reads, and it is what
    # the document's identity already derives from. Its stem drops the extension
    # the way `path.stem` did; a key with directories in it keeps only the last
    # segment, since the folder is not part of the book's name.
    fallback = pathlib.PurePosixPath(request.source_key).stem or path.stem
    return Staged(
        content_sha256=digest,
        byte_size=size,
        fmt=suffix.lstrip("."),
        extractor=extractor_name(str(path)),
        title=request.title or fallback,
    )


@activity.defn(name="register_document")
async def register_document(
    request: IngestRequest, staged: Staged, run_id: str, workflow_id: str
) -> Registered:
    """Create the catalog rows, deduplicating on content.

    Idempotent in both directions: re-running creates nothing new, and a second
    path holding identical bytes links to the existing version rather than
    minting a second one.
    """
    settings = _settings()
    doc_id = make_document_id(request.library_id, request.source_key)
    # Salted with the tenant: two customers importing the same PDF used to
    # compute the same id, and this is a primary key.
    ver_id = make_version_id(staged.content_sha256, request.tenant_id)

    with Catalog(settings.database_url) as catalog:
        # The name travels, and empty means "leave it alone". This used to pass
        # the id in both positions, which is why a library seeded as «Teología»
        # reads as `lib_teologia` in every picker after its first import.
        catalog.ensure_library(
            request.library_id,
            request.library_name,
            tenant_id=request.tenant_id,
        )
        catalog.upsert_document(
            tenant_id=request.tenant_id,
            document_id=doc_id,
            library_id=request.library_id,
            source_key=request.source_key,
            title=staged.title,
            fmt=staged.fmt,
            author=request.author,
            folder_id=request.folder_id,
            # Recorded so the Library can re-index without asking the user to
            # find the file again. `source_key` is library-relative on purpose
            # and cannot name a file on disk.
            source_path=request.source_path,
        )
        # The run row is created without a version and the version attached
        # afterwards. `run.version_id` is a foreign key, so naming it here would
        # reference a `document_version` row that this activity has not written
        # yet — and the resulting ForeignKeyViolation surfaces as a failed
        # ingest with no obvious connection to ordering.
        catalog.start_run(
            tenant_id=request.tenant_id,
            run_id=run_id,
            workflow_id=workflow_id,
            # Through `run_kind_of`, which `IngestWorkflow.open_run` also uses:
            # `start_run` is `ON CONFLICT DO NOTHING`, so the workflow's earlier
            # call is the one whose kind survives and two copies of the
            # expression would eventually disagree.
            kind=run_kind_of(request),
            document_id=doc_id,
            library_id=request.library_id,
        )
        version, created = catalog.register_version(
            tenant_id=request.tenant_id,
            version_id=ver_id,
            document_id=doc_id,
            content_sha256=staged.content_sha256,
            byte_size=staged.byte_size,
        )
        catalog.attach_version(run_id, doc_id, version.id)

    return Registered(
        document_id=doc_id,
        version_id=version.id,
        created=created,
        already_indexed=version.state == "indexed",
        tenant_id=request.tenant_id,
    )


@activity.defn(name="extract_text")
async def extract_text(
    request: IngestRequest, run_id: str, rules: ProfileRules | None = None
) -> Extraction:
    """Run the engine's format-specific extractor. Free, and never rewrites.

    Called twice for a document whose family learned header patterns, and once
    otherwise. The first pass runs with no rules, because the fingerprint that
    selects a profile is computed from the evidence *that* pass produces — there
    is no way to know which rules apply until the document has been read once.
    The second pass strips the running headers and writes `extracted_text`.

    `ProfileRules.needs_reextraction` is what decides, and it is true only when
    header patterns were learned: no other `DocRules` field is ever learned, so
    re-reading a 900-page PDF to apply nothing would be pure waste.

    Structured sources — spreadsheets, decks, CSV — come back as chunks rather
    than as a text stream, and those are written straight through: an LLM must
    never rewrite a cell value, and the plan limits correction to prose for
    exactly that reason.

    The evidence is written too, not because this stage needs it but because the
    profile-collision check does, and re-extracting a 900-page PDF to recover
    something already computed would cost minutes.
    """
    from dataclasses import asdict as _asdict

    from docagent.extract import extract, extractor_name

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)

    extracted = extract(request.source_path, doc_rules(rules))
    evidence_ref = _record(
        run_id, "evidence", store.write_json("evidence", _asdict(extracted.evidence))
    )
    #: The rules-free pass writes `raw_text`; a pass applying learned header
    #: patterns writes `extracted_text`, so both survive and the run records
    #: exactly what the profile changed.
    text_kind = "extracted_text" if rules is not None and rules.needs_reextraction else "raw_text"

    if extracted.is_structured:
        rows = [_asdict(c) for c in (extracted.chunks or [])]
        text_ref = _record(
            run_id, "structured_chunks", store.write_jsonl("structured_chunks", rows)
        )
        structured = True
    else:
        text_ref = _record(
            run_id, text_kind, store.write_bytes(text_kind, extracted.text or b"")
        )
        structured = False

    return Extraction(
        text=text_ref,
        evidence=evidence_ref,
        extractor=extractor_name(request.source_path),
        structured=structured,
        source_key=request.source_key,
        tenant_id=request.tenant_id,
    )


@activity.defn(name="preview_chunks")
async def preview_chunks(
    run_id: str,
    extraction: Extraction,
    options: StageOptions,
    decision: ProfileDecision | None = None,
) -> Preview:
    """Chunk without correcting, so the gate can show real structure for free.

    `chunks_are_final` is False whenever correction is enabled, and that is not
    a caveat the UI may drop: correction runs before chunking because it changes
    the text's length, which would invalidate every `char_span`. The chunks
    below are therefore a faithful preview of *this* text and not of the text
    that will actually be indexed.
    """
    from docagent.chunk import build_chunks, split_paragraphs

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    warnings: list[str] = []
    text_ref = extraction.text
    rules = decision.rules if decision and decision.source == "reused" else None
    #: A profile that has not been learned yet changes the chunks this preview is
    #: showing. Saying so is not optional — the whole claim of the gate is that
    #: what it shows is what will be indexed.
    will_learn = bool(
        decision
        and decision.source == "default"
        and options.learn_profile
        and not extraction.structured
    )

    if extraction.structured:
        rows = list(store.iter_jsonl(text_ref))
        kinds = Counter(r.get("kind", "desconocido") for r in rows)
        characters = sum(len(r.get("text", "")) for r in rows)
        chunk_ref = _record(
            run_id, "preview_chunks", store.write_jsonl("preview_chunks", rows)
        )
        return Preview(
            text=text_ref,
            chunks=chunk_ref,
            chunk_count=len(rows),
            kinds=[ChunkKindCount(k, n) for k, n in sorted(kinds.items())],
            characters=characters,
            # A structured source is never corrected, so its preview *is* final.
            chunks_are_final=True,
            warnings=["fuente estructurada: no se corrige ni se reescribe"],
        )

    data = store.read_bytes(text_ref)
    paragraphs = split_paragraphs(data)
    chunks = build_chunks(data, paragraphs, chunk_rules(rules), kind_classifier(rules))

    rows = [
        {
            "index": c.index,
            "kind": c.kind,
            "chapter": c.chapter,
            "section": c.section,
            "text": c.text,
            "char_from": c.char_from,
            "char_to": c.char_to,
        }
        for c in chunks
    ]
    chunk_ref = _record(
        run_id, "preview_chunks", store.write_jsonl("preview_chunks", rows)
    )

    if not chunks:
        warnings.append(
            "la extracción no produjo ningún fragmento — el PDF puede no tener "
            "capa de texto y necesitar OCR"
        )
    elif not any(c.chapter or c.section for c in chunks) and not will_learn:
        # `heading_level` only recognises *numbered* headings ("2.1 Título")
        # without a learned profile; an unnumbered all-caps heading like "LIBRO
        # PRIMERO" is prose to it. The document still indexes and still cites
        # correctly — it just has no table of contents to navigate, and the user
        # should learn that here rather than from an empty Explore screen.
        warnings.append(
            "no se detectó ninguna sección: el motor solo reconoce encabezados "
            "numerados salvo que un perfil aprenda un patrón para los que no lo "
            "están, así que este documento se indexará sin índice de contenidos"
        )
    if options.correct:
        warnings.append(
            "la corrección cambia la longitud del texto, así que estos "
            "fragmentos no son los que se indexarán"
        )
    if will_learn:
        warnings.append(
            "todavía no hay un perfil para esta familia de documentos: se "
            "aprenderá uno tras la aprobación, así que estos fragmentos "
            "cambiarán"
        )
    elif rules is not None:
        warnings.append(
            f"troceado con las reglas heredadas del perfil «{decision.slug}»"
        )

    kinds = Counter(c.kind for c in chunks)
    return Preview(
        text=text_ref,
        chunks=chunk_ref,
        chunk_count=len(chunks),
        kinds=[ChunkKindCount(k, n) for k, n in sorted(kinds.items())],
        characters=len(data),
        # False whenever anything downstream will change the text or the rules:
        # correction rewrites it, and learning a profile re-reads and re-chunks
        # it. Either way the previewed chunks are not the indexed ones.
        chunks_are_final=not options.correct and not will_learn,
        warnings=warnings,
    )


@activity.defn(name="estimate_cost")
async def estimate_cost(
    preview: Preview, options: StageOptions, decision: ProfileDecision | None = None
) -> Estimate:
    """Project the work from a preview's character and chunk counts.

    A thin wrapper over :func:`estimate_for`, which is where the arithmetic
    lives so a second gate can reuse it rather than reimplement it. The video
    route does exactly that: it has the same characters and chunks to reason
    about and no `Preview` to put them in, and a gate whose numbers were
    computed twice would eventually quote two different bills for one pipeline.
    """
    return estimate_for(preview.characters, preview.chunk_count, options, decision)


def estimate_for(
    characters: int,
    chunk_count: int,
    options: StageOptions,
    decision: ProfileDecision | None = None,
) -> Estimate:
    """Project the work from character counts, before spending anything.

    Token counts deliberately over-estimate: `CHARS_PER_TOKEN` takes the low end
    of the measured range, so the figure at the gate is more likely to exceed
    the real usage than to undershoot it. A user who approved a smaller number
    than they were billed has been misled; the reverse has not.

    Dollars are a separate question and currently answered with None — see
    `PRICES_PER_MILLION`.
    """
    settings = _settings()
    tokens = int(characters / CHARS_PER_TOKEN)
    stages: list[StageEstimate] = []

    # Reasoning is billed as output, so an estimate that ignored it under-reported
    # a real page by roughly 4x. A budget of 0 switches it off entirely; None
    # means the model's own default, which is on.
    #
    # Resolved **per stage**, not from the global fallback. Reading only the
    # fallback was wrong in the shipped configuration rather than in some corner
    # of it: `thinking_budget` defaults to None while `stage_thinking` turns
    # reasoning off for correction, semantics and profile, so the estimate
    # applied a 6x multiplier to three stages that were never going to reason.
    # Over-reporting is the safe direction, but not by 6x on the stages that
    # dominate the bill — a user pushed to decline affordable work has been
    # misled exactly as much as one billed more than they approved.
    def add(stage: str, model: str, input_tokens: int, output_tokens: int) -> None:
        if settings.gemini.thinking_for(stage) != 0:
            output_tokens = int(
                output_tokens * THINKING_OUTPUT_MULTIPLIER.get(stage, 1.0)
            )
        # The spread widens the *output* only. Input is nearly deterministic —
        # the document's characters plus a per-call overhead, both known before
        # the run — and the 2026-08-21 measurement had it over-reporting already
        # at 15,509 estimated against 15,388 billed.
        high = int(output_tokens * OUTPUT_SPREAD.get(stage, 1.0))
        stages.append(
            StageEstimate(
                stage=stage,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usd=price_for(model, input_tokens, output_tokens),
                output_tokens_high=high,
                usd_high=price_for(model, input_tokens, high),
            )
        )

    # Calls, not characters, is what the prompt overhead multiplies by.
    correction_calls = max(1, -(-characters // CORRECTION_BATCH_CHARS))
    semantic_calls = max(1, chunk_count)

    # Learning is skipped entirely when the family already has a profile, so the
    # second document of a family is cheaper than the first — which is the whole
    # reason profiles are keyed on a family fingerprint rather than on a file.
    if options.learn_profile and (decision is None or decision.source != "reused"):
        add(
            "profile",
            settings.gemini.model,
            PROFILE_CALL_INPUT * PROFILE_MAX_ATTEMPTS,
            PROFILE_CALL_OUTPUT * PROFILE_MAX_ATTEMPTS,
        )
    if options.correct:
        add(
            "correction",
            settings.gemini.model,
            tokens + correction_calls * CORRECTION_CALL_OVERHEAD,
            int(tokens * CORRECTION_OUTPUT_RATIO),
        )
    if options.embed:
        # Embeddings carry no system prompt and no output charge.
        add("embedding", settings.gemini.embedding_model, tokens, 0)
    if options.extract_semantics:
        # One call per chunk — batching would make a relation unattributable —
        # so the overhead is paid once per chunk rather than once per document.
        #
        # A gleaning pass runs in the same conversation, which means it re-sends
        # the chunk *and* everything already extracted from it. Both legs scale
        # with the number of passes, not just the call overhead, and the worst
        # case is assumed: every round used, no early stop. At the default of
        # zero this reduces to exactly the single-pass figure.
        passes = 1 + max(0, settings.gemini.max_gleaning)
        add(
            "semantics",
            settings.gemini.model,
            (tokens + semantic_calls * SEMANTICS_CALL_OVERHEAD) * passes
            + semantic_calls * SEMANTICS_OUTPUT_PER_CHUNK * (passes - 1),
            semantic_calls * SEMANTICS_OUTPUT_PER_CHUNK * passes,
        )
    if options.extract_semantics and options.condense_descriptions:
        # **This figure has no measured baseline**, and the same caveat the
        # recall@5 target of 0.85 carries applies: it is reasoned from the
        # constants, not from a run.
        #
        # A concept only costs a call once its accumulated descriptions pass
        # `CONDENSE_CHARS`, which takes roughly `CONDENSE_SOURCE_CAP` chunks
        # describing the same concept — so calls are modelled as chunks over that
        # cap rather than as one per concept. One per concept would over-report by
        # orders of magnitude, and pushing a user to decline affordable work
        # misleads them exactly as much as billing more than they approved.
        condense_calls = max(1, chunk_count // CONDENSE_SOURCE_CAP)
        add(
            "semantics-condense",
            settings.gemini.model,
            condense_calls * (CONDENSE_INPUT_PER_CALL + SEMANTICS_CALL_OVERHEAD),
            condense_calls * CONDENSE_OUTPUT_PER_CALL,
        )
    if options.generate_evalset:
        # **One call per sampled chunk, not one call per document.** The previous
        # figure charged the whole document's tokens once and 20% of them as
        # output, which is the same class of miss this repository has already
        # measured twice: a generation call pays for its system instruction,
        # schema and JSON envelope *per call*, so the error scales with the call
        # count. On a 600-chunk book the old line under-reported the input by
        # roughly the overhead of forty calls and over-reported the output by an
        # order of magnitude — wrong in both directions at once.
        #
        # It had never mattered, because nothing implemented the stage.
        sample = EVAL_SAMPLE_TUNING if options.tune else EVAL_SAMPLE
        evalset_calls = min(sample, max(1, chunk_count))
        # The chunk this document actually has, not the cap — bounded by it.
        # `EVALSET_INPUT_CHARS` is right for a document at the chunker's ceiling
        # and 4.5x too big for one whose chunks run 580 characters, which is what
        # the first real run had: 11,456 input tokens quoted against 4,408 spent.
        # Over-reporting is the correct direction and *wildly* over-reporting is
        # the other failure the range exists to avoid.
        chunk_chars = characters / max(1, chunk_count)
        evalset_chars = min(EVALSET_INPUT_CHARS, chunk_chars + 2 * EVALSET_NEIGHBOUR_CHARS)
        add(
            "evalset",
            settings.gemini.model,
            evalset_calls
            * (int(evalset_chars / CHARS_PER_TOKEN) + SEMANTICS_CALL_OVERHEAD),
            evalset_calls * EVALSET_OUTPUT_PER_CALL,
        )
        if options.embed:
            # Measuring embeds every question once and every noise query once.
            # Small, and not nothing — and a stage that spends without a row is
            # how the ledger came to be missing every question ever asked.
            add(
                "evaluation",
                settings.gemini.embedding_model,
                (evalset_calls + NOISE_QUERIES) * EVAL_QUERY_TOKENS,
                0,
            )
            if options.tune:
                # **The whole document, embedded a second time.** A chunking
                # candidate re-cuts the text, so every chunk is a new string and
                # not one of them is in the cache. This is the largest single
                # line a gate can show and it is why tuning is off by default:
                # at the measured quota of ~6 embeddings a minute it is also
                # about a hundred minutes of wall clock for a 600-chunk book.
                #
                # The free half of a round — `min_score`, `per_section`,
                # dense-only — changes nothing in the index and costs only the
                # query embeddings already counted above.
                add("tuning", settings.gemini.embedding_model, tokens, 0)

    priced = [s.usd for s in stages if s.usd is not None]
    priced_high = [s.usd_high for s in stages if s.usd_high is not None]
    return Estimate(
        stages=stages,
        # None, not 0.0: "we cannot price this" and "this is free" are different
        # answers and the gate must not render one as the other.
        total_usd=sum(priced) if priced else None,
        total_usd_high=sum(priced_high) if priced_high else None,
        price_source=PRICE_SOURCE,
        unpriced_stages=[s.stage for s in stages if s.usd is None],
    )


def profile_dir(settings, tenant_id: str) -> "pathlib.Path":
    """This organisation's profile directory.

    `Paths.for_tenant` gives the legacy tenant the volume root itself, so the 23
    profiles already on disk keep working untouched — the same exemption, for the
    same reason, as the one `_salt()` makes for that tenant's ids. Every other
    organisation gets `tenants/<id>/profiles`, which is what stops a profile
    learned from one customer's book being applied to another's by structural
    fingerprint.

    Passed to `docagent.profiles` as an argument rather than arranged with a
    `chdir`: the worker runs activities concurrently, and `os.chdir` is
    process-global.
    """
    return settings.paths.for_tenant(tenant_id).profiles


@activity.defn(name="resolve_profile")
async def resolve_profile(
    version_id: str, run_id: str, extraction: Extraction, options: StageOptions
) -> ProfileDecision:
    """Find the rules this document's family already learned. Free.

    The engine keys a learned profile on a *structural* fingerprint — extractor,
    normalised repeating headers, page height bucket, heading-numbering depth —
    and deliberately excludes page count, filename and word counts so siblings
    share one profile. That is the whole value: the first document of a family
    pays for the exploration and every one after it is free.

    It also has a cost. Two unrelated families with the same layout collide
    exactly, and the second document is then chunked with the first one's heading
    rules. Retrieval metrics cannot see this — the synthetic eval questions are
    generated from the very chunks the wrong rules produced, so recall stays high
    while the chunking is wrong. There is no automatic fix and inventing one
    would be worse than the defect, so this reports and a person decides, with
    `StageOptions.ignore_profile` as the way out.

    Reuse happens here, before the gate, because it costs nothing. *Learning* a
    profile costs a generation call and lives in `paid.learn_profile`, on the
    other side of the gate with every other stage that spends.
    """
    from docagent import profiles as engine_profiles

    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)

    evidence = _Evidence(store.read_json(extraction.evidence))
    fingerprint = engine_profiles.fingerprint(evidence, extraction.extractor)
    decision = ProfileDecision(fingerprint=fingerprint)

    root = profile_dir(settings, extraction.tenant_id)
    matches = [
        p for p in engine_profiles.all_profiles(root) if p.fingerprint == fingerprint
    ]
    if not matches:
        return decision

    # `profiles.load` picks the newest of several files sharing a fingerprint,
    # and that tie-break is load-bearing: this corpus once left two files on one
    # fingerprint, a real one with eight measured revisions and a stale one with
    # every score at 0.0. Going through `load` rather than `matches[0]` is what
    # keeps the family's rules from depending on a filename.
    profile = engine_profiles.load(fingerprint, root)
    if profile is None:  # pragma: no cover - all_profiles and load disagree
        return decision

    rules = rules_from_profile(profile)
    warnings = [
        ProfileWarning(
            profile_id=p.slug,
            collides_with=p.learned_from or p.slug,
            similarity=_topical_overlap(p.learned_from, extraction, evidence),
            detail="",
        )
        for p in matches
    ]
    for w in warnings:
        w.detail = _collision_detail(w, fingerprint, options.ignore_profile)

    if options.ignore_profile:
        # The rules are dropped, the warnings are not: the record of what was
        # declined is part of knowing how this document was indexed.
        decision.warnings = warnings
        return decision

    # **Only for rules this document did not learn itself.** The check asks
    # whether *inherited* rules read a different structure than the built-in
    # ones, and a profile reused on the document it was learned from is not
    # inherited from anywhere — the disagreement is the profile doing its job.
    #
    # Measured 2026-08-31 on `01_RetoDeDios_INT-S.pdf`: its own learned pattern
    # reads 38 chapters where the defaults read 8, exactly the improvement
    # profiles exist to provide, and without this every re-index of a document
    # that had learned its own profile was blocked from activating. The
    # `default == 0` escape below covers the same case only when the built-in
    # detector finds *nothing*; here it found eight.
    inherited = profile.learned_from != (extraction.source_key or "")
    disagreement = (
        _heading_disagreement(store, extraction, rules) if inherited else ""
    )
    if disagreement:
        warnings.append(
            ProfileWarning(
                profile_id=profile.slug,
                collides_with=profile.learned_from or profile.slug,
                similarity=0.0,
                detail=disagreement,
                kind="heading_disagreement",
            )
        )

    decision.source = "reused"
    decision.slug = profile.slug
    decision.learned_from = profile.learned_from
    decision.revisions = profile.revisions
    decision.rules = rules
    decision.warnings = warnings
    _record_warnings(settings, version_id, warnings)
    return decision


def _collision_detail(w: ProfileWarning, fingerprint: str, ignored: bool) -> str:
    applied = (
        "Se han descartado por petición del usuario"
        if ignored
        else "Se le aplicarán sus reglas de encabezado y de troceado"
    )
    return (
        f"este documento comparte huella estructural ({fingerprint}) con el "
        f"perfil «{w.profile_id}», aprendido de "
        f"«{w.collides_with or 'origen desconocido'}». {applied}. "
        f"Solapamiento temático: {w.similarity:.0%}"
    )


def _heading_disagreement(
    store: ArtifactStore, extraction: Extraction, rules: ProfileRules
) -> str:
    """Warn when the inherited rules read a different structure than the defaults.

    `doc/CLAUDE.md` records this as the honest fix for a fingerprint collision:
    the fingerprint is structural and structure is not subject matter, so the
    only automatic signal available is that *the two rule sets disagree about how
    many chapters this document has*. A hermeneutics chapter inherited a
    church-history book's guards and the report read "4 chapters inherited vs 1
    read" — invisible to recall, obvious here.

    Local CPU on text already extracted, so it stays free and stays before the
    gate.
    """
    if extraction.structured:
        return ""
    from docagent.chunk import build_chunks, split_paragraphs

    data = store.read_bytes(extraction.text)
    paragraphs = split_paragraphs(data)
    inherited = _chapter_count(
        build_chunks(data, paragraphs, chunk_rules(rules), kind_classifier(rules))
    )
    default = _chapter_count(build_chunks(data, paragraphs))

    # Only a *contradiction* is worth a warning, not any difference.
    #
    # When the defaults find nothing, the profile is supplying structure the
    # built-in detector cannot see — a document whose headings carry no number.
    # That is the entire reason for learning a profile, and it is the common
    # case: a learned pattern turned 0 detected chapters into 9 on a real
    # document here. Warning about it would fire on every successful reuse, and a
    # warning that fires on success is one an operator learns to click past —
    # which is exactly how the collision it exists to catch gets missed.
    if default == 0 or inherited == default:
        return ""
    return (
        f"las reglas heredadas detectan {inherited} capítulo(s) donde las reglas "
        f"por defecto detectan {default}. Una huella estructural idéntica no "
        f"implica la misma materia: revise el índice antes de indexar, o marque "
        f"«ignorar perfil»"
    )


def _chapter_count(chunks: list) -> int:
    return len({c.chapter for c in chunks if c.chapter})


def _record_warnings(settings, version_id: str, warnings: list[ProfileWarning]) -> None:
    """Persist the warnings, best-effort and unpooled.

    Unpooled for the reason artifact recording is: a pool retries a refused
    connection in the background, so a catalog that is merely down turns a
    bookkeeping write into a full-timeout stall rather than one immediate error.
    Measured elsewhere in this file at 2 seconds against 99.

    And best-effort, because the warnings also travel to the gate in the
    `ProfileDecision` this activity returns. Losing the row is bad; failing the
    stage that produced the warning — and so never showing it to anyone — is
    worse.
    """
    if not warnings:
        return
    try:
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            for w in warnings:
                catalog.record_profile_warning(
                    version_id=version_id,
                    profile_id=w.profile_id,
                    collides_with=w.collides_with,
                    similarity=w.similarity,
                    detail=w.detail,
                )
    except Exception as e:
        log.warning("could not record profile warnings for %s: %s", version_id, e)


# --- translating between the engine's rules and the payload ------------------


def rules_from_profile(profile) -> ProfileRules:
    """Flatten a `docagent.profiles.Profile` to the fields that get applied.

    Deliberately field-by-field rather than `asdict`: a profile also carries an
    eval set, scores and a tuning history, and those must not reach a Temporal
    payload that persists for the namespace's whole retention period.
    """
    d, c = profile.doc_rules, profile.chunk_rules
    return ProfileRules(
        header_patterns=list(d.header_patterns),
        footer_cutoff=d.footer_cutoff,
        line_gap_factor=d.line_gap_factor,
        target_chars=c.target_chars,
        hard_cap_chars=c.hard_cap_chars,
        overlap_chars=c.overlap_chars,
        min_chunk_chars=c.min_chunk_chars,
        max_embed_chars=c.max_embed_chars,
        question_chars_per_mark=c.question_chars_per_mark,
        heading_l1_max=c.heading_l1_max,
        heading_l2_max=c.heading_l2_max,
        heading_l1_pattern=c.heading_l1_pattern,
        heading_l2_pattern=c.heading_l2_pattern,
        question_pattern=profile.question_pattern,
        footnote_pattern=profile.footnote_pattern,
    )


def doc_rules(rules: ProfileRules | None):
    """The extraction half. `None` gives the engine's measured defaults."""
    from docagent.chunk import DocRules

    if rules is None:
        return DocRules()
    return DocRules(
        header_patterns=tuple(rules.header_patterns),
        footer_cutoff=rules.footer_cutoff,
        line_gap_factor=rules.line_gap_factor,
    )


def chunk_rules(rules: ProfileRules | None):
    """The chunking half. `None` gives the engine's measured defaults."""
    from docagent.chunk import ChunkRules

    if rules is None:
        return ChunkRules()
    return ChunkRules(
        target_chars=rules.target_chars,
        hard_cap_chars=rules.hard_cap_chars,
        overlap_chars=rules.overlap_chars,
        min_chunk_chars=rules.min_chunk_chars,
        max_embed_chars=rules.max_embed_chars,
        question_chars_per_mark=rules.question_chars_per_mark,
        heading_l1_max=rules.heading_l1_max,
        heading_l2_max=rules.heading_l2_max,
        heading_l1_pattern=rules.heading_l1_pattern,
        heading_l2_pattern=rules.heading_l2_pattern,
    )


def kind_classifier(rules: ProfileRules | None):
    """A `build_chunks` classifier for the profile's learned kind patterns.

    Passed *into* the chunker rather than applied to its output, because a change
    of kind is a chunk boundary: relabelling finished chunks can only rename ones
    the default rules already cut. A family marking its review questions "P1"
    rather than "1." had them merged into the preceding prose, and the relabel
    then read that prose's first paragraph and called the whole thing body text.

    Goes through `docagent.rules.classify_with` rather than reimplementing the
    fallback, so an unproposed half still lands on the built-in classifier —
    which was itself measured, and is a known-good default rather than a gap.
    None when nothing was learned, so the chunker keeps its own rules.
    """
    if rules is None or not (rules.question_pattern or rules.footnote_pattern):
        return None
    from docagent.rules import classify_with

    q, f = rules.question_pattern, rules.footnote_pattern
    return lambda text, engine_rules: classify_with(text, engine_rules, q, f)


class _Evidence:
    """Adapts the stored evidence JSON to what the engine reads off it.

    Rebuilt from the stored artifact rather than imported as the engine's own
    dataclass, so nothing about `docagent.extract.Evidence` leaks into a Temporal
    payload — but it has to carry **every** field its two readers touch, not just
    the four `profiles.fingerprint` needs. `rules.propose` reads `pages`,
    `numbered_paragraphs`, `last_lines` and `short_lines` as plain attributes,
    and a narrower adapter fails there with an `AttributeError` on the first real
    learning run rather than anywhere near its cause.
    """

    def __init__(self, data: dict) -> None:
        self.source = data.get("source", "") or ""
        self.extractor = data.get("extractor", "") or ""
        self.pages = data.get("pages", 0) or 0
        self.repeated_lines = data.get("repeated_lines", {}) or {}
        self.first_lines = data.get("first_lines", []) or []
        self.last_lines = data.get("last_lines", []) or []
        self.numbered_lines = data.get("numbered_lines", []) or []
        self.numbered_paragraphs = data.get("numbered_paragraphs", []) or []
        self.short_lines = data.get("short_lines", []) or []
        self.page_height = data.get("page_height", 0.0) or 0.0
        self.median_line_gap = data.get("median_line_gap", 0.0) or 0.0
        self.pages_without_text = data.get("pages_without_text", []) or []
        self.notes = data.get("notes", []) or []


_WORD = __import__("re").compile(r"[^\W\d_]{4,}", __import__("re").UNICODE)


def _topical_overlap(learned_from: str, extraction: Extraction, evidence: _Evidence) -> float:
    """Jaccard overlap between the two documents' visible wording.

    Crude on purpose. The question it answers is not "how similar are these
    texts" but "is there any reason to believe these are the same family",
    and the filename plus the repeating header lines are the only signals
    available for the *other* document without re-reading it.
    """
    mine = _tokens(" ".join(evidence.repeated_lines) + " " + " ".join(evidence.first_lines[:20]))
    theirs = _tokens(pathlib.Path(learned_from).stem.replace("-", " ").replace("_", " "))
    if not mine or not theirs:
        # No basis to judge. Reporting 0.0 would assert "unrelated", which is a
        # stronger claim than the evidence supports.
        return 0.0
    return len(mine & theirs) / len(mine | theirs)


def _tokens(text: str) -> set[str]:
    return {m.group(0).casefold() for m in _WORD.finditer(text)}


# ---------------------------------------------------------------------------
# Post-gate stages
# ---------------------------------------------------------------------------


def _heading_titles(row: dict) -> tuple[str, ...]:
    """The heading path a chunk sits under, outermost first.

    The engine stores the chapter separately and joins deeper levels with
    " > " into one `section` string, so reconstructing the hierarchy means
    splitting that back apart rather than treating it as a single title.
    """
    parts: list[str] = []
    if chapter := (row.get("chapter") or "").strip():
        parts.append(chapter)
    for piece in (row.get("section") or "").split(" > "):
        if piece := piece.strip():
            parts.append(piece)
    return tuple(parts)


def section_tree(
    rows: list[dict],
) -> tuple[dict[tuple[str, ...], "proj.SectionNode"], dict[tuple[str, ...], tuple[int, ...]]]:
    """Assign each heading an ordinal path that preserves its nesting.

    Ordinals are counted *per parent*, not globally. Counting globally is the
    obvious shortcut and it is wrong in two ways at once: the second-level
    heading under chapter 1 comes out as `2.1` instead of `1.1`, and its parent
    path `(2,)` names a section that does not exist — so the `CONTAINS` edge is
    never written and the document has no navigable outline despite having
    headings.

    Keyed on the title path rather than the title, because titles repeat:
    "Introducción" appears once per part in most of this corpus.
    """
    nodes: dict[tuple[str, ...], proj.SectionNode] = {}
    paths: dict[tuple[str, ...], tuple[int, ...]] = {}
    counters: dict[tuple[int, ...], int] = {}

    for row in rows:
        titles = _heading_titles(row)
        for depth in range(1, len(titles) + 1):
            key = titles[:depth]
            if key in paths:
                continue
            parent = paths[key[:-1]] if depth > 1 else ()
            counters[parent] = counters.get(parent, 0) + 1
            paths[key] = parent + (counters[parent],)
            nodes[key] = proj.SectionNode(
                path=paths[key], title=key[-1], level=depth
            )
    return nodes, paths


@activity.defn(name="link_duplicate")
async def link_duplicate(
    request: IngestRequest, staged: Staged, registered: Registered
) -> None:
    """Give the graph the second path to already-indexed content.

    The duplicate short-circuit returns before `project_structure`, which is the
    activity that creates the `Document` node — so without this the catalog knows
    about two paths and the graph knows about one, and the Library screen (which
    reads the graph) silently omits the copy.

    Only the document node and the `HAS_VERSION` edge are written. Re-projecting
    the chunks is exactly what the short-circuit exists to avoid.

    It also *activates* the version for the new document. That is easy to miss:
    the short-circuit returns before `activate_version`, so without this the
    duplicate is linked to a fully indexed version and still reports "not yet
    active" forever — visible in the Library as a second copy that never becomes
    answerable.
    """
    settings = _settings()
    with Catalog(settings.database_url) as catalog:
        catalog.activate(registered.document_id, registered.version_id)

    version = proj.VersionNode(
        library=request.library_id,
        source_key=request.source_key,
        content_sha256=staged.content_sha256,
        title=staged.title,
        tenant_id=request.tenant_id,
        author=request.author,
        fmt=staged.fmt,
    )
    with Graph(settings.memgraph_url) as graph:
        graph.ensure_schema()
        proj.project_structure(graph, version)


@activity.defn(name="project_structure")
async def project_structure(
    request: IngestRequest,
    staged: Staged,
    registered: Registered,
    run_id: str,
    chunks_ref: ArtifactRef,
) -> dict[str, int]:
    """Write the document's structure and citations into the graph.

    Free, deterministic, and separate from semantic extraction on purpose: a
    failed extraction then leaves a document that can still be browsed, cited
    and searched, rather than one that is absent from the library entirely.
    """
    settings = _settings()
    store = ArtifactStore(settings.workspace, run_id)
    rows = list(store.iter_jsonl(chunks_ref))

    nodes, paths = section_tree(rows)

    chunks = [
        proj.ChunkNode(
            ordinal=row.get("index", i),
            kind=row.get("kind", "cuerpo"),
            text=row.get("text", ""),
            char_start=row.get("char_from", 0),
            char_end=row.get("char_to", 0),
            section_path=paths.get(_heading_titles(row)),
            page=row.get("page"),
            # `sheet` and `slide` were declared on ChunkNode, written by
            # `_MERGE_CHUNKS` and rendered by `_locator` — and set by nothing,
            # because no row schema carried them. Read them here rather than
            # adding `start_s` beside two fields with the same bug.
            sheet=row.get("cell_ref") or None,
            slide=row.get("slide"),
            start_s=row.get("start_s"),
            end_s=row.get("end_s"),
        )
        for i, row in enumerate(rows)
    ]

    version = proj.VersionNode(
        library=request.library_id,
        source_key=request.source_key,
        content_sha256=staged.content_sha256,
        title=staged.title,
        tenant_id=request.tenant_id,
        author=request.author,
        fmt=staged.fmt,
        sections=tuple(nodes.values()),
        chunks=tuple(chunks),
    )

    with Graph(settings.memgraph_url) as graph:
        graph.ensure_schema()
        return proj.project_structure(graph, version)


@activity.defn(name="activate_version")
async def activate_version(
    request: IngestRequest, staged: Staged, registered: Registered
) -> None:
    """Make this the version questions see — and only once everything landed.

    Catalog first, then graph. If the process dies between them the document is
    answerable from a graph whose `active` flag is stale, which re-projection
    repairs; the reverse order would make a version answerable in the graph that
    the catalog does not consider indexed, which nothing repairs automatically.
    """
    settings = _settings()
    with Catalog(settings.database_url) as catalog:
        catalog.activate(registered.document_id, registered.version_id)

    version = proj.VersionNode(
        library=request.library_id,
        source_key=request.source_key,
        content_sha256=staged.content_sha256,
        title=staged.title,
        tenant_id=request.tenant_id,
    )
    with Graph(settings.memgraph_url) as graph:
        proj.activate(graph, version)


@activity.defn(name="open_run")
async def open_run(opening: RunOpen) -> None:
    """Open the catalog row before anything can fail against it.

    Both ingest workflows call this first. Until they did, the row was created by
    `register_document` — the *second* activity — so a failure in `probe_video`
    or `stage_source` had nothing to be recorded against: `finish_run` is a bare
    UPDATE, zero rows affected raises nothing, and the run vanished. Measured in
    production on 2026-09-05, on a video YouTube refused from the EC2 egress IP:
    the workflow failed in 2.8 s, `record_run_outcome` reported Completed, and
    the import queue showed nothing at all.

    The pattern is `asking.start_question_run`'s, which opens a question's row
    before it asks for exactly the same reason and with the same caveat about
    what a missing row costs downstream — `record_cost`, `record_artifact` and
    `_insert_event` all derive their tenant from the run, so no row means no
    bookkeeping of any kind.

    **Best-effort, like every other write in this file.** An import that has not
    spent anything must not fail because the catalog is blinking; a run that then
    dies invisibly is no worse than what happened before this existed. Idempotent
    at the database — `start_run` is `ON CONFLICT (id) DO NOTHING` — so a
    Temporal retry writes nothing twice and `register_document`'s own call
    remains a no-op that `attach_version` immediately completes.
    """
    settings = _settings()
    try:
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            catalog.start_run(
                run_id=opening.run_id,
                workflow_id=opening.workflow_id,
                kind=opening.kind,
                tenant_id=opening.tenant_id,
                library_id=opening.library_id,
                label=opening.label,
            )
    except Exception as e:  # noqa: BLE001 - bookkeeping never fails its stage
        log.warning("could not open the run row for %s: %s", opening.run_id, e)


@activity.defn(name="record_run_outcome")
async def record_run_outcome(
    run_id: str,
    state: str,
    error_kind: str | None = None,
    error_detail: str | None = None,
    seq: int | None = None,
    at: datetime | None = None,
    stage: str | None = None,
) -> None:
    """Close the run, and close its last stage in the same statement pair.

    `seq`, `at` and `stage` come from the workflow — never from here. See
    `Catalog._insert_event`: the workflow's counter and `workflow.now()` are what
    make a retry a no-op, and a timestamp taken here would move under one.
    """
    settings = _settings()
    with Catalog(settings.database_url) as catalog:
        closed = catalog.finish_run(
            run_id,
            state,
            error_kind=error_kind,
            error_detail=error_detail,
            seq=seq,
            at=at,
            stage=stage,
        )
    if not closed:
        # An UPDATE that matched nothing. This is what a failure before the run
        # row existed used to look like from here — Completed, having written
        # nothing — and `open_run` is what removed the cause. Say so rather than
        # ticking; the run is about to fail and the catalog will not know why.
        log.warning(
            "closed no run row for %s (%s): the row does not exist", run_id, state
        )


@activity.defn(name="set_run_stage")
async def set_run_stage(
    run_id: str,
    stage: str,
    state: str | None = None,
    seq: int | None = None,
    at: datetime | None = None,
) -> None:
    settings = _settings()
    with Catalog(settings.database_url) as catalog:
        catalog.set_run_stage(run_id, stage, state=state, seq=seq, at=at)


@activity.defn(name="record_run_events")
async def record_run_events(run_id: str, events: list[dict[str, Any]]) -> None:
    """Flush the transitions that happened before the run row existed.

    Takes dicts rather than `RunEvent`s because the workflow buffers them before
    any dataclass the catalog owns is in scope, and because a payload that
    crosses the converter is better off being the plain shape it will be
    reassembled from anyway.
    """
    settings = _settings()
    with Catalog(settings.database_url) as catalog:
        catalog.record_run_events(
            run_id,
            [
                RunEvent(
                    seq=int(e["seq"]),
                    at=e["at"] if isinstance(e["at"], datetime)
                    else datetime.fromisoformat(str(e["at"])),
                    stage=str(e["stage"]),
                    outcome=e.get("outcome"),
                    detail=e.get("detail"),
                )
                for e in events
            ],
        )
