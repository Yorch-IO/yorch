"""The audit's comparators, asserted without a store standing up.

Everything here is a pure function over rows, which is the point: the audit's
value is in what it *compares*, and a test that needed a Postgres, a Memgraph
and a Qdrant to check a set difference would be run rarely enough to be worth
nothing. The store-touching half lives in `scripts/audit_version.py` and holds no
decisions.
"""

from __future__ import annotations

import pytest

from brainworker import auditversion as av


# --- the read-only guarantee -------------------------------------------------


def test_a_write_clause_is_refused():
    with pytest.raises(av.NotReadOnly):
        av.assert_read_only("MATCH (n:Chunk) SET n.text = 'x' RETURN n")
    with pytest.raises(av.NotReadOnly):
        av.assert_read_only("MATCH (n) DETACH DELETE n")
    with pytest.raises(av.NotReadOnly):
        av.assert_read_only("MERGE (c:Concept {id: $id})")


def test_the_guard_matches_clauses_not_substrings():
    """A guard that flags `OFFSET` for `SET` is one somebody turns off."""
    av.assert_read_only("MATCH (n) RETURN n ORDER BY n.id SKIP 10 LIMIT 5")
    av.assert_read_only("MATCH (cl:Claim) RETURN cl.created_at AS at")


def test_every_statement_this_module_ships_is_a_read():
    """Importing already ran the guard; this states it as a property.

    A literal added to this module without going through `assert_read_only`
    would pass every other test in the file.
    """
    statements = [
        value
        for name, value in vars(av).items()
        if name.isupper() and isinstance(value, str) and "MATCH" in value
    ]
    assert statements
    for cypher in statements:
        av.assert_read_only(cypher)


# --- spans -------------------------------------------------------------------

RAW = "Primero. Segundo. Tercero.".encode("utf-8")


def _chunk(index, a, b, text):
    return {"index": index, "char_from": a, "char_to": b, "text": text}


def test_a_byte_exact_index_verifies():
    chunks = [_chunk(0, 0, 8, "Primero."), _chunk(1, 9, 17, "Segundo.")]
    report = av.verify_spans(chunks, RAW)
    assert report["spans_verified"] == 2
    assert report["spans_mismatched"] == 0


def test_a_shifted_span_is_found_and_both_ends_are_reported():
    """The two ways this fails look nothing alike, so both ends are shown."""
    chunks = [_chunk(0, 1, 9, "Primero.")]
    report = av.verify_spans(chunks, RAW)
    assert report["spans_verified"] == 0
    (miss,) = report["mismatches"]
    assert miss["index"] == 0
    assert miss["expected_head"] == "Primero."
    assert miss["found_head"] == "rimero. "


def test_a_span_that_does_not_decode_is_a_mismatch_not_a_crash():
    accented = "Café".encode("utf-8")  # the é is two bytes
    report = av.verify_spans([_chunk(0, 0, 4, "Café")], accented)
    assert report["spans_mismatched"] == 1
    assert "does not decode" in report["mismatches"][0]["why"]


def test_a_chunk_with_no_span_is_reported_rather_than_skipped():
    report = av.verify_spans([{"index": 0, "text": "x"}], RAW)
    assert report["spans_mismatched"] == 1


# --- the index sequence ------------------------------------------------------


def test_a_contiguous_chunking_is_contiguous():
    chunks = [_chunk(0, 0, 8, "a"), _chunk(1, 9, 17, "b"), _chunk(2, 18, 26, "c")]
    report = av.chunk_sequence(chunks, len(RAW))
    assert report["contiguous"] is True
    assert report["missing_indices"] == []


def test_a_hole_in_the_index_sequence_is_a_point_nothing_will_overwrite():
    chunks = [_chunk(0, 0, 8, "a"), _chunk(2, 18, 26, "c")]
    report = av.chunk_sequence(chunks, len(RAW))
    assert report["contiguous"] is False
    assert report["missing_indices"] == [1]


def test_overlapping_chunks_do_not_double_count_coverage():
    """`overlap_chars` is a real setting, so overlap is the normal case."""
    chunks = [_chunk(0, 0, 15, "a"), _chunk(1, 10, 26, "b")]
    report = av.chunk_sequence(chunks, 26)
    assert report["bytes_covered"] == 26
    assert report["coverage"] == 1.0


def test_an_inverted_span_is_named():
    report = av.chunk_sequence([_chunk(0, 10, 4, "a")], 26)
    assert report["inverted_spans"]


