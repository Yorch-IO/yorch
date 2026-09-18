"""One vocabulary for a stage, which this codebase spells three different ways.

A run's audit trail has to answer "what did this stage do, and what did it
cost", and until this module existed that question could not be asked, because
the three things it joins are three unrelated sets of string literals scattered
across their use sites:

- **What the workflow calls itself.** ``IngestWorkflow._stage``: ``learning``,
  ``correcting``, ``evaluating``. Written to ``run.stage`` and answered to the
  ``stage`` query, so these strings are already in Postgres and on the wire.
- **What a charge calls itself.** ``Spend.stage`` and therefore
  ``cost_entry.stage``: ``profile``, ``correction``, ``evalset``,
  ``evaluation``. The estimator uses the same set, deliberately, so a gate's
  estimate row and a billed row can be compared — and ``thinking_for()``,
  ``THINKING_OUTPUT_MULTIPLIER`` and ``OUTPUT_SPREAD`` are all keyed on it.
- **What an artifact calls itself.** ``artifacts.KINDS``: ``proposal``,
  ``corrected_text``, ``scores_candidate``. Attributed to a stage only
  implicitly, by which activity happens to call ``_record``.

The overlap between the first two is three words — ``embedding``, ``tuning``,
``semantics`` — and every other pairing has to be looked up. **Nothing is
renamed here**, and that is the point: ``learning`` is what is in the ``stage``
column of every run this installation has ever done, and ``correction`` is what
is in every ``cost_entry`` row. Renaming either would make the history unreadable
to fix a tidiness problem. The mapping is the fix.

Mirrored in ``../yorch-tauri-backend/src/runs/stages.ts``, which serves the same
audit payload for the paid plane; ``tests/test_stages.py`` and that repository's
parity spec are what keep the two from drifting.
"""

from __future__ import annotations

#: Every stage :class:`IngestWorkflow` names, in the order a reader expects.
#:
#: **This is a display order, not a schedule.** ``extracting`` runs a second
#: time when a learned profile needs the text re-read; ``tuning`` runs only when
#: the gate asked for it; ``blocked`` and ``done`` are mutually exclusive. What
#: actually happened in one run is that run's own ``run_event`` rows, ordered by
#: ``seq`` — this tuple is what tells a UI where to put a stage it has been
#: handed, and what tells a test that a new stage was added without an event.
INGEST_STAGES: tuple[str, ...] = (
    "staging",
    "registering",
    "extracting",
    "profiling",
    "previewing",
    "awaiting_approval",
    "learning",
    "correcting",
    "awaiting_correction_review",
    "chunking",
    "projecting",
    "epub",
    "embedding",
    "evaluating",
    "tuning",
    "semantics",
    "blocked",
    "activating",
    "done",
)

#: :class:`RebuildWorkflow`'s own set. Shorter, and only two names are shared
#: with the ingest list by accident of doing the same work.
REBUILD_STAGES: tuple[str, ...] = (
    "loading",
    "estimating",
    "awaiting_approval",
    "embedding",
    "projecting",
    "replaying semantics",
    "activating",
    "done",
)

