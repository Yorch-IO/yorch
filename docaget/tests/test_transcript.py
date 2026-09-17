"""Transcript parsing, grouping, and the property a video citation rests on.

The last test in this file is the one that matters: it runs the *real* chunker
over a real grouped transcript and asserts that a chunk's ``para_from`` /
``para_to`` recover the times of the cues inside it. That is the whole claim a
timestamped deep link makes, and it is checkable with no network, no stores and
no video.
"""

from __future__ import annotations

import pytest

from docagent import transcript as t
from docagent.chunk import (
    KIND_TRANSCRIPT,
    ChunkRules,
    build_chunks,
    heading_level,
    split_paragraphs,
)
from docagent.extract import join_paragraphs

VTT = b"""WEBVTT
Kind: captions
Language: es

NOTE this block is not a cue

1
00:00:00.320 --> 00:00:02.000 align:start position:0%
hola <00:00:01.000><c>que</c> tal

00:00:02.000 --> 00:00:04.500
hola que tal amigos&amp;compania.

00:01:09.000 --> 00:01:11.250
despues de una pausa larga
"""


def test_parse_vtt_reads_times_strips_markup_and_skips_non_cue_blocks():
    cues = t.parse_vtt(VTT)
    assert [c.text for c in cues] == [
        "hola que tal",
        "hola que tal amigos&compania.",
        "despues de una pausa larga",
    ]
    assert cues[0].start_s == pytest.approx(0.32)
    assert cues[0].end_s == pytest.approx(2.0)
    # An hours field, and a cue preceded by an identifier line, both survive.
    assert cues[2].start_s == pytest.approx(69.0)
    assert cues[2].end_s == pytest.approx(71.25)


def test_parse_vtt_accepts_the_srt_comma_and_an_hours_field():
    cues = t.parse_vtt(b"WEBVTT\n\n01:02:03,004 --> 01:02:04,500\nhola\n")
    assert cues[0].start_s == pytest.approx(3723.004)
    assert cues[0].end_s == pytest.approx(3724.5)