# --- the stale diff ----------------------------------------------------------


def test_a_graph_that_holds_exactly_what_the_run_produced_is_not_stale():
    report = av.stale_diff({"clm_a", "clm_b"}, {"clm_a", "clm_b"})
    assert report == {
        "produced": 2, "in_store": 2, "converged": 2,
        "left_behind": 0, "missing": 0, "stale_share": 0.0,
        "left_behind_sample": [], "missing_sample": [],
    }


def test_a_second_extraction_unions_rather_than_converging():
    """The recorded defect: MERGE adds, and `claim_id` keys on a paraphrase."""
    produced = {"clm_new1", "clm_new2", "clm_shared"}
    present = {"clm_new1", "clm_new2", "clm_shared", "clm_old1", "clm_old2"}
    report = av.stale_diff(produced, present)
    assert report["left_behind"] == 2
    assert report["converged"] == 3
    assert report["stale_share"] == 0.4
    assert report["left_behind_sample"] == ["clm_old1", "clm_old2"]


def test_what_the_run_made_and_the_graph_lacks_is_reported_too():
    """Its mirror: a projection that did not finish, which no count can see."""
    report = av.stale_diff({"a", "b"}, {"a"})
    assert report["missing"] == 1
    assert report["missing_sample"] == ["b"]


def test_an_empty_store_yields_no_share_rather_than_zero():
    assert av.stale_diff({"a"}, set())["stale_share"] is None


def test_the_diff_takes_tuples_so_an_edge_is_comparable_by_its_ends():
    report = av.stale_diff({("chk_1", "con_1")}, {("chk_1", "con_1"), ("chk_9", "con_9")})
    assert report["left_behind"] == 1


# --- quotes ------------------------------------------------------------------


def test_a_quote_tolerates_whitespace_and_nothing_else():
    assert av.quote_still_locates("la  regla\nde tres", "dice la regla de tres aquí")
    assert not av.quote_still_locates("la regla de dos", "dice la regla de tres aquí")


def test_a_claim_with_no_quote_is_not_a_broken_one():
    """`None` is "cannot say"; counting it as a failure would invent a defect."""
    assert av.quote_still_locates(None, "cualquier cosa") is None
    assert av.quote_still_locates("", "cualquier cosa") is None


def test_a_quote_whose_chunk_is_gone_does_not_locate():
    assert av.quote_still_locates("algo", None) is False


# --- the eval set ------------------------------------------------------------


def test_the_real_corrupted_question_is_flagged():
    question = "%C3%82%C2%BFQu%C3%A9 caracter%C3%ADsticas tiene?"
    assert av.suspect_question(question) == "percent-encoded"


def test_ordinary_spanish_is_not_flagged():
    """Accents, ñ and the opening question mark are the normal case here."""
    for question in (
        "¿Qué características tiene la niña?",
        "¿Cuál es el propósito del capítulo 3, según el autor?",
        "¿Por qué se cita a Agustín en la sección «La puerta»?",
        "Un porcentaje del 50% no es una codificación",
    ):
        assert av.suspect_question(question) is None, question


def test_mojibake_is_flagged_separately_from_percent_encoding():
    assert "mojibake" in av.suspect_question("Â¿QuÃ© pasÃ³?")


def test_scan_reports_usable_and_suspect_and_where():
    items = [
        {"question": "¿Qué dice el texto?", "chunk_index": 1},
        {"question": "%C3%82%C2%BFQu%C3%A9?", "chunk_index": 225},
    ]
    report = av.scan_questions(items)
    assert report == {
        "questions": 2, "suspect": 1, "usable": 1,
        "suspects": [
            {"position": 1, "chunk_index": 225, "why": "percent-encoded",
             "question": "%C3%82%C2%BFQu%C3%A9?"}
        ],
    }


def test_a_corrupted_question_bounds_the_misses_it_could_explain():
    suspects = [{"position": 1, "chunk_index": 225, "why": "percent-encoded"}]
    misses = [{"want": 225, "rank": -1}, {"want": 33, "rank": -1}]
    report = av.misses_explained(suspects, misses)
    assert report["misses"] == 2
    assert report["misses_from_suspect_questions"] == 1
    assert report["share"] == 0.5


def test_no_misses_yields_no_share_rather_than_zero():
    assert av.misses_explained([], [])["share"] is None


# --- scores ------------------------------------------------------------------


