"""Assembling a run's stage ledger out of the four things the catalog holds.

A pure function over rows, deliberately: the paid plane serves the same payload
from the same tables, so keeping the assembly out of the route makes the
TypeScript a translation of something with tests rather than a second opinion.

What it joins, and why none of the four alone is enough:

- `run_event` — what happened and when. The only per-stage timing there is.
- `cost_entry` — what it cost, keyed by a *different* stage vocabulary. See
  `stages.py`; `learning` charges `profile`, `correcting` charges `correction`,
  `evaluating` charges two.
- `run_artifact` — what it produced, attributed to a stage only by which
  activity happened to write it.
- `profile_warning` — what it noticed, keyed by version rather than by run.

Three rules the assembly keeps, each already established elsewhere in this
codebase and each about not asserting more than is known:

- **A stage with no charges carries `cost: null`, not a zeroed block.** "This
  stage does not spend" and "this stage's charge was not recorded" are different
  claims, and a zero renders as the first while sometimes meaning the second.
  The same rule `/runs/{id}` follows by omitting `semantics` rather than zeroing
  it.
- **`usd` is `null`, never `0`, when no price is known**, with
  `unpriced_entries` beside it so the total is still honest. `Cost.usd`'s rule.
- **A charge that maps to no stage is not dropped.** The three a question makes
  — `planning`, `ask-embedding`, `answering` — belong to no pipeline stage,
  because asking has no gate and therefore no pipeline to name. They land in a
  trailing group with `stage: null`, which is what keeps the ledger's totals
  equal to `total_cost` rather than quietly less than the bill.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import stages as stage_vocab
from .catalog import Cost, RunEvent, RunSummary


def _money(rows: Sequence[Cost]) -> dict[str, Any]:
    priced = [c.usd for c in rows if c.usd is not None]
    return {
        "input_tokens": sum(c.input_tokens for c in rows),
        "output_tokens": sum(c.output_tokens for c in rows),
        # None rather than 0.0 when nothing in this group carries a price: a
        # stage billed against a model with no known price and a stage that spent
        # nothing are different facts, and zero claims the second.
        "usd": sum(priced) if priced else None,
        "unpriced_entries": sum(1 for c in rows if c.usd is None),
        "entries": [
            {
                "stage": c.stage,
                "provider": c.provider,
                "model": c.model,
                "input_tokens": c.input_tokens,
                "output_tokens": c.output_tokens,
                "usd": c.usd,
            }
            for c in rows
        ],
    }


def build(
    run: RunSummary,
    events: Sequence[RunEvent],
    costs: Sequence[Cost],
    artifacts: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """The ledger, from rows that are already scoped to one organisation.

    This function applies **no** tenant predicate and must never be handed rows
    from more than one: every reader it consumes takes a required `tenant_id`,
    and that is where the boundary is. A predicate here would be a third guard
    that hides a missing first one.
    """
    ordered = sorted(events, key=lambda e: e.seq)

    # Charges grouped by the stage that made them. A stage that runs twice —
    # `extracting` when a learned profile re-reads the document, `embedding`
    # again inside a tuning round — has its charges shown against its **first**
    # occurrence, because that is where the bulk of the work is. The totals below
    # are exact either way; only the row a charge is displayed on can be off, and
    # never by more than one occurrence of the same stage.
    by_stage: dict[str, list[Cost]] = {}
    unattributed: list[Cost] = []
    for cost in costs:
        stage = stage_vocab.stage_of_cost(cost.stage)
        if stage is None:
            unattributed.append(cost)
        else:
            by_stage.setdefault(stage, []).append(cost)

    by_artifact_stage: dict[str, list[dict[str, Any]]] = {}
    loose_artifacts: list[dict[str, Any]] = []
    for artifact in artifacts:
        stage = stage_vocab.stage_of_artifact(str(artifact.get("name", "")))
        if stage is None:
            loose_artifacts.append(artifact)
        else:
            by_artifact_stage.setdefault(stage, []).append(artifact)

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for i, event in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        # `ended_at` is the next transition, so a stage's duration is measured
        # rather than reported by the stage itself. The last row has none, which
        # means one of two things a reader can tell apart from `outcome`: the run
        # is still in this stage, or this row *is* the outcome and is an instant.
        ended_at = nxt.at if nxt is not None else None
        first = event.stage not in seen
        seen.add(event.stage)
        rows.append(
            {
                "seq": event.seq,
                "stage": event.stage,
                "at": event.at,
                "ended_at": ended_at,
                "seconds": (
                    (ended_at - event.at).total_seconds() if ended_at else None
                ),
                "outcome": event.outcome,
                "detail": event.detail,
                "cost": (
                    _money(by_stage[event.stage])
                    if first and event.stage in by_stage
                    else None
                ),
                "artifacts": (
                    by_artifact_stage.get(event.stage, []) if first else []
                ),
            }
        )

    # Charges whose stage never appeared in the trail, which is not the same
    # gap as a charge that maps to no stage at all.
    #
    # Every run indexed before `run_event` existed has costs and no events, so
    # `rows` is empty and there is nowhere for `by_stage` to attach — the money
    # was in `totals` and in no row, which is exactly the "bill that does not add
    # up" this function claims not to produce. Measured against a real run on
    # 2026-09-01: two charges, `stages: []`, and a total nothing accounted for.
    # A re-chunked run can hit it more narrowly, when a charge's stage is one the
    # trail happens not to record.
    for stage, charges in by_stage.items():
        if stage not in seen:
            unattributed.extend(charges)
    for stage, files in by_artifact_stage.items():
        if stage not in seen:
            loose_artifacts.extend(files)

    # Whatever no stage claimed, rather than nothing. A ledger that silently
    # dropped a charge would be a bill that does not add up, which is worse than
    # one that says "these belong to no stage".
    if unattributed or loose_artifacts:
        rows.append(
            {
                "seq": None,
                "stage": None,
                "at": None,
                "ended_at": None,
                "seconds": None,
                "outcome": None,
                "detail": None,
                "cost": _money(unattributed) if unattributed else None,
                "artifacts": loose_artifacts,
            }
        )

    return {
        "run": {
            "id": run.id,
            "workflow_id": run.workflow_id,
            "kind": run.kind,
            "state": run.state,
            "stage": run.stage,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "error_kind": run.error_kind,
            "error_detail": run.error_detail,
            "title": run.title,
            "library_id": run.library_id,
            # Hand-built, so a field added to `RunSummary` does not arrive here
            # by itself — and the paid plane's `auditRun` derives from its own
            # `runSummary`, so it *does*. Dropping it made the two planes
            # disagree about the same run with nothing failing anywhere, which
            # is the shape of the `asdict`/`style_effort` defect this file is
            # one hop away from. Caught on production: `/runs` reported the URL
            # and `/runs/{id}/audit` reported null for the same row.
            "label": run.label,
            "document_id": run.document_id,
            "version_id": run.version_id,
        },
        "stages": rows,
        # Summed over every charge, attributed or not, so this always equals
        # what `total_cost` reports for the same run.
        "totals": {
            k: v for k, v in _money(list(costs)).items() if k != "entries"
        },
        "warnings": list(warnings),
    }
