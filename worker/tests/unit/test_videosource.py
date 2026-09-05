"""A video's identity, and the paragraph-to-time mapping a citation rests on.

No Temporal, no AWS, no stores. The interesting test is the last one: it puts a
real correction-shaped mangling through the real chunker and shows the guard
catching it.
"""

from __future__ import annotations

import pytest
from docagent import transcript as dt
from docagent.chunk import build_chunks, split_paragraphs
from docagent.extract import join_paragraphs

from brainworker import videosource as v

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/watch?v={VID}&list=PLabc&index=2&t=30s",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?si=Ab_c-D",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://music.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/embed/{VID}?start=10",
        f"https://www.youtube.com/live/{VID}",
        f"youtube.com/watch?v={VID}",
        f"  https://youtu.be/{VID}  ",
    ],
)
def test_video_id_reads_every_shape_a_link_arrives_in(url):
    assert v.video_id(url) == VID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https://vimeo.com/123456",
        "https://www.youtube.com/playlist?list=PLabc",
        "https://www.youtube.com/@canal",
        "https://www.youtube.com/watch?v=tooshort",
        "https://www.youtube.com/watch",
        "not a url at all",
    ],
)
def test_video_id_refuses_anything_that_is_not_one_video(url):
    with pytest.raises(v.NotAVideoUrl):
        v.video_id(url)


def test_the_id_is_the_documents_key_and_the_title_is_not():
    # A title is something the uploader can change; document_id is
    # digest(library, source_key), so the key has to be the stable half.
    assert v.source_key(VID) == f"youtube/{VID}"


def test_watch_url_floors_the_offset_rather_than_rounding_it():
    # Rounding up can land after the word being cited.
    assert v.watch_url(VID, 754.9) == f"https://youtu.be/{VID}?t=754"
    assert v.watch_url(VID, 0.0) == f"https://youtu.be/{VID}?t=0"
    assert v.watch_url(VID) == f"https://youtu.be/{VID}"
    assert v.watch_url(VID, -5) == f"https://youtu.be/{VID}?t=0"


def test_hhmmss_grows_a_field_only_when_it_needs_one():
    assert v.hhmmss(0) == "0:00"
    assert v.hhmmss(9.99) == "0:09"
    assert v.hhmmss(754) == "12:34"
    assert v.hhmmss(3600) == "1:00:00"
    assert v.hhmmss(3754) == "1:02:34"
    assert v.hhmmss(-1) == "0:00"


# --- the time table ----------------------------------------------------------


def _table(*spans) -> list[v.ParagraphTime]:
    return [v.ParagraphTime(i, a, b) for i, (a, b) in enumerate(spans)]


def test_table_from_groups_numbers_the_paragraphs_in_order():
    groups = dt.group_cues([dt.Cue(0.0, 1.0, "uno"), dt.Cue(9.0, 10.0, "dos")])
    table = v.table_from_groups(groups)
    assert [(t.idx, t.start_s, t.end_s) for t in table] == [(0, 0.0, 1.0), (1, 9.0, 10.0)]


def test_span_for_spans_the_first_paragraphs_start_to_the_lasts_end():
    table = _table((0.0, 5.0), (5.0, 9.0), (9.5, 14.0))
    assert v.span_for(0, 2, table) == (0.0, 14.0)
    assert v.span_for(1, 1, table) == (5.0, 9.0)


@pytest.mark.parametrize("args", [(0, 3), (-1, 1), (2, 1), (5, 5)])
def test_span_for_raises_rather_than_clamping(args):
    # A clamped span is a wrong answer wearing the shape of a right one.
    with pytest.raises(v.TimeAlignmentLost):
        v.span_for(*args, _table((0.0, 5.0), (5.0, 9.0), (9.5, 14.0)))


def test_span_for_refuses_an_empty_table():
    with pytest.raises(v.TimeAlignmentLost):
        v.span_for(0, 0, [])


# --- the repair and the guard ------------------------------------------------