#: :class:`VideoIngestWorkflow`'s own set.
#:
#: **A display order, not a schedule**, and two stages here move between the two
#: transcript sources — the same caveat ``INGEST_STAGES`` makes about
#: ``extracting`` running twice:
#:
#: - When the video has captions, ``grouping`` runs *before* ``previewing``, so
#:   the gate can quote correction from the real character count and show a real
#:   chunk preview. ``fetching`` and ``transcribing`` never happen and nothing
#:   is charged for the transcript.
#: - When it does not, ``previewing`` quotes from the duration alone, and
#:   ``fetching`` (audio to S3) then ``transcribing`` (the Amazon Transcribe job)
#:   run *after* approval, with ``grouping`` after them.
#:
#: ``probing`` and ``registering`` precede the ``run`` row for the reason
#: ``IngestWorkflow`` buffers its own first two: ``_insert_event`` derives its
#: tenant from that row, so an event written before it is silently dropped.
VIDEO_STAGES: tuple[str, ...] = (
    "probing",
    "registering",
    "grouping",
    "previewing",
    "awaiting_approval",
    "fetching",
    "transcribing",
    "correcting",
    "chunking",
    "projecting",
    "epub",
    "embedding",
    # Added after the first real video shipped with an empty Graph screen.
    # `library_mentions` derives its node list from `MENTIONS` edges and those
    # come from semantic extraction, so a workflow with no `semantics` stage
    # produces a document that is indexed, citable, and invisible on the canvas
    # — which is indistinguishable, to a reader, from one that failed to index.
    # It stays **off in `_recommended`**: the stage exists so it can be ticked,
    # and defaults off because it nearly triples the bill ($0.2258 -> $0.6532 on
    # a 76-minute talk) and its value on speech is unmeasured.
    "semantics",
    "activating",
    "done",
)

#: One recording out of a customer's S3 bucket.
#:
#: `VIDEO_STAGES` minus `grouping` before the gate — an object has no captions
#: to group, so there is nothing to preview until the money is spent — plus
#: `archiving` after `transcribing`, where the raw transcript is written back
#: into the customer's own bucket so the next import of these bytes pays no
#: ASR. `check_archive` runs at the head of `fetching` and, when it finds one,
#: the run enters `transcribing` with a detail saying so and skips straight to
#: `grouping`: the `transcription_result` artifact is attributed to
#: `transcribing` on both paths, which is what keeps the map below honest.
AUDIO_STAGES: tuple[str, ...] = (
    "probing",
    "registering",
    "previewing",
    "awaiting_approval",
    "fetching",
    "transcribing",
    "archiving",
    "grouping",
    "correcting",
    "chunking",
    "projecting",
    "epub",
    "embedding",
    "semantics",
    "activating",
    "done",
)

#: Reading a channel: what its videos are about, before any of them is indexed.
#:
#: **A display order, not a schedule**, and here the two halves never run in one
#: execution: `ChannelDiscoverWorkflow` quotes and preselects, and
#: `ChannelTopicsWorkflow` quotes and reads. They are one `run.kind` and one
#: vocabulary because they are one act from the reader's side — "work out which
#: of these are worth paying for" — and because splitting the kind would mean a
#: second migration to say the same thing.
#:
#: `quoting` writes the `estimate` artifact, which is why this path needs an
#: entry in `ARTIFACT_STAGE_OVERRIDES`: that artifact is mapped to `previewing`
#: by default, and `previewing` is not a stage a channel run has. It is the same
#: mismatch `rebuild` records there, found the same way — by writing the file.
CHANNEL_STAGES: tuple[str, ...] = ("quoting", "preselecting", "reading", "done")

#: The two stages :mod:`brainworker.removal` walks, in the order it walks them.
#:
#: That order is not cosmetic — ``removal.py`` owns it precisely so no caller can
#: get it wrong: projections first, catalog last, because the catalog is the
#: source of truth and a crash after its row is gone leaves points and nodes
#: nothing can find again to retry.
REMOVAL_STAGES: tuple[str, ...] = ("projections", "catalog")

ACTIVATION_STAGES: tuple[str, ...] = ("activating",)

#: The standalone build, for a version indexed before the stage existed.
#:
#: ``epub`` and not ``building``, and that is the whole decision: the stage is
#: named identically on all three paths that write the artifact, so
#: ``ARTIFACT_STAGES`` — which is keyed by artifact name alone and can therefore
#: name one writer — is right for every one of them and needs no override. The
#: alternative was a third entry in ``ARTIFACT_STAGE_OVERRIDES``, which exists
#: because ``evidence`` was attributed to a stage a video run does not have and
#: spent months rendering under the trailing ``stage: null`` heading.
EPUB_STAGES: tuple[str, ...] = ("epub", "done")