def test_a_difference_is_judged_against_the_runs_own_margin():
    """Quoting a delta without the margin is what makes noise look like a find."""
    report = av.compare_scores(
        {"recall_at_5": 0.8125, "mrr_at_10": 0.75},
        {"recall_at_5": 0.8125, "mrr_at_10": 0.6587},
        margin=0.0401,
    )
    by_field = {row["field"]: row for row in report["fields"]}
    assert by_field["recall_at_5"]["delta"] == 0.0
    assert by_field["recall_at_5"]["beyond_margin"] is False
    assert by_field["mrr_at_10"]["beyond_margin"] is True


def test_a_field_nobody_recorded_yields_no_verdict():
    report = av.compare_scores({"recall_at_1": 0.5}, {}, margin=0.04)
    (row,) = [r for r in report["fields"] if r["field"] == "recall_at_1"]
    assert row["delta"] is None and row["beyond_margin"] is None


def test_a_floor_below_the_noise_floor_is_not_honest():
    """It wins the metric by admitting exactly what the floor excludes."""
    assert av.floor_verdict(0.60, 0.4967)["honest"] is True
    assert av.floor_verdict(0.50, 0.5153)["honest"] is False
    assert av.floor_verdict(0.5153, 0.5153)["honest"] is False


# --- degradation -------------------------------------------------------------


def test_a_leg_that_could_not_answer_carries_no_figures():
    """A stopped store must not render as a version with nothing in it."""
    leg = av.unavailable("semantics", "bolt://127.0.0.1:7789: refused")
    assert leg["available"] is False
    assert "7789" in leg["detail"]
    # `bool` is an `int`, so the flag itself has to be excluded before this
    # says anything: the property is that no *figure* survives.
    figures = [v for v in leg.values() if isinstance(v, (int, float))
               and not isinstance(v, bool)]
    assert figures == []


def test_a_leg_that_answered_says_so_beside_its_figures():
    leg = av.leg("structure", {"chunks": 0})
    assert leg["available"] is True and leg["chunks"] == 0


# --- which stream the chunks index -------------------------------------------


def test_the_stream_is_chosen_by_measuring_not_by_precedence():
    """A run can leave three streams and only one of them was chunked.

    `raw.txt` comes last in `STREAMS` and would lose a precedence contest to
    `extracted.txt`; here it is the one that verifies, and the choice has to
    follow the bytes rather than the order.
    """
    chunked = "Primero. Segundo.".encode("utf-8")
    other = "PRIMERO. SEGUNDO.".encode("utf-8")
    chunks = [_chunk(0, 0, 8, "Primero."), _chunk(1, 9, 17, "Segundo.")]
    picked = av.choose_stream({"extracted": other, "raw": chunked}, chunks)
    assert picked["chosen"] == "raw"
    assert picked["unanimous"] is True
    assert picked["streams"]["extracted"]["spans_verified"] == 0


def test_a_profile_re_extraction_is_the_stream_a_precedence_rule_would_miss():
    """The real shape: `raw.txt` present, and wrong, beside `extracted.txt`."""
    extracted = "Primero. Segundo.".encode("utf-8")
    raw = "  Primero. Segundo.".encode("utf-8")  # two bytes of header, offsets shift
    chunks = [_chunk(0, 0, 8, "Primero.")]
    picked = av.choose_stream({"extracted": extracted, "raw": raw}, chunks)
    assert picked["chosen"] == "extracted"


def test_no_stream_verifying_is_reported_as_the_finding_it_is():
    chunks = [_chunk(0, 0, 8, "Primero.")]
    picked = av.choose_stream({"raw": b"algo completamente distinto"}, chunks)
    assert picked["chosen"] == "raw"
    assert picked["unanimous"] is False
    assert picked["report"]["spans_mismatched"] == 1


def test_a_run_with_no_stream_at_all_chooses_nothing():
    assert av.choose_stream({}, [])["chosen"] is None


# ---------------------------------------------------------------------------
# The video path's stream, and the locator that carries no title
# ---------------------------------------------------------------------------


def test_the_uncorrected_transcript_is_a_stream_this_can_choose():
    """The case the audit most needs and could not see.

    `chunk_transcript` falls back to the uncorrected stream when correction
    moves the paragraph count, so the offsets index `transcript.txt`. Before it
    was listed, `choose_stream` had only `corrected.txt` to crown and reported
    a byte-exact index as broken — which is the failure `choose_stream`'s own
    docstring exists to prevent, reached from the video direction.
    """
    assert ("transcript.txt", "transcript") in av.STREAMS
    transcript = "Primero. Segundo.".encode("utf-8")
    corrected = "Primero, y luego. Segundo.".encode("utf-8")
    chunks = [_chunk(0, 0, 8, "Primero."), _chunk(1, 9, 17, "Segundo.")]
    picked = av.choose_stream(
        {"corrected": corrected, "transcript": transcript}, chunks
    )
    assert picked["chosen"] == "transcript"
    assert picked["unanimous"] is True
    assert picked["streams"]["corrected"]["spans_verified"] == 0


