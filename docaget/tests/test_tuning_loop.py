"""The tuning loop's state must stay consistent across rounds.

Every round loops back through the graph, and three separate bugs in this area
were only found by running the loop for real. Each one silently invalidated the
comparisons the loop was making, while the logs looked perfectly healthy:

1. The eval set was regenerated every round, so baselines drifted because the
   *questions* changed, not the configuration.
2. Reverting a chunk candidate updated the profile but not the indexed collection,
   so the next round measured a baseline belonging to a rejected configuration.
3. The objective was recall@5, which saturated and could not see real gains.

These tests pin all three down without touching the API.
"""

from __future__ import annotations

import pytest

from docagent import evaluate as ev
from docagent import graph as g
from docagent import profiles as prof
from docagent.chunk import ChunkRules


class CountingVertex:
    """Counts eval-set generations so regeneration is detectable."""

    def __init__(self):
        self.calls = 0

    def generate(self, prompt, **kw):
        self.calls += 1
        import json

        return json.dumps({"question": f"pregunta {self.calls}", "answerable_only_by_main": True})


class Deps(g.Deps):
    def __init__(self, vertex):
        super().__init__(vertex=vertex)


def _profile(**chunk_overrides) -> prof.Profile:
    return prof.Profile(
        fingerprint="fp",
        slug="slug",
        extractor="plain",
        chunk_rules=ChunkRules(**chunk_overrides),
    )


def _items(n: int) -> list[prof.EvalItem]:
    return [
        prof.EvalItem(question=f"q{i}", chunk_index=i, char_mid=i * 100 + 50)
        for i in range(n)
    ]


# --- 1. the eval set is built once per run -----------------------------------


def test_evalset_is_reused_from_state_on_a_loop_back():
    """The bug: the node only consulted the profile, whose copy is written at
    persist — after tuning. So every round regenerated the questions."""
    v = CountingVertex()
    deps = Deps(v)
    state = {"profile": _profile(), "chunks": [], "evalset": _items(30)}

    out = g.n_build_evalset(state, deps)

    assert v.calls == 0, "must not regenerate when state already holds an eval set"
    assert len(out["evalset"]) == 30
    assert "reusing" in out["log"][0]


def test_evalset_is_reused_from_the_profile_on_a_later_run():
    v = CountingVertex()
    p = _profile()
    p.evalset = _items(12)
    out = g.n_build_evalset({"profile": p, "chunks": []}, Deps(v))
    assert v.calls == 0
    assert len(out["evalset"]) == 12


def test_evalset_is_skipped_entirely_on_a_dry_run():
    v = CountingVertex()
    out = g.n_build_evalset(
        {"profile": _profile(), "chunks": [], "dry_run": True}, Deps(v)
    )
    assert v.calls == 0
    assert out["evalset"] == []


# --- 2. a reverted chunk candidate forces a reindex --------------------------


def test_revert_routes_back_to_chunk_so_the_index_matches_the_profile():
    """Reverting the profile leaves the collection holding the rejected
    candidate's chunks. Continuing without reindexing measures the wrong thing."""
    assert g.e_needs_tuning({"revert_reindex": True}) == "chunk"


def test_chunk_node_clears_the_revert_flag():
    """Otherwise the graph would loop through chunk forever."""
    src = b"1. Titulo\n\n" + b"Cuerpo del documento con suficiente texto. " * 40
    from docagent.chunk import split_paragraphs

    out = g.n_chunk(
        {"profile": _profile(), "text": src, "chunks": [], "revert_reindex": True},
        Deps(CountingVertex()),
    )
    assert out["revert_reindex"] is False
    assert out["chunks"]
    del split_paragraphs


def test_tuning_stops_at_the_round_cap():
    assert g.e_needs_tuning(
        {"tune_round": g.MAX_TUNE_ROUNDS, "evalset": _items(5), "scores": prof.Scores()}
    ) == "persist"


def test_force_tune_makes_the_loop_reachable_when_the_target_is_met():
    """Without it the loop is unreachable on any document that already scores well,
    which is how the first attempt to verify it never entered it at all."""
    scores = prof.Scores(recall_at_5=0.95)
    base = {"scores": scores, "evalset": _items(5), "tune_round": 0}
    assert g.e_needs_tuning(base) == "persist"
    assert g.e_needs_tuning({**base, "force_tune": True}) == "tune"


# --- 3. the objective must be able to see a real gain ------------------------


def test_recall_at_5_would_have_been_blind_to_the_measured_overlap_effect():
    """Measured: overlap=300 moved MRR@10 by +0.040 while leaving recall@5
    identical. Tuning on recall@5 could never have recovered a sabotaged overlap."""
    def run(ranks):
        rr = [1.0 / r for r in ranks if r]
        return ev.EvalRun(
            hits_at_1=sum(1 for r in ranks if r == 1),
            hits_at_5=sum(1 for r in ranks if r and r <= 5),
            reciprocal_ranks=rr,
            questions=len(ranks),
        )

    sabotaged = run([4] * 21 + [0] * 3)
    restored = run([2] * 21 + [0] * 3)
    assert sabotaged.recall_at_5 == restored.recall_at_5
    assert ev.objective(restored) - ev.objective(sabotaged) > 0.1


def test_the_noise_margin_shrinks_with_the_square_root_of_the_sample():
    """Quantifies why a 24-question eval could not detect a +0.040 effect: the
    margin was ±0.073. Detecting it needs about 80 questions."""
    ranks = [2, 3, 1, 0, 4, 1, 2, 5]

    def run(n):
        rs = (ranks * ((n // len(ranks)) + 1))[:n]
        return ev.EvalRun(
            hits_at_5=sum(1 for r in rs if r and r <= 5),
            reciprocal_ranks=[1.0 / r for r in rs if r],
            questions=n,
        )

    small = ev.noise_margin(run(24))
    large = ev.noise_margin(run(96))
    assert small > large
    # Four times the questions should roughly halve the margin.
    assert 1.6 < small / large < 2.6, (small, large)