#: Recasting a document into another literary genre.
#:
#: Two gates, which no other pipeline here has for this reason: the first quote
#: is arithmetic over the source's own character and chapter counts — all that
#: is knowable before a model has seen it — while the outline, and therefore the
#: real chapter count, exists only after `planning`. So `awaiting_approval`
#: buys the planning calls and `awaiting_plan_review` buys the composition,
#: which is where essentially the whole bill is. The shape is
#: `awaiting_correction_review`'s: look at what the expensive-to-undo step
#: produced before paying for the next one.
TRANSFORM_STAGES: tuple[str, ...] = (
    "reading",
    "probing",
    "previewing",
    "awaiting_approval",
    "planning",
    "awaiting_plan_review",
    "composing",
    "writing",
    "done",
)
# `analysing` and `binding` were in this tuple and are not, and the reason is
# worth keeping: both named work that happens *inside* another stage's single
# activity — detecting the source genre inside `planning`, rendering the
# bibliography inside `writing` — so no transition could ever enter them. A
# stage in the trail that nothing can be in is not documentation, it is a
# duration attributed to the wrong place, and this trail already has one of
# those it could not fix.

#: What a run can end as. Constrained in ``run_state_check``, which Prisma owns.
#:
#: ``blocked`` is a real outcome and not a failure: the run did every stage and
#: paid for them, and withheld only the activation. ``cancelled`` covers a gate
#: that timed out, which costs nothing.
TERMINAL_OUTCOMES: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled", "blocked"}
)

#: Workflow stage -> the ``cost_entry.stage`` values charged while it runs.
#:
#: One-to-many because ``evaluating`` covers both halves of the measurement —
#: generating the questions and answering them are separately priced — and
#: because condensing a concept's description is billed apart from extracting
#: it, having its own estimator entry for the same reason.
#:
#: A workflow stage absent from this map spends nothing, which is a claim worth
#: making explicitly: it is what lets the audit view render ``cost: null`` for
#: a free stage rather than a zero somebody would read as "the charge was lost".
COST_STAGES: dict[str, tuple[str, ...]] = {
    #: Recasting a document. `probing` is four thousandths of a cent of query
    #: embeddings and is here anyway: a stage that spends without a row is
    #: exactly how the ledger came to be missing every question ever asked, and
    #: `ask-embedding` was invisible for months by being too small to notice.
    #: `composing` covers both halves of what a chapter costs — the query
    #: embeddings its research makes and the call that writes it — because they
    #: are charged separately and a reader of the ledger wants them apart.
    "probing": ("transform-probe",),
    #: Both halves of planning, because one activity makes both calls: the genre
    #: analysis and the outline proposals. Splitting them across two workflow
    #: stages would put a duration on a stage nothing can be in.
    "planning": ("transform-genre", "transform-plan"),
    "composing": ("transform-research", "transform-compose"),
    "learning": ("profile",),
    "correcting": ("correction",),
    "embedding": ("embedding",),
    "evaluating": ("evalset", "evaluation"),
    "tuning": ("tuning",),
    "semantics": ("semantics", "semantics-condense"),
    "replaying semantics": ("semantics-replay",),
    # The first charge in this product that is not tokens times a third-party
    # multiplier: Amazon Transcribe bills per second of audio, so the figure is
    # exact rather than projected. It is absent entirely on the caption path,
    # where the stage never runs — which is what makes `cost: null` there mean
    # "free", not "the charge was lost".
    "transcribing": ("transcription",),
    # One call, and only when the catalog has no author for the document. A
    # video never pays it: `register_video` already fills the author from the
    # channel. So this stage is charged on a minority of the runs that enter it,
    # which is why the estimate over-reports rather than skipping the line —
    # over-reporting is the direction the gate's rule permits.
    "epub": ("epub-metadata",),
    # Reading a channel. Both are classification over text that was handed to
    # them, both have reasoning off in `stage_thinking`, and neither has an
    # entry in `THINKING_OUTPUT_MULTIPLIER` for the same reason `embedding` does
    # not: there is nothing measured to put there.
    "preselecting": ("channel-preselect",),
    "reading": ("channel-topics",),
}