def test_the_corrected_stream_still_wins_when_it_is_the_one_indexed():
    """The healthy video, so the entry above cannot be a thumb on the scale."""
    transcript = "primero segundo".encode("utf-8")
    corrected = "Primero. Segundo.".encode("utf-8")
    chunks = [_chunk(0, 0, 8, "Primero."), _chunk(1, 9, 17, "Segundo.")]
    picked = av.choose_stream(
        {"corrected": corrected, "transcript": transcript}, chunks
    )
    assert picked["chosen"] == "corrected"
    assert picked["unanimous"] is True


def test_a_timed_locator_reports_no_titles_because_it_carries_none():
    """A healthy 69-chunk video reported 69 distinct "titles" before this.

    `_locator` returns early for a timed source and carries no title at all, so
    the leading segment is a clock. A check that fires on every correct video
    teaches a reader to skip the field.
    """
    assert av.title_prefixes([
        "0:00 · https://youtu.be/yq6uVBsVkeQ?t=0",
        "1:14 · https://youtu.be/yq6uVBsVkeQ?t=74",
        "1:16:13 · https://youtu.be/yq6uVBsVkeQ?t=4573",
    ]) is None


def test_a_document_locator_still_reports_the_titles_it_was_written_for():
    assert av.title_prefixes([
        "Hermenéutica · Cap. 4 · [0:100]",
        "Hermenéutica · Cap. 5 · [100:200]",
    ]) == ["Hermenéutica"]


def test_two_titles_on_one_version_is_still_the_defect_it_always_was():
    assert av.title_prefixes([
        "El reto de Dios · [0:100]",
        "El Reto de Dios · [100:200]",
    ]) == ["El Reto de Dios", "El reto de Dios"]


def test_a_locator_with_no_separator_is_not_mistaken_for_a_clock():
    assert av.title_prefixes(["", "algo"]) == ["", "algo"]


# ---------------------------------------------------------------------------
# A chunk's span as a moment
# ---------------------------------------------------------------------------


def _timed(index, para_from, para_to, start_s, end_s):
    return {"index": index, "para_from": para_from, "para_to": para_to,
            "start_s": start_s, "end_s": end_s}


def _table(*spans):
    from brainworker.videosource import ParagraphTime

    return [ParagraphTime(i, a, b) for i, (a, b) in enumerate(spans)]


def test_a_healthy_timed_run_re_derives_every_span():
    table = _table((0.0, 10.0), (10.0, 20.0), (20.0, 30.0))
    chunks = [_timed(0, 0, 1, 0.0, 20.0), _timed(1, 2, 2, 20.0, 30.0)]
    rep = av.time_report(chunks, table, duration_s=30)
    assert rep["derivation_disagreements"] == []
    assert rep["all_timed"] is True
    assert rep["starts_non_decreasing"] is True
    assert rep["covered_s"] == 30.0
    assert rep["ends_past_duration"] == []


def test_a_span_that_disagrees_with_the_cue_table_is_named():
    """The failure that produces a citation which looks verifiable and is not."""
    table = _table((0.0, 10.0), (10.0, 20.0))
    chunks = [_timed(0, 0, 0, 0.0, 99.0)]
    rep = av.time_report(chunks, table)
    assert rep["derivation_disagreement_total"] == 1
    assert rep["derivation_disagreements"][0]["index"] == 0
    assert rep["derivation_disagreements"][0]["want"] == [0.0, 10.0]


def test_a_paragraph_range_the_table_cannot_hold_is_reported_not_clamped():
    """`span_for` raises on purpose; the raise is the finding, not a crash."""
    table = _table((0.0, 10.0))
    rep = av.time_report([_timed(0, 0, 7, 0.0, 10.0)], table)
    assert "raised" in rep["derivation_disagreements"][0]
    assert "table of 1" in rep["derivation_disagreements"][0]["raised"]