def test_parse_vtt_drops_a_cue_that_is_empty_after_stripping():
    # An empty paragraph would be dropped by join_paragraphs *after* the time
    # table was built, which is precisely how an index shift is introduced.
    cues = t.parse_vtt(b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<c></c>\n")
    assert cues == []


def test_parse_transcribe_attaches_punctuation_to_the_word_before_it():
    payload = {
        "results": {
            "items": [
                _word("Hola", 0.0, 0.5),
                {"type": "punctuation", "alternatives": [{"content": ","}]},
                _word("mundo", 0.6, 1.2),
                {"type": "punctuation", "alternatives": [{"content": "."}]},
            ]
        }
    }
    cues = t.parse_transcribe(payload)
    assert [(c.text, c.start_s) for c in cues] == [("Hola,", 0.0), ("mundo.", 0.6)]


def test_parse_transcribe_keeps_a_word_that_carries_no_timing():
    payload = {
        "results": {
            "items": [
                _word("Hola", 0.0, 0.5),
                {"type": "pronunciation", "alternatives": [{"content": "mundo"}]},
            ]
        }
    }
    # Text wins over span precision: the span is already a range, and dropping
    # the word would lose it from the index entirely.
    assert [c.text for c in t.parse_transcribe(payload)] == ["Hola mundo"]


def test_parse_transcribe_refuses_a_document_that_is_not_one():
    with pytest.raises(t.TranscriptError):
        t.parse_transcribe({"jobName": "x"})


def _word(content: str, start: float, end: float) -> dict:
    return {
        "type": "pronunciation",
        "start_time": str(start),
        "end_time": str(end),
        "alternatives": [{"content": content}],
    }


# --- rolling captions --------------------------------------------------------


def test_dedupe_rolling_removes_the_scrolled_repeat():
    cues = [
        t.Cue(0.0, 1.0, "en el principio"),
        t.Cue(1.0, 2.0, "en el principio creo Dios"),
        t.Cue(2.0, 3.0, "creo Dios los cielos"),
    ]
    assert [c.text for c in t.dedupe_rolling(cues)] == [
        "en el principio",
        "creo Dios",
        "los cielos",
    ]


def test_dedupe_rolling_drops_a_cue_that_repeats_its_predecessor_entirely():
    cues = [t.Cue(0.0, 1.0, "hola que tal"), t.Cue(1.0, 2.0, "hola que tal")]
    assert [c.text for c in t.dedupe_rolling(cues)] == ["hola que tal"]


def test_dedupe_rolling_is_word_aligned_not_character_aligned():
    # "cielos" ends with "los" and the next cue starts with "los cielos"; a
    # character-aligned rule would eat the repeat's first word. A partial word
    # overlap is a coincidence, not a scroll.
    cues = [t.Cue(0.0, 1.0, "los cielos"), t.Cue(1.0, 2.0, "loso pardo")]
    assert [c.text for c in t.dedupe_rolling(cues)] == ["los cielos", "loso pardo"]


# --- grouping ----------------------------------------------------------------


def test_group_cues_closes_on_a_silence():
    cues = [t.Cue(0.0, 1.0, "antes"), t.Cue(9.0, 10.0, "despues")]
    groups = t.group_cues(cues, target_chars=1000, hard_cap_chars=1000, max_gap_s=2.0)
    assert [g.text for g in groups] == ["antes", "despues"]


def test_group_cues_closes_at_a_sentence_end_once_past_the_target():
    cues = [
        t.Cue(0.0, 1.0, "una frase que no termina"),
        t.Cue(1.0, 2.0, "y aqui si termina."),
        t.Cue(2.0, 3.0, "lo siguiente"),
    ]
    groups = t.group_cues(cues, target_chars=20, hard_cap_chars=500, max_gap_s=99)
    assert [g.text for g in groups] == [
        "una frase que no termina y aqui si termina.",
        "lo siguiente",
    ]


def test_group_cues_closes_at_the_hard_cap_whatever_the_punctuation_says():
    # No cue ends a sentence and no silence falls between them, so the cap is
    # the only rule that can close a group here.
    cues = [t.Cue(float(i), float(i) + 1, "palabra") for i in range(10)]
    groups = t.group_cues(cues, target_chars=20, hard_cap_chars=20, max_gap_s=99)
    assert len(groups) > 1
    assert all(len(g.text) <= 20 for g in groups)


def test_group_cues_carries_the_span_of_the_cues_inside_it():
    cues = [t.Cue(1.5, 2.0, "uno"), t.Cue(2.0, 4.25, "dos")]
    (group,) = t.group_cues(cues, target_chars=500, hard_cap_chars=500, max_gap_s=99)
    assert (group.start_s, group.end_s, group.cues) == (1.5, 4.25, 2)


def test_group_cues_keeps_every_cue_exactly_once_and_in_order():
    cues = [t.Cue(float(i), float(i) + 0.9, f"w{i}") for i in range(200)]
    groups = t.group_cues(cues, target_chars=30, hard_cap_chars=60, max_gap_s=0.5)
    words = " ".join(g.text for g in groups).split()
    assert words == [c.text for c in cues]


def test_group_cues_of_nothing_is_nothing():
    assert t.group_cues([]) == []


def test_group_cues_refuses_a_cap_below_its_target():
    with pytest.raises(ValueError):
        t.group_cues([], target_chars=100, hard_cap_chars=10)


# --- how a transcript is chunked ---------------------------------------------


def test_transcript_rules_suppress_headings_that_the_defaults_would_accept():
    # Measured on the shapes a Spanish transcript actually produces: each of
    # these is a level-1 chapter under the built-in rules, and would become the
    # breadcrumb of every chunk after it.
    for line in ("1975 Fue un ano decisivo", "2 Corintios habla de esto"):
        assert heading_level(line, ChunkRules()) == 1
        assert heading_level(line, t.chunk_rules()) == 0


def test_the_transcript_classifier_labels_everything_as_a_transcript():
    assert t.classify("¿Y esto es una pregunta?", t.chunk_rules()) == KIND_TRANSCRIPT


# --- the property a timestamped citation is a claim about ---------------------


def _spoken_transcript(n_cues: int = 900) -> list[t.Cue]:
    """Cues with a unique token each, so a chunk's text names its own cues."""
    cues = []
    at = 0.0
    for i in range(n_cues):
        # A sentence end every eleventh cue, and a real pause every 40th, so the
        # grouper's three rules all fire somewhere in the run.
        word = f"tok{i}." if i % 11 == 10 else f"tok{i}"
        cues.append(t.Cue(at, at + 0.5, word))
        at += 0.6 if i % 40 else 3.0
    return cues


def test_a_chunk_recovers_the_times_of_the_cues_inside_it():
    """The whole feature, asserted without a network.

    Group the cues, join them the way the pipeline does, chunk them with the
    real chunker, and check that the span derived from ``para_from``/``para_to``
    contains every cue whose text is in that chunk. Nothing here reads a byte
    offset: the mapping is paragraph indices, which is the point.
    """
    groups = t.group_cues(_spoken_transcript())
    data = join_paragraphs(t.to_paragraphs(groups))
    paras = split_paragraphs(data)

    # The mapping the whole design rests on: paragraph i of the byte stream is
    # group i. Everything below is a consequence of this line.
    assert len(paras) == len(groups)

    chunks = build_chunks(data, paras, t.chunk_rules(), t.classify)
    assert len(chunks) > 3, "need several chunks for this to be worth asserting"

    by_token = {c.text.rstrip("."): c for c in _spoken_transcript()}
    checked = 0
    for chunk in chunks:
        start = groups[chunk.para_from].start_s
        end = groups[chunk.para_to].end_s
        assert start <= end
        for token in chunk.text.split():
            cue = by_token[token.rstrip(".")]
            assert start <= cue.start_s and cue.end_s <= end, (
                f"chunk {chunk.index} spans [{start}, {end}] "
                f"but contains {token!r} spoken at [{cue.start_s}, {cue.end_s}]"
            )
            checked += 1
    assert checked > 500


def test_no_group_ever_reaches_the_chunkers_cap_so_no_two_chunks_share_a_span():
    """`_split_oversized` gives every piece of a split paragraph the same
    ``para_idx``, so a group at the cap would make two chunks report one time.
    The defaults must keep that path unreachable."""
    groups = t.group_cues(_spoken_transcript())
    cap = ChunkRules().hard_cap_chars
    assert max(len(g.text.encode("utf-8")) for g in groups) < cap

    data = join_paragraphs(t.to_paragraphs(groups))
    chunks = build_chunks(data, split_paragraphs(data), t.chunk_rules(), t.classify)
    spans = [(c.para_from, c.para_to) for c in chunks]
    assert len(spans) == len(set(spans)), "two chunks share a paragraph range"


def test_a_transcript_chunks_with_no_chapter_and_no_breadcrumb():
    groups = t.group_cues(_spoken_transcript(120))
    data = join_paragraphs(t.to_paragraphs(groups))
    chunks = build_chunks(data, split_paragraphs(data), t.chunk_rules(), t.classify)
    assert chunks
    assert all(c.chapter == "" and c.section == "" for c in chunks)
    assert all(c.breadcrumb() == "" for c in chunks)
    assert all(c.kind == KIND_TRANSCRIPT for c in chunks)


def test_a_group_exceeds_the_cap_only_when_one_cue_does():
    # Nothing can split a single cue without inventing a time for the halves,
    # so this is the one case the bound cannot hold — and it must be the only
    # one, or `_split_oversized` becomes reachable.
    cues = [
        t.Cue(0.0, 1.0, "corto"),
        t.Cue(1.0, 2.0, "x" * 200),
        t.Cue(2.0, 3.0, "corto"),
    ]
    groups = t.group_cues(cues, target_chars=50, hard_cap_chars=50, max_gap_s=99)
    over = [g for g in groups if len(g.text) > 50]
    assert [g.cues for g in over] == [1]


# --- whisper.cpp -------------------------------------------------------------


def _whisper(segments):
    return {
        "systeminfo": "x", "model": {"type": "large-v3-turbo"},
        "params": {"model": "ggml-large-v3-turbo.bin", "language": "es", "translate": False},
        "result": {"language": "es"},
        "transcription": segments,
    }


def test_whisper_segments_become_cues_with_millisecond_offsets_in_seconds():
    from docagent.transcript import parse_any, parse_whisper, transcript_engine

    doc = _whisper([
        {"timestamps": {"from": "00:00:00,000", "to": "00:00:04,500"},
         "offsets": {"from": 0, "to": 4500}, "text": " Hermanos, buenas noches."},
        {"timestamps": {"from": "00:00:04,500", "to": "00:00:09,000"},
         "offsets": {"from": 4500, "to": 9000}, "text": "   "},
        {"timestamps": {"from": "00:00:09,000", "to": "00:00:12,250"},
         "offsets": {"from": 9000, "to": 12250}, "text": " Abramos en Juan 3:16."},
    ])
    cues = parse_whisper(doc)
    assert [(c.start_s, c.end_s, c.text) for c in cues] == [
        (0.0, 4.5, "Hermanos, buenas noches."),
        (9.0, 12.25, "Abramos en Juan 3:16."),
    ]
    assert transcript_engine(doc) == "whisper"
    assert parse_any(doc) == cues


def test_a_segment_with_no_offsets_keeps_its_words_on_the_previous_cue():
    from docagent.transcript import parse_whisper

    doc = _whisper([
        {"offsets": {"from": 0, "to": 1000}, "text": "uno"},
        {"text": "dos"},
    ])
    assert [c.text for c in parse_whisper(doc)] == ["uno dos"]


def test_the_engine_is_read_off_the_shape_and_neither_is_refused():
    import pytest

    from docagent.transcript import TranscriptError, transcript_engine

    assert transcript_engine({"results": {"items": []}}) == "transcribe"
    with pytest.raises(TranscriptError):
        transcript_engine({"hello": "world"})
    with pytest.raises(TranscriptError):
        transcript_engine([])