#: ``cost_entry.stage`` -> the workflow stage that charged it.
#:
#: Built by inversion so the two can never disagree. A cost row whose stage is
#: not in here belongs to no workflow stage and **must not be dropped** — the
#: three a question charges (``planning``, ``ask-embedding``, ``answering``)
#: have no stage vocabulary at all, because asking has no gate and therefore no
#: pipeline to name. The audit view groups them under no stage rather than
#: losing them, which is what keeps the ledger's total equal to ``total_cost``.
STAGE_FOR_COST: dict[str, str] = {
    cost: stage for stage, costs in COST_STAGES.items() for cost in costs
}

#: The cost stages a question charges, which belong to no pipeline stage.
#:
#: `channel-synthesis` is here rather than in `COST_STAGES` because a channel
#: *question* is an `ask` run and not a pipeline: it has no gate, so it has no
#: stage vocabulary to belong to, exactly like the three above it.
ASK_COST_STAGES: tuple[str, ...] = (
    "planning",
    "ask-embedding",
    "answering",
    "channel-synthesis",
)

#: ``run_artifact.name`` -> the workflow stage that wrote it.
#:
#: Derived by reading the ``_record`` call sites rather than by adding a column,
#: because ``run_artifact`` is upserted on ``(run_id, name)`` and a stage column
#: would have to be maintained at fifteen call sites to say what the call site
#: already says. Two consequences a reader of the audit view needs:
#:
#: - ``profile`` is written three times while learning and keeps only the last,
#:   so the list is a final state and not a history.
#: - ``_record`` is best-effort and swallows failures, so a missing row does not
#:   mean the stage produced nothing.
ARTIFACT_STAGES: dict[str, str] = {
    "evidence": "extracting",
    "raw_text": "extracting",
    "extracted_text": "extracting",
    "structured_chunks": "extracting",
    "preview_chunks": "previewing",
    "estimate": "previewing",
    "proposal": "learning",
    "validation": "learning",
    "profile": "learning",
    "corrected_text": "correcting",
    "correction_report": "correcting",
    "chunks": "chunking",
    "evalset": "evaluating",
    "scores": "evaluating",
    "scores_candidate": "tuning",
    "tuning": "tuning",
    "semantics": "semantics",
    "ledger": "done",
    "events": "done",
    # The video path. Each of these is written by exactly one stage, which is
    # why `grouping` exists as a stage of its own rather than being folded into
    # whichever half produced the cues: the transcript is built the same way
    # from either source, and an artifact attributed to two stages would make
    # this map a lie on one of the two paths.
    "video_probe": "probing",
    "audio_probe": "probing",
    "captions": "probing",
    "transcription_result": "transcribing",
    "transcript": "grouping",
    "transcript_text": "grouping",
    # Written by a stage of the same name on all three paths. See EPUB_STAGES.
    "epub": "epub",
    # Reading a channel.
    "preselection": "preselecting",
    "topics": "reading",
    # Recasting a document. Four artifacts, four writers, no two of them the
    # same stage — so no `ARTIFACT_STAGE_OVERRIDES` entry is needed for any of
    # them, and `estimate` needs none either because this path's quoting stage
    # is called `previewing`, which is the name this map already carries.
    "transform_plan": "planning",
    "transform_draft": "composing",
    "transform_continuity": "composing",
    "transform": "writing",
    "transform_report": "writing",
}


def stage_of_cost(cost_stage: str) -> str | None:
    """Which workflow stage charged this, or ``None`` for one that names none."""
    return STAGE_FOR_COST.get(cost_stage)


