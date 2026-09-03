"""The probe's verdicts, asserted without a Qdrant standing up.

Same split as `test_audit_version.py`: the arithmetic that decides *which gate*
dropped a chunk is pure, and the store-touching half in
`scripts/probe_retrieval.py` holds no decisions. The numbers in the canonical
case below are the real ones measured on 2026-09-03 against `lib_teologia`, so
the test that pins the verdict is also the record of the measurement.
"""

from __future__ import annotations

from brainworker import proberetrieval as pr


# The case this module was written for. «¿Quién fue Jesucristo?» against
# `chk_80a04dd78031ef67992a9b44` (05-CodigoJesus, char_span [14461,15266]),
# measured against the live index: cosine 0.594133 at dense rank 318 of 7,538,
# BM25 2.566 at rank 221, both prefetches 50 wide.
MEASURED_DENSE = dict(rank=318, score=0.594133, prefetch_limit=50, floor=0.60, searched=4000)
MEASURED_SPARSE = dict(rank=221, score=2.5659842, prefetch_limit=50, floor=None, searched=400)


# --- rank arithmetic ---------------------------------------------------------


def test_a_rank_is_one_based_because_every_limit_it_is_compared_against_is_a_count():
    assert pr.rank_of(["a", "b", "c"], "a") == 1
    assert pr.rank_of(["a", "b", "c"], "c") == 3


def test_a_chunk_outside_the_window_is_unranked_not_last():
    """`None` and a large integer must not be the same value.

    A rank that fell back to `len(ids)` would read as "ranked worst" and would
    silently pass a `<= prefetch_limit` comparison the moment the window was
    narrower than the limit.
    """
    assert pr.rank_of(["a", "b"], "z") is pr.UNRANKED
    assert pr.rank_of([], "z") is pr.UNRANKED


# --- one leg's account -------------------------------------------------------


def test_the_dense_floor_excludes_a_chunk_from_the_prefetch_whatever_its_rank():
    """The threshold is applied *inside* the dense prefetch, not after it.

    Deriving `in_prefetch` from the rank alone would report a chunk as carried
    by a leg whose own verdict two keys higher says it was refused.
    """
    leg = pr.leg("dense", rank=3, score=0.55, prefetch_limit=50, floor=0.60)
    assert leg["clears_floor"] is False
    assert leg["in_prefetch"] is False


def test_the_sparse_leg_has_no_floor_and_reports_none_rather_than_a_verdict():
    """`min_score` reaches the dense prefetch and never the sparse one.

    That is invariant #8, and it is what lets a lexical match answer a question
    no vector cleared. A `clears_floor: false` here would report a gate that
    does not exist in the code.
    """
    leg = pr.leg("sparse", rank=221, score=2.56, prefetch_limit=50, floor=None)
    assert leg["floor"] is None
    assert leg["clears_floor"] is None
    assert leg["in_prefetch"] is False


def test_a_leg_carries_how_deep_the_probe_looked():
    """Absent from a window of 400 is not the same finding as absent from 4,000."""
    leg = pr.leg("sparse", rank=None, score=None, prefetch_limit=50, searched=400)
    assert leg["searched"] == 400
    assert leg["rank"] is None


# --- the verdict -------------------------------------------------------------


def _verdict(dense, sparse, *, fused_rank=None, delivered_rank=None,
             candidate_limit=40, top_k=8):
    return pr.where_lost(
        dense=pr.leg("dense", **dense),
        sparse=pr.leg("sparse", **sparse),
        fused_rank=fused_rank,
        candidate_limit=candidate_limit,
        delivered_rank=delivered_rank,
        top_k=top_k,
    )


def test_a_chunk_no_leg_carried_is_lost_at_the_prefetch_never_at_the_fusion():
    """RRF cannot rank what it was not handed.

    Reporting this as a fusion problem sends somebody to look at a fusion
    parameter that had nothing to do with it. This is the canonical measured
    case: dense below the floor at rank 318, sparse at rank 221, prefetch 50.
    """
    v = _verdict(MEASURED_DENSE, MEASURED_SPARSE)
    assert v["reached"] is False
    assert v["lost_at"] == "prefetch"
    assert "RRF never saw this chunk" in v["note"]


