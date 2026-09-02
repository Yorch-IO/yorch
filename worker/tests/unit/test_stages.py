"""The stage vocabulary, checked against the code that actually sets it.

These read source text rather than running a workflow, deliberately. What they
are guarding is not behaviour but a *correspondence*: a stage added to
`IngestWorkflow` and not to `stages.INGEST_STAGES` produces an audit trail with
a row nothing can name or order, and no amount of exercising the workflow would
say so — the run would succeed and the ledger would quietly sort the new stage
last. The same trick `LibraryScreen.test.ts` uses in the app, for the same
reason: the property is about the source, so the source is what to assert on.
"""

from __future__ import annotations

import pathlib
import re

from brainworker import stages

ROOT = pathlib.Path(stages.__file__).resolve().parent
INGEST = (ROOT / "workflows" / "ingest.py").read_text(encoding="utf-8")
REBUILD = (ROOT / "workflows" / "rebuild.py").read_text(encoding="utf-8")
PAID = (ROOT / "activities" / "paid.py").read_text(encoding="utf-8")


def _assigned(source: str) -> set[str]:
    """Every stage the source names, however it names it.

    Two forms, because a transition that records an event goes through
    `_enter(run_id, "x")` while the two terminal stages are still a bare
    assignment — they are set immediately before `_finish`, which writes the
    event that closes the run and names the stage it was in.
    """
    return set(
        re.findall(r'self\._stage = "([^"]+)"', source)
    ) | set(
        re.findall(r'_enter\(\s*run_id,\s*"([^"]+)"', source)
    )


def test_every_stage_the_ingest_workflow_sets_is_in_the_list() -> None:
    missing = _assigned(INGEST) - set(stages.INGEST_STAGES) - {"starting"}
    assert missing == set(), (
        f"IngestWorkflow sets {sorted(missing)}, which stages.INGEST_STAGES does "
        "not name — the audit view cannot order or translate them"
    )


def test_every_stage_the_rebuild_workflow_sets_is_in_the_list() -> None:
    missing = _assigned(REBUILD) - set(stages.REBUILD_STAGES) - {"starting"}
    assert missing == set()


def test_the_list_names_no_stage_no_workflow_sets() -> None:
    """The other direction, so a removed stage does not linger as a dead key.

    `starting` is excluded above because it is the initial value of the field
    rather than a stage anything transitions into, and no event is ever recorded
    for it.
    """
    unused = set(stages.INGEST_STAGES) - _assigned(INGEST)
    assert unused == set(), f"stages.INGEST_STAGES names {sorted(unused)}, unset"


def test_every_charged_stage_maps_to_a_workflow_stage_or_to_asking() -> None:
    """A cost row must be attributable, or explicitly known to be unattributable.

    The audit view's totals are asserted to equal `total_cost`, which is only
    achievable if every `cost_entry.stage` either maps to a pipeline stage or is
    a named exception. An unlisted one would be silently dropped from the ledger
    while still being billed.
    """
    charged = set(re.findall(r'stage="([a-z-]+)"', PAID))
    # `correct` is the engine's own stage name, passed to the correction adapter
    # rather than to `record_cost`; `thinking_for()` resolves it, no charge
    # carries it.
    charged -= {"correct"}
    unattributable = charged - set(stages.STAGE_FOR_COST) - set(stages.ASK_COST_STAGES)
    assert unattributable == set(), (
        f"paid.py charges {sorted(unattributable)}, which no workflow stage "
        "claims and which is not one of the ask stages"
    )


def test_the_cost_map_inverts_without_collision() -> None:
    """Two workflow stages must not claim the same charge.

    A collision would make `STAGE_FOR_COST` lose one of them silently, and the
    ledger would attribute a real bill to the wrong stage — worse than not
    attributing it at all.
    """
    flat = [cost for costs in stages.COST_STAGES.values() for cost in costs]
    assert len(flat) == len(set(flat))
    assert len(stages.STAGE_FOR_COST) == len(flat)


def test_every_cost_stage_belongs_to_a_stage_the_workflow_has() -> None:
    known = set(stages.INGEST_STAGES) | set(stages.REBUILD_STAGES)
    assert set(stages.COST_STAGES) <= known


def test_every_artifact_kind_is_attributed_to_a_stage() -> None:
    """Because an artifact with no stage would render outside the table."""
    from brainworker.artifacts import KINDS

    assert set(KINDS) == set(stages.ARTIFACT_STAGES), (
        "artifacts.KINDS and stages.ARTIFACT_STAGES disagree: "
        f"{sorted(set(KINDS) ^ set(stages.ARTIFACT_STAGES))}"
    )
    known = set(stages.INGEST_STAGES) | set(stages.REBUILD_STAGES)
    assert set(stages.ARTIFACT_STAGES.values()) <= known


def test_an_unknown_stage_sorts_last_rather_than_first() -> None:
    assert stages.order_of("staging") == 0
    assert stages.order_of("done") == len(stages.INGEST_STAGES) - 1
    assert stages.order_of("a stage from the future") == len(stages.INGEST_STAGES)
