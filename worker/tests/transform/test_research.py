"""Where a transformation's retrieval charges are filed.

Found by running the first real transformation and reading `cost_entry`: the
whole exercise's spend was filed under `ask-embedding`, which `ASK_COST_STAGES`
deliberately maps to **no workflow stage** because a question has no pipeline.
So every query embedding a transformation made rendered in the audit view under
the trailing `stage: null` heading, beside charges that belong to nobody — the
`evidence` defect exactly, which spent months doing the same thing — and the two
`COST_STAGES` entries naming these stages were declared and written by nothing.
"""

from __future__ import annotations

from brainworker.answering.types import Evidence
from brainworker.pipeline import Spend
from brainworker.stages import COST_STAGES, stage_of_cost
from brainworker.transform import research as R


def _search(stage_written: str = "ask-embedding"):
    """A stand-in for `retrieve.search`, writing the stage it really writes."""

    def search(settings, provider, question, plan, spend=None, supported=None):
        if spend is not None:
            spend.append(
                Spend(stage=stage_written, model="gemini-embedding-2",
                      input_tokens=12, output_tokens=0, usd=0.000004)
            )
        if supported is not None:
            supported.append(3)
        return [
            Evidence(chunk_id="chk_a", version_id="v_other", document_id="d",
                     title="Otra", breadcrumb="", text="t", kind="cuerpo",
                     score=0.7, locator="Otra · p. 1")
        ]

    return search


def test_a_probe_s_charge_is_filed_under_the_probing_stage(monkeypatch):
    monkeypatch.setattr(R, "search", _search())
    found = R.gather(
        object(), object(), stage=R.STAGE_PROBE, library_id="l", tenant_id="t",
        exclude_version_id="v_self", purposes=["context"], title="", intent="",
        source_text="algo de texto", allowance=1,
    )
    assert [s.stage for s in found.spend] == [R.STAGE_PROBE]
    assert "ask-embedding" not in {s.stage for s in found.spend}


def test_a_chapter_s_research_is_filed_under_composing(monkeypatch):
    monkeypatch.setattr(R, "search", _search())
    found = R.gather(
        object(), object(), library_id="l", tenant_id="t",
        exclude_version_id="v_self", purposes=["context"], title="", intent="",
        source_text="algo de texto", allowance=1,
    )
    assert [s.stage for s in found.spend] == [R.STAGE_RESEARCH]


def test_both_stages_are_ones_the_workflow_actually_has():
    """The other half of the same defect: a charge filed under a stage the run
    never enters lands in the trailing `stage: null` group just the same."""
    assert stage_of_cost(R.STAGE_PROBE) == "probing"
    assert stage_of_cost(R.STAGE_RESEARCH) == "composing"
    assert R.STAGE_PROBE in COST_STAGES["probing"]
    assert R.STAGE_RESEARCH in COST_STAGES["composing"]


def test_the_amount_and_the_model_are_untouched(monkeypatch):
    """Only the filing changes. A re-label that moved a figure would be worse
    than the defect it fixed."""
    monkeypatch.setattr(R, "search", _search())
    found = R.gather(
        object(), object(), stage=R.STAGE_PROBE, library_id="l", tenant_id="t",
        exclude_version_id="v_self", purposes=["context"], title="", intent="",
        source_text="algo", allowance=1,
    )
    assert found.spend[0].usd == 0.000004
    assert found.spend[0].model == "gemini-embedding-2"
    assert found.spend[0].input_tokens == 12