def test_the_verdict_says_that_lowering_the_floor_alone_would_not_admit_it():
    """The finding that kills the cheap parameter fix, stated as the remedy.

    The chunk sits at dense rank 318 against a prefetch of 50, so a floor low
    enough to admit it still leaves it 268 places outside the window. A verdict
    that named only the floor would recommend exactly the change that cannot
    work.
    """
    v = _verdict(MEASURED_DENSE, MEASURED_SPARSE)
    assert "lowering the floor alone cannot admit it" in v["remedy"]


def test_a_chunk_that_clears_the_floor_but_ranks_too_deep_is_a_width_problem():
    """Not a threshold problem — and the remedy must not mention the floor.

    Same shape as the measured case with one number changed, which is the point:
    the two are indistinguishable by outcome and have different fixes.
    """
    dense = dict(MEASURED_DENSE, score=0.72)
    v = _verdict(dense, MEASURED_SPARSE)
    assert v["lost_at"] == "prefetch"
    assert "floor" not in v["remedy"]
    assert "prefetch width" in v["remedy"]


def test_a_chunk_a_leg_carried_but_the_fusion_ranked_past_the_candidates():
    v = _verdict(
        dict(MEASURED_DENSE, rank=12, score=0.71),
        MEASURED_SPARSE,
        fused_rank=55,
    )
    assert v["lost_at"] == "fusion"


def test_a_chunk_inside_the_candidates_but_absent_from_the_evidence_blames_diversify():
    """The only gate whose cause is a chunk's neighbours rather than itself."""
    v = _verdict(
        dict(MEASURED_DENSE, rank=12, score=0.71),
        MEASURED_SPARSE,
        fused_rank=9,
    )
    assert v["lost_at"] == "diversify"
    assert "neighbours" in v["remedy"]


def test_a_delivered_chunk_reports_no_gate_at_all():
    v = _verdict(
        dict(MEASURED_DENSE, rank=2, score=0.81),
        MEASURED_SPARSE,
        fused_rank=2,
        delivered_rank=2,
    )
    assert v["reached"] is True
    assert v["lost_at"] is None
    assert v["delivered_rank"] == 2


# --- the property no score can show ------------------------------------------


def test_a_question_that_reduces_to_one_term_is_reported_as_such():
    """«¿Quién fue Jesucristo?» tokenises to `['jesucristo']`.

    «quién» and «fue» are both stopwords, so the sparse leg's entire input is
    one term — and with one term BM25 is length-normalised term frequency, which
    separates short chunks from long ones rather than relevant from irrelevant.
    That is a property of the question, invisible in the score, and no index
    change fixes it.
    """
    out = pr.probe(
        question="¿Quién fue Jesucristo?",
        terms=["jesucristo"],
        target="chk_80a04dd78031ef67992a9b44",
        dense=pr.leg("dense", **MEASURED_DENSE),
        sparse=pr.leg("sparse", **MEASURED_SPARSE),
        fused_rank=None,
        candidate_limit=40,
        delivered_rank=None,
        top_k=8,
    )
    assert out["single_term_query"] is True
    assert out["query_terms"] == ["jesucristo"]
    assert out["verdict"]["lost_at"] == "prefetch"


def test_a_multi_term_question_is_not_flagged():
    out = pr.probe(
        question="¿Qué enseñó Pablo sobre la resurrección?",
        terms=["ensen", "pablo", "resurreccion"],
        target="chk_x",
        dense=pr.leg("dense", rank=1, score=0.8, prefetch_limit=50, floor=0.6),
        sparse=pr.leg("sparse", rank=1, score=4.0, prefetch_limit=50),
        fused_rank=1,
        candidate_limit=40,
        delivered_rank=1,
        top_k=8,
    )
    assert out["single_term_query"] is False
    assert out["verdict"]["reached"] is True
