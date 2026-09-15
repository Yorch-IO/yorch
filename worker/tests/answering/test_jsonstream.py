"""What a partial JSON envelope decodes to, one chunk at a time.

A table of adversarial strings rather than a live stream, for the reason the
module's own docstring gives. Every case here is one that produced wrong text
in a hand-written first draft, or that a real model can produce: Spanish prose
carries accents, and an accent inside a JSON string is either a raw UTF-8
character or a `\\u00e1` escape depending on how the SDK decoded the wire.
"""

from __future__ import annotations

import json

import pytest

from brainworker.answering.jsonstream import HOLD_BACK, FieldStreamer


def drain(chunks: list[str], *, field: str = "respuesta", hold_back: int = 0) -> str:
    s = FieldStreamer(field, hold_back=hold_back)
    out = "".join(s.feed(c) for c in chunks)
    return out + s.finish()


def test_a_whole_envelope_in_one_piece():
    assert drain(['{"suficiente": true, "respuesta": "hola mundo"}']) == "hola mundo"


def test_the_field_arrives_across_many_pieces():
    chunks = ['{"sufi', 'ciente": tr', 'ue, "respue', 'sta": "hola', ' mun', 'do"}']
    assert drain(chunks) == "hola mundo"


def test_an_earlier_value_containing_the_field_name_is_not_the_field():
    # The model explains itself in `motivo` using the word `respuesta`, which a
    # `find()` would take for the key and a scanner does not.
    raw = '{"motivo": "no hay respuesta: ...", "respuesta": "la verdadera"}'
    assert drain([raw]) == "la verdadera"


def test_a_key_of_the_same_name_nested_deeper_is_not_the_field():
    raw = '{"citas": [{"respuesta": "anidada"}], "respuesta": "de arriba"}'
    assert drain([raw]) == "de arriba"


def test_an_escaped_quote_does_not_end_the_string():
    raw = r'{"respuesta": "dijo \"hola\" y se fue"}'
    assert drain([raw]) == 'dijo "hola" y se fue'


def test_an_escape_split_across_the_boundary():
    # The backslash lands in one chunk and the `n` in the next.
    assert drain(['{"respuesta": "linea', "\\", 'nsiguiente"}']) == "linea\nsiguiente"


def test_a_unicode_escape_split_across_three_boundaries():
    assert drain(['{"respuesta": "caf', "\\u", "00", 'e9"}']) == "café"


def test_a_surrogate_pair_is_one_character():
    assert drain([r'{"respuesta": "😀"}']) == "\U0001f600"


def test_a_surrogate_pair_split_between_its_halves():
    assert drain([r'{"respuesta": "\ud83d', r'\ude00"}']) == "\U0001f600"


def test_an_unpaired_high_surrogate_becomes_the_replacement_character():
    # Never released as a lone surrogate: that is a `str` Python builds and then
    # refuses to encode, and the failure would surface far from here.
    out = drain([r'{"respuesta": "\ud83dx"}'])
    assert out == "�x"
    out.encode("utf-8")


def test_the_decoded_text_matches_what_json_itself_would_produce():
    value = 'Con acentos: ñáéíóú — "comillas", \\barra, salto\nlínea, emoji \U0001f600'
    raw = json.dumps({"suficiente": True, "respuesta": value, "citas": []})
    # Fed one character at a time, which is the worst case for every boundary.
    assert drain(list(raw)) == value


def test_a_truncated_stream_keeps_the_draft_it_had():
    # MAX_TOKENS cuts the envelope mid-string; the characters already decoded
    # are still the best draft there is.
    assert drain(['{"respuesta": "a medio escr']) == "a medio escr"


def test_nothing_after_the_field_is_scanned():
    s = FieldStreamer("respuesta", hold_back=0)
    assert s.feed('{"respuesta": "listo",') == "listo"
    assert s.closed
    assert s.feed(' "citas": [ ... anything at all ... ]}') == ""


# -- the hold-back ---------------------------------------------------------


def test_the_tail_is_withheld_until_more_arrives():
    s = FieldStreamer("respuesta")
    assert s.feed('{"respuesta": "' + "x" * 10) == ""
    assert s.feed("y" * 100) == ("x" * 10 + "y" * 100)[:-HOLD_BACK]


def test_the_withheld_tail_is_released_when_the_field_closes():
    s = FieldStreamer("respuesta")
    body = "z" * 20
    assert s.feed('{"respuesta": "' + body) == ""
    assert s.feed('"}') == body


def test_a_chunk_id_is_never_released_half_written():
    # `answer._clean` strips `chk_` + 24 hex from the prose. Releasing the first
    # half would show the reader an id the finished answer does not contain.
    chunk_id = "chk_" + "0123456789ab" * 2
    assert len(chunk_id) == 28
    s = FieldStreamer("respuesta")
    shown = s.feed('{"respuesta": "ver [' + chunk_id[:15])
    assert shown == ""
    shown += s.feed(chunk_id[15:] + '] y ya"}')
    assert shown == f"ver [{chunk_id}] y ya"


@pytest.mark.parametrize("hold", [0, 1, 7, HOLD_BACK, 500])
def test_the_hold_back_never_changes_what_is_finally_released(hold):
    raw = '{"suficiente": true, "respuesta": "una respuesta cualquiera", "citas": []}'
    assert drain(list(raw), hold_back=hold) == "una respuesta cualquiera"


# -- malformed envelopes ---------------------------------------------------
#
# Not a hypothetical input class. The envelope is written by a model and can be
# cut off mid-object by `MAX_TOKENS`, so the scanner has to stay wrong-in-a-safe
# -direction rather than latch onto the first thing that looks like the field.


def test_a_key_with_no_colon_does_not_capture_the_string_after_it():
    # `{"respuesta" "trampa"}` — the key never became a key, so nothing that
    # follows it is that key's value.
    assert drain(['{"respuesta" "trampa"}']) == ""


def test_a_value_equal_to_the_field_name_does_not_make_the_next_string_the_field():
    assert drain(['{"a": "respuesta" "trampa"}']) == ""


def test_a_value_equal_to_the_field_name_followed_by_the_real_field():
    assert drain(['{"a": "respuesta", "respuesta": "real"}']) == "real"