def test_repair_collapses_a_blank_line_a_correction_put_inside_a_paragraph():
    out = v.repair_paragraphs(["uno\n\ndos", "tres"])
    assert out == ["uno\ndos", "tres"]
    assert len(split_paragraphs(join_paragraphs(out))) == 2


def test_repair_keeps_every_character_of_the_prose():
    before = "El Señor dijo\n\n\n  a Moisés  "
    (after,) = v.repair_paragraphs([before])
    assert after.split() == before.split()


def test_assert_aligned_refuses_a_count_that_no_longer_matches():
    with pytest.raises(v.TimeAlignmentLost, match="3 paragraphs against 2"):
        v.assert_aligned(["a", "b", "c"], _table((0.0, 1.0), (1.0, 2.0)))


def test_assert_aligned_refuses_an_empty_paragraph_the_join_would_drop():
    # This is the shift as it actually arrives: the count still matches here,
    # and only becomes wrong once join_paragraphs filters the blank one out.
    with pytest.raises(v.TimeAlignmentLost, match="would be dropped"):
        v.assert_aligned(["a", "   ", "c"], _table((0.0, 1.0), (1.0, 2.0), (2.0, 3.0)))


def test_assert_aligned_passes_the_ordinary_case():
    v.assert_aligned(["a", "b"], _table((0.0, 1.0), (1.0, 2.0)))


# --- correction, end to end, against the real chunker ------------------------


def _spoken(n: int = 600) -> list[dt.Cue]:
    cues, at = [], 0.0
    for i in range(n):
        cues.append(dt.Cue(at, at + 0.5, f"tok{i}." if i % 9 == 8 else f"tok{i}"))
        at += 0.6 if i % 30 else 3.0
    return cues


def test_a_correction_that_adds_a_blank_line_does_not_move_any_timestamp():
    """The one way the mapping breaks, repaired and then re-checked."""
    groups = dt.group_cues(_spoken())
    table = v.table_from_groups(groups)
    paragraphs = dt.to_paragraphs(groups)

    # What `correct_paragraphs` is contracted to return — same count, same
    # order — except that paragraph 4 came back with a blank line in it.
    corrected = list(paragraphs)
    corrected[4] = corrected[4].replace(" ", "\n\n", 1)

    repaired = v.repair_paragraphs(corrected)
    v.assert_aligned(repaired, table)

    data = join_paragraphs(repaired)
    paras = split_paragraphs(data)
    assert len(paras) == len(table)

    chunks = build_chunks(data, paras, dt.chunk_rules(), dt.classify)
    spoken = {c.text.rstrip("."): c for c in _spoken()}
    for chunk in chunks:
        start, end = v.span_for(chunk.para_from, chunk.para_to, table)
        for token in chunk.text.split():
            cue = spoken[token.rstrip(".")]
            assert start <= cue.start_s and cue.end_s <= end


def test_without_the_repair_the_guard_is_what_stops_a_wrong_timestamp():
    """Skipping the repair must fail loudly, not silently shift the table."""
    groups = dt.group_cues(_spoken(120))
    table = v.table_from_groups(groups)
    corrected = list(dt.to_paragraphs(groups))
    corrected[2] = corrected[2].replace(" ", "\n\n", 1)

    data = join_paragraphs(corrected)
    assert len(split_paragraphs(data)) != len(table), "the shift must be real"
    with pytest.raises(v.TimeAlignmentLost):
        v.assert_aligned([p.text for p in split_paragraphs(data)], table)


# --- content that reached no chunk -------------------------------------------


def test_every_paragraph_of_a_real_transcript_reaches_a_chunk():
    groups = dt.group_cues(_spoken())
    table = v.table_from_groups(groups)
    data = join_paragraphs(dt.to_paragraphs(groups))
    chunks = build_chunks(data, split_paragraphs(data), dt.chunk_rules(), dt.classify)
    assert v.uncovered_paragraphs(chunks, table) == []