def test_a_cue_table_ending_past_the_duration_is_reported_and_not_failed():
    """Measured on the real 76-minute talk: 4574.699 against a probed 4573.

    YouTube reports duration as a truncated integer while the caption track runs
    to the true end, so asserting `end_s <= duration_s` would mark every
    auto-captioned video defective.
    """
    table = _table((0.0, 4543.199), (4543.199, 4574.699))
    chunks = [_timed(0, 0, 0, 0.0, 4543.199), _timed(1, 1, 1, 4543.199, 4574.699)]
    rep = av.time_report(chunks, table, duration_s=4573)
    assert rep["derivation_disagreements"] == []
    assert rep["ends_past_duration"] == [1]
    assert rep["covered_s"] == 4574.699


def test_no_duration_means_the_question_was_not_asked():
    rep = av.time_report([_timed(0, 0, 0, 0.0, 1.0)], _table((0.0, 1.0)))
    assert rep["ends_past_duration"] is None


def test_each_chunk_covering_its_own_paragraphs_is_the_healthy_answer():
    chunks = [_timed(0, 0, 1, 0.0, 2.0), _timed(1, 2, 2, 2.0, 3.0)]
    rep = av.para_ranges_unique(chunks)
    assert rep["unique"] is True and rep["shared_by"] == {}


def test_two_chunks_reporting_one_time_span_are_named():
    """`_split_oversized` gives every piece of a split paragraph the same index,
    so several chunks would claim one span. `GROUP_HARD_CAP` keeps it
    unreachable; this says so about a run rather than about the constant."""
    chunks = [_timed(0, 3, 3, 1.0, 2.0), _timed(1, 3, 3, 1.0, 2.0)]
    rep = av.para_ranges_unique(chunks)
    assert rep["unique"] is False
    assert list(rep["shared_by"].values()) == [[0, 1]]


# --- what a version cost, across every run that touched it -------------------


def _charge(stage, usd):
    return {"stage": stage, "usd": usd}


def test_a_stage_charged_in_two_runs_is_named():
    """The finding: on `ver_0cde3e…` the eval set was generated twice, for
    $0.5753 + $0.5710, the first of them inside a run that was cancelled —
    $1.1965 of that document's $3.7572, 31.8%. Nothing in the code was wrong
    about it; there was simply no way to see it."""
    out = av.cost_by_stage(
        [
            {"run_id": "a", "state": "cancelled", "costs": [_charge("evalset", 0.5753)]},
            {"run_id": "b", "state": "succeeded", "costs": [_charge("evalset", 0.5710)]},
        ]
    )
    assert out["charged_in_more_than_one_run"] == ["evalset"]
    assert out["by_stage"]["evalset"]["runs"] == ["a", "b"]
    assert out["total_usd"] == pytest.approx(1.1463)


def test_the_split_is_by_run_state_rather_than_a_figure_called_wasted():
    """Which spend bought something is a judgement the rows cannot make. A
    cancelled run bought nothing durable; a **failed** one can still have left a
    complete index behind — the 2026-08-31 semantics failure billed $10.017265
    and left 600 points and 5 001 claims under a version the catalog still calls
    `pending`. So the states are reported and the reader draws the line."""
    out = av.cost_by_stage(
        [
            {"run_id": "a", "state": "cancelled", "costs": [_charge("evalset", 1.0)]},
            {"run_id": "b", "state": "failed", "costs": [_charge("semantics", 2.0)]},
            {"run_id": "c", "state": "succeeded", "costs": [_charge("semantics", 3.0)]},
        ]
    )
    assert out["usd_by_run_state"] == {
        "cancelled": 1.0,
        "failed": 2.0,
        "succeeded": 3.0,
    }


def test_an_unpriced_charge_is_counted_and_never_totalled_as_zero():
    """A missing price means the model id is absent from the table, which
    under-reports the bill rather than describing a free call."""
    out = av.cost_by_stage(
        [{"run_id": "a", "state": "succeeded", "costs": [_charge("semantics", None)]}]
    )
    assert out["by_stage"]["semantics"]["unpriced_entries"] == 1
    assert out["total_usd"] == 0.0
    assert out["usd_by_run_state"] == {}


def test_a_run_that_spent_nothing_still_appears_in_no_stage():
    out = av.cost_by_stage([{"run_id": "a", "state": "succeeded", "costs": []}])
    assert out == {
        "by_stage": {},
        "total_usd": 0.0,
        "charged_in_more_than_one_run": [],
        "usd_by_run_state": {},
    }


# --- what the document does with its claims ----------------------------------


