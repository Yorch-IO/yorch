"""What a run reports about how well the index it wrote can be searched.

The property under test is the same one `_semantics_counts` has: the figure is
read from the run's own `scores.json`, so a run nobody asked to measure reports
**nothing** rather than having a zero invented for it. Recall of 0.00 and "this
was never measured" are different statements, and one of them sends a person to
fix an index that is fine.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("fastapi.testclient")

from brainworker.api import main  # noqa: E402
from brainworker.artifacts import ArtifactStore  # noqa: E402

RUN = "ingest-1787000000000-abcdef01"


def _write(workspace: pathlib.Path, **over):
    report = {
        "scores": {
            "recall_at_1": 0.675,
            "recall_at_5": 0.9,
            "mrr_at_10": 0.7642,
            "recall_at_5_dense_only": 1.0,
            "noise_floor": 0.6232,
            "chunks": 274,
            "eval_questions": 40,
        },
        "leakage": "hybrid 0.900 vs dense 1.000",
        "margin": 0.0771,
        "retrieval": {"min_score": 0.6, "per_section": 2, "dense_only": False},
        "scope": {"tenant_id": "tnt_x", "version_id": "ver_y"},
        "misses": [{"question": "¿…?", "want": 12, "rank": -1}],
    }
    report["scores"].update(over.pop("scores", {}))
    report.update(over)
    ref = ArtifactStore(workspace, RUN).write_json("scores", report)
    return [{"name": ref.kind, "rel_path": ref.path, "sha256": ref.sha256,
             "size_bytes": ref.bytes}]


def test_a_measured_run_reports_recall_beside_its_noise_floor(tmp_path: pathlib.Path):
    """Both, or neither is interpretable. Recall says how often the right chunk
    came back; the floor says what a wrong one scores."""
    out = main._measured_scores(tmp_path, RUN, _write(tmp_path))

    assert out["recall_at_5"] == 0.9
    assert out["noise_floor"] == 0.6232
    # The dense-only figure is the eval set's own vocabulary leakage: the
    # questions were written from the chunks they must find.
    assert out["recall_at_5_dense_only"] == 1.0
    assert out["eval_questions"] == 40
    assert out["misses"] == 1


def test_a_run_nobody_measured_reports_nothing_rather_than_zero(tmp_path: pathlib.Path):
    assert main._measured_scores(tmp_path, RUN, [{"name": "chunks"}]) is None


def test_an_eval_set_that_produced_no_questions_reports_nothing(tmp_path: pathlib.Path):
    """Recall over zero questions is 0.0 and reads exactly like a broken index."""
    artifacts = _write(tmp_path, scores={"eval_questions": 0, "recall_at_5": 0.0})

    assert main._measured_scores(tmp_path, RUN, artifacts) is None


def test_a_pruned_workspace_still_renders_a_status(tmp_path: pathlib.Path):
    """`infra/backup.sh` found 79 artifact rows with no file. A status page must
    survive one."""
    missing = [{"name": "scores", "rel_path": f"runs/{RUN}/scores.json",
                "sha256": "0" * 64, "size_bytes": 3}]

    assert main._measured_scores(tmp_path, RUN, missing) is None


def test_a_changed_artifact_is_not_reported_as_a_measurement(tmp_path: pathlib.Path):
    """`ArtifactStore.read_json` verifies the sha256. A file that no longer
    matches the row is not this run's measurement, whatever it says."""
    artifacts = _write(tmp_path, scores={"recall_at_5": 0.9})
    path = tmp_path / artifacts[0]["rel_path"]
    path.write_text(path.read_text().replace("0.9", "1.0"), encoding="utf-8")

    assert main._measured_scores(tmp_path, RUN, artifacts) is None
