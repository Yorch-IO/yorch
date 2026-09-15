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
VIDEO = (ROOT / "workflows" / "video.py").read_text(encoding="utf-8")
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


def test_every_stage_the_video_workflow_sets_is_in_the_list() -> None:
    missing = _assigned(VIDEO) - set(stages.VIDEO_STAGES) - {"starting"}
    assert missing == set(), (
        f"VideoIngestWorkflow sets {sorted(missing)}, which stages.VIDEO_STAGES "
        "does not name — the audit view cannot order or translate them"
    )


def test_the_video_list_names_no_stage_the_workflow_never_sets() -> None:
    """The other direction. Two stages here run at different points depending on
    whether the video had captions, but each is still *set* somewhere in the
    source, so this holds for both paths."""
    unused = set(stages.VIDEO_STAGES) - _assigned(VIDEO)
    assert unused == set(), (
        f"stages.VIDEO_STAGES names {sorted(unused)}, which the workflow never "
        "enters"
    )


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


def _every_stage() -> set[str]:
    """Every stage any workflow names.

    A union rather than a literal, so a workflow added later is covered by the
    two tests below the moment its tuple exists — the failure they exist to
    catch is a *new* stage nobody attributed, and a hand-written list would have
    to be remembered at exactly the moment it was being forgotten.
    """
    return (
        set(stages.INGEST_STAGES)
        | set(stages.REBUILD_STAGES)
        | set(stages.VIDEO_STAGES)
        | set(stages.REMOVAL_STAGES)
        | set(stages.ACTIVATION_STAGES)
    )


def test_every_cost_stage_belongs_to_a_stage_the_workflow_has() -> None:
    assert set(stages.COST_STAGES) <= _every_stage()


def test_every_artifact_kind_is_attributed_to_a_stage() -> None:
    """Because an artifact with no stage would render outside the table."""
    from brainworker.artifacts import KINDS

    assert set(KINDS) == set(stages.ARTIFACT_STAGES), (
        "artifacts.KINDS and stages.ARTIFACT_STAGES disagree: "
        f"{sorted(set(KINDS) ^ set(stages.ARTIFACT_STAGES))}"
    )
    assert set(stages.ARTIFACT_STAGES.values()) <= _every_stage()


def test_an_unknown_stage_sorts_last_rather_than_first() -> None:
    assert stages.order_of("staging") == 0
    assert stages.order_of("done") == len(stages.INGEST_STAGES) - 1
    assert stages.order_of("a stage from the future") == len(stages.INGEST_STAGES)


def test_an_override_names_a_stage_the_path_it_overrides_actually_has() -> None:
    """The property that makes the override map worth having.

    `ARTIFACT_STAGES` is keyed by artifact name alone, so it can name only one
    writer per artifact — and for `evidence`, which both paths write, it named
    `extracting`, a stage no video run has. `auditlog.build` then found nothing
    to attach it to and rendered it under the trailing `stage: null` heading,
    beside charges that belong to nobody. This asserts the fix for every
    override rather than for the one that was reported.
    """
    lists = {
        "video": stages.VIDEO_STAGES,
        "rebuild": stages.REBUILD_STAGES,
        "index": stages.INGEST_STAGES,
        "preview": stages.INGEST_STAGES,
        "reindex": stages.INGEST_STAGES,
    }
    for kind, overrides in stages.ARTIFACT_STAGE_OVERRIDES.items():
        assert kind in lists, f"no stage list known for run kind {kind!r}"
        for artifact, stage in overrides.items():
            assert stage in lists[kind], (
                f"{kind}/{artifact} is attributed to {stage!r}, "
                f"which is not a stage a {kind} run has"
            )


def test_the_artifacts_a_video_writes_all_land_on_stages_a_video_has() -> None:
    """`evidence` is the one that did not, and it was visible in the UI."""
    written_by_the_video_path = (
        "video_probe", "captions", "transcript", "transcript_text",
        "evidence", "preview_chunks", "estimate", "corrected_text",
        "correction_report", "chunks",
    )
    for name in written_by_the_video_path:
        stage = stages.stage_of_artifact(name, "video")
        assert stage in stages.VIDEO_STAGES, f"{name} -> {stage}"


def test_an_override_does_not_leak_into_the_path_it_was_not_written_for() -> None:
    assert stages.stage_of_artifact("evidence", "video") == "grouping"
    assert stages.stage_of_artifact("evidence", "index") == "extracting"
    assert stages.stage_of_artifact("evidence") == "extracting"


def test_the_overrides_are_dumped_for_the_typescript_fork_to_compare() -> None:
    """`stages.parity.spec.ts` compares this map with exact equality, so an
    override added here and not there is a cross-repo defect, not a follow-up."""
    import json

    dumped = json.loads(stages.as_json())
    assert dumped["artifact_stage_overrides"] == {
        k: dict(v) for k, v in stages.ARTIFACT_STAGE_OVERRIDES.items()
    }
