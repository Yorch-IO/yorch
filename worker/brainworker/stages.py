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
    "embedding",
    "activating",
    "done",
)

#: The two stages :mod:`brainworker.removal` walks, in the order it walks them.
#:
#: That order is not cosmetic — ``removal.py`` owns it precisely so no caller can
#: get it wrong: projections first, catalog last, because the catalog is the
#: source of truth and a crash after its row is gone leaves points and nodes
#: nothing can find again to retry.
REMOVAL_STAGES: tuple[str, ...] = ("projections", "catalog")

ACTIVATION_STAGES: tuple[str, ...] = ("activating",)

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
ASK_COST_STAGES: tuple[str, ...] = ("planning", "ask-embedding", "answering")

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
    "captions": "probing",
    "transcription_result": "transcribing",
    "transcript": "grouping",
    "transcript_text": "grouping",
}


def stage_of_cost(cost_stage: str) -> str | None:
    """Which workflow stage charged this, or ``None`` for one that names none."""
    return STAGE_FOR_COST.get(cost_stage)


def stage_of_artifact(name: str) -> str | None:
    """Which workflow stage wrote this artifact, or ``None`` if unrecognised."""
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
            "removal_stages": list(REMOVAL_STAGES),
            "activation_stages": list(ACTIVATION_STAGES),
            "terminal_outcomes": sorted(TERMINAL_OUTCOMES),
            "cost_stages": {k: list(v) for k, v in COST_STAGES.items()},
            "ask_cost_stages": list(ASK_COST_STAGES),
            "artifact_stages": dict(ARTIFACT_STAGES),
        },
        indent=2,
        sort_keys=True,
    )


if __name__ == "__main__":  # pragma: no cover - a dump, not behaviour
    print(as_json())