#: Where `ARTIFACT_STAGES` is wrong because the artifact has two writers.
#:
#: `evidence` is the one artifact both paths produce: `extract` writes it for a
#: document and `group_transcript` writes it for a video. The map above is keyed
#: by artifact name alone, so it can only name one of them — and it names
#: `extracting`, which is **not a stage a video run has**. The consequence was
#: visible in every video's run detail: `auditlog.build` found no `extracting`
#: row to attach it to and rendered it under the trailing `stage: null`
#: heading, beside charges that belong to nobody.
#:
#: That is precisely the lie `grouping` was made a stage of its own to avoid —
#: "an artifact attributed to two stages would make this map a lie on one of
#: the two paths". The map was right to refuse to hold both; what was missing
#: was somewhere for the second one to live.
#:
#: Keyed by `run.kind`, because that is what a client already branches on to
#: know which gate shape to expect, and it is on the row the ledger is built
#: from.
ARTIFACT_STAGE_OVERRIDES: dict[str, dict[str, str]] = {
    "video": {"evidence": "grouping"},
    # The same grouper writes it on the bucket path.
    "audio": {"evidence": "grouping"},
    # `estimate` is written by whichever stage quotes, and that stage is named
    # `previewing` on the ingest path and `estimating` on the rebuild one. Found
    # by adding the writer: the artifact had never been written, so the
    # mismatch could not have shown up before there was a file to misplace.
    "rebuild": {"estimate": "estimating"},
    # And `quoting` on a channel run, for the same reason: one artifact, three
    # writers, and this map holds one.
    "channel": {"estimate": "quoting"},
}


def stage_of_artifact(name: str, kind: str | None = None) -> str | None:
    """Which workflow stage wrote this artifact, or ``None`` if unrecognised.

    ``kind`` is the run's kind. Optional so that every existing caller keeps
    working, and consulted first so that a path which writes an artifact from
    somewhere else than the default gets the right answer rather than a
    plausible one.
    """
    if kind and name in (over := ARTIFACT_STAGE_OVERRIDES.get(kind, {})):
        return over[name]
    return ARTIFACT_STAGES.get(name)


def order_of(stage: str, stages: tuple[str, ...] = INGEST_STAGES) -> int:
    """Where a stage sorts for display; unknown stages sort last, not first.

    Last rather than first on purpose: a stage this module has not been taught
    about is newer than the ones it has, and burying it at the top of the table
    is how it would go unnoticed.
    """
    try:
        return stages.index(stage)
    except ValueError:
        return len(stages)


def as_json() -> str:
    """Dump the vocabulary, so the TypeScript port can be checked against it.

    Live at test time rather than into a committed fixture, for the reason
    `queries.parity.spec.ts` records about its own: a snapshot only detects
    drift if something forces it to be refreshed, and nothing does. The paid
    plane's `stages.parity.spec.ts` shells out to this.
    """
    import json

    return json.dumps(
        {
            "ingest_stages": list(INGEST_STAGES),
            "rebuild_stages": list(REBUILD_STAGES),
            "video_stages": list(VIDEO_STAGES),
            "audio_stages": list(AUDIO_STAGES),
            "removal_stages": list(REMOVAL_STAGES),
            "activation_stages": list(ACTIVATION_STAGES),
            "epub_stages": list(EPUB_STAGES),
            "transform_stages": list(TRANSFORM_STAGES),
            "channel_stages": list(CHANNEL_STAGES),
            "terminal_outcomes": sorted(TERMINAL_OUTCOMES),
            "cost_stages": {k: list(v) for k, v in COST_STAGES.items()},
            "ask_cost_stages": list(ASK_COST_STAGES),
            "artifact_stages": dict(ARTIFACT_STAGES),
            "artifact_stage_overrides": {
                k: dict(v) for k, v in ARTIFACT_STAGE_OVERRIDES.items()
            },
        },
        indent=2,
        sort_keys=True,
    )


if __name__ == "__main__":  # pragma: no cover - a dump, not behaviour
    print(as_json())