def test_a_claim_with_no_status_keeps_its_own_key():
    """`sin_estado` must never render as `afirma`: a text expounding the
    doctrine it is about to rebut enunciates it in the same words as one who
    holds it. Measured on a real document, 14% of claims were not plain
    assertions — 10 `atribuido` and 1 `niega` out of 79."""
    out = av.claim_shape(
        [
            {"status": "afirma", "claims": 68, "with_quote": 66},
            {"status": None, "claims": 1, "with_quote": 0},
        ]
    )
    assert out["by_status"] == {"afirma": 68, "sin_estado": 1}
    assert (out["claims"], out["with_a_quote"]) == (69, 66)


# --- the set difference, keyed on the pre-images ------------------------------
#
# The whole reason these keys are not the derived ids: the paid plane has to
# make the same comparison, and keying on `claim_id`/`concept_id` would fork the
# tenant-salted id contract into TypeScript. A drift in `_salt()` is a silently
# wrong graph, not a wrong number.


def test_the_claim_key_partitions_exactly_as_claim_id_does():
    """`claim_id` is `digest(source_chunk_id, collapse_space(text))`, so equal
    keys and equal ids are the same relation — including the whitespace fold,
    without which two claims that are one node in the graph would be two keys
    here and one of them would read as `missing`."""
    from brainworker.graph.schema import claim_id

    rows = [
        {"source_chunk_id": "chk_1", "text": "hola  mundo"},
        {"source_chunk_id": "chk_1", "text": "hola mundo"},
        {"source_chunk_id": "chk_2", "text": "hola mundo"},
    ]
    keys = {av.claim_key(r) for r in rows}
    ids = {claim_id(r["source_chunk_id"], r["text"]) for r in rows}
    assert len(keys) == len(ids) == 2


def test_concepts_come_off_the_edges_by_all_three_routes():
    """`MENTIONS`, a claim's `ABOUT` and a claim's `INVOLVES` — deliberately the
    same three `CONCEPTS_REACHED` walks. A concept reached only as a claim's
    subject carries no `MENTIONS` edge from any chunk, and following the first
    route alone is how claim-only concepts were orphaned once already."""
    doc = {
        "edges": [
            {"type": "MENTIONS", "source_id": "chk_1", "target_id": "con_a"},
            {"type": "ABOUT", "source_id": "clm_1", "target_id": "con_b"},
            {"type": "INVOLVES", "source_id": "clm_1", "target_id": "con_c"},
            {"type": "SUPPORTS", "source_id": "clm_1", "target_id": "clm_2"},
        ]
    }
    assert av.concepts_produced(doc) == {"con_a", "con_b", "con_c"}


def test_a_re_index_that_left_the_previous_extraction_behind_is_visible():
    """Measured on `ver_0b71d21eeb3228f54437d9cf`, re-indexed with a corrected
    profile that took it from 600 chunks to 631: 5 001 claims where the run
    extracted 3 055 — 4 988 left behind, and not inert, because `claim_id` keys
    on `(chunk_id, text)` while the text underneath moved."""
    doc = {"claims": [{"source_chunk_id": "chk_1", "text": "nuevo"}], "edges": []}
    graph = [
        {"source_chunk_id": "chk_1", "text": "nuevo"},
        {"source_chunk_id": "chk_1", "text": "viejo"},
    ]
    out = av.semantic_diff(doc, claims=graph, concepts=[], mentions=[])
    assert out["claims"]["produced"] == 1
    assert out["claims"]["left_behind"] == 1
    assert out["claims"]["converged"] == 1
    assert [c["text"] for c in av.stale_claims(doc, graph)] == ["viejo"]


def test_the_names_a_run_extracted_are_reported_beside_the_folded_count():
    """`canonical_concept` strips accents and punctuation, so "Espíritu Santo"
    and "Espiritu santo" are one node and two names. A reader comparing
    `produced` against `semantics.json` would otherwise read the fold as a
    loss. Measured on a real artifact: 2 634 names, 2 517 concepts."""
    doc = {
        "claims": [],
        "concepts": [{"name": "Espíritu Santo"}, {"name": "Espiritu santo"}],
        "edges": [{"type": "MENTIONS", "source_id": "chk_1", "target_id": "con_a"}],
    }
    out = av.semantic_diff(doc, claims=[], concepts=[{"id": "con_a"}], mentions=[])
    assert out["concepts"]["names_extracted"] == 2
    assert out["concepts"]["produced"] == 1
    assert out["concepts"]["converged"] == 1