def test_uncovered_paragraphs_names_the_ones_a_heading_swallowed():
    """`build_chunks` drops a heading's own text. Under the default rules a
    numbered transcript line is a heading, so this is the loss the transcript
    rules exist to prevent — shown here by turning them off."""
    from docagent.chunk import ChunkRules

    groups = dt.group_cues(
        [dt.Cue(0.0, 1.0, "Hablemos de esto ahora mismo con calma y detalle."),
         dt.Cue(9.0, 10.0, "2 Corintios Dice"),
         dt.Cue(19.0, 20.0, "y seguimos hablando del asunto con mucho detalle.")],
        target_chars=10, hard_cap_chars=500, max_gap_s=2.0,
    )
    table = v.table_from_groups(groups)
    data = join_paragraphs(dt.to_paragraphs(groups))
    paras = split_paragraphs(data)

    lost = build_chunks(data, paras, ChunkRules(), dt.classify)
    assert v.uncovered_paragraphs(lost, table) == [1]

    kept = build_chunks(data, paras, dt.chunk_rules(), dt.classify)
    assert v.uncovered_paragraphs(kept, table) == []


# --- pricing -----------------------------------------------------------------


def test_transcribe_usd_is_none_when_no_rate_is_configured():
    # None, not 0.0: "we cannot price this" and "this is free" are different
    # answers, and a gate rendering one as the other invites somebody to approve
    # an unknown amount believing it was nothing.
    assert v.transcribe_usd(600, 0.0) is None
    assert v.transcribe_usd(600, -1) is None


def test_transcribe_usd_applies_the_per_request_floor():
    # Rounding up to the floor is the safe direction; quoting 10 seconds of a
    # 15-second minimum would undershoot the bill.
    assert v.transcribe_usd(10, 0.024) == pytest.approx(0.024 * 15 / 60)
    assert v.transcribe_usd(60, 0.024) == pytest.approx(0.024)
    assert v.transcribe_usd(3600, 0.024) == pytest.approx(1.44)


def test_projected_characters_scales_with_duration_and_never_goes_negative():
    assert v.projected_characters(60) == int(60 * v.CHARS_PER_SECOND_OF_SPEECH)
    assert v.projected_characters(0) == 0
    assert v.projected_characters(-5) == 0


def test_the_high_projection_is_wider_than_the_low_one():
    """The constant was declared and used by nothing until it was measured —
    the same defect as `ChunkNode.sheet`. A spread nothing widens is a spread
    that does not exist."""
    assert v.projected_characters(3600, high=True) > v.projected_characters(3600)


#: What `scripts/measure_speech_rate.py` found on 2026-09-05 over 11.21 hours of
#: real Spanish preaching and theology video — the domain this corpus indexes.
MEASURED_POOLED = 13.59
MEASURED_MAX = 15.24
#: Transcribe keeps the fillers a caption writer drops: 225 characters against
#: the captions' 217 on the same audio.
TRANSCRIBE_UPLIFT = 1.04


def test_the_low_end_stays_within_reach_of_a_typical_video():
    """The rule the range binds: the low figure is what a typical video costs,
    the high one covers the worst. A low end above the *fastest* video in an
    11-hour sample is not a low end — which is what 16.0 was."""
    typical = MEASURED_POOLED * TRANSCRIBE_UPLIFT
    assert typical <= v.CHARS_PER_SECOND_OF_SPEECH <= MEASURED_MAX * TRANSCRIBE_UPLIFT


def test_the_high_end_covers_the_fastest_video_that_was_measured():
    """Under-reporting a bill is the failure the gate exists to prevent."""
    high = v.CHARS_PER_SECOND_OF_SPEECH * v.SPEECH_RATE_SPREAD
    assert high >= MEASURED_MAX * TRANSCRIBE_UPLIFT
    # And not wildly: over-reporting pushes somebody to decline affordable work.
    assert high <= 2 * MEASURED_MAX


# --- what the gate suggests --------------------------------------------------


def test_correction_defaults_on_only_for_automatic_captions():
    # Auto-captions have no punctuation, which is the one deficit correction can
    # close without `verify` rejecting the change.
    assert v.correction_default("auto") is True
    # A human already punctuated these, and Amazon punctuates its own output.
    assert v.correction_default("manual") is False
    assert v.correction_default(None) is False
