"""Editing a chunk, and the six things that keep it from costing more than
the byte-exact span it deliberately gives up.

Every one of these fails silently if it goes wrong: a stale override attaches a
correction to the wrong passage, an un-rebuilt sparse vector finds a chunk by
words it no longer holds, a missing locator makes an edited chunk uncitable,
and a positive `enabled` flag hides a corpus while every log line reads as
healthy. None of them raises.
"""

from __future__ import annotations

import hashlib

import pytest

from brainworker.catalog.repo import ChunkOverride
from brainworker.indexing import QdrantWriter, StoredChunk, apply_overrides


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk(index: int = 0, text: str = "el texto original", **kw) -> StoredChunk:
    base = dict(
        index=index, kind="cuerpo", chapter="Cap", section="Sec", text=text,
        context="", char_from=0, char_to=len(text), cell_ref="",
        _embed_text=f"Cap > Sec\n\n{text}",
    )
    return StoredChunk(**{**base, **kw})


def override(index: int = 0, *, against: str = "el texto original",
             text: str | None = "el texto corregido", disabled: bool = False):
    return ChunkOverride(
        version_id="ver_x", chunk_index=index, text=text,
        disabled=disabled, replaced_sha256=sha(against),
    )


# --- 1. the edit reaches the vector, not only the payload --------------------


def test_the_edited_text_is_what_gets_embedded():
    """Substituted before the engine sees the chunk, so the dense vector, the
    BM25 vector, the scripture filters and the payload are all of the same
    words. Replacing it in the payload alone retrieves on what the chunk used
    to say and displays what it now says — plausible, and invisible."""
    out, orphans = apply_overrides([chunk()], {0: override()})

    assert out[0].text == "el texto corregido"
    assert out[0].embed_text() == "Cap > Sec\n\nel texto corregido"
    assert orphans == []


def test_hiding_a_chunk_changes_no_text_and_therefore_no_vector():
    """Two different verbs. Hiding a table of contents needs no new words and
    breaks no span; rewriting does both."""
    out, orphans = apply_overrides([chunk()], {0: override(text=None, disabled=True)})
    assert out[0].text == "el texto original"
    assert out[0].embed_text() == "Cap > Sec\n\nel texto original"
    assert orphans == []


# --- 2. an override is keyed by content, not only by index -------------------


def test_an_override_written_against_other_text_is_orphaned_not_applied():
    """`chunk_index` is not stable across a re-cut — a corrected profile took
    one document from 600 chunks to 631 — so reapplying by index alone attaches
    a correction to a different passage and reads exactly like a good edit."""
    out, orphans = apply_overrides(
        [chunk(text="un pasaje completamente distinto")], {0: override()}
    )
    assert out[0].text == "un pasaje completamente distinto"
    assert [o.chunk_index for o in orphans] == [0]


def test_an_override_for_an_index_the_document_no_longer_has_is_orphaned():
    """The shape a re-cut that *shrank* produces."""
    out, orphans = apply_overrides([chunk(index=0)], {7: override(7)})
    assert len(out) == 1
    assert [o.chunk_index for o in orphans] == [7]


def test_an_orphan_is_reported_rather_than_dropped():
    """An edit that vanished without a word is worse than one that stopped
    being applied: a screen has to be able to say which."""
    _, orphans = apply_overrides([chunk(text="otro")], {0: override()})
    assert orphans and orphans[0].text == "el texto corregido"


# --- 3. the payload flags, and what must never be written --------------------


def _writer(**kw) -> QdrantWriter:
    return QdrantWriter(
        object(), tenant_id="tnt_1", library_id="lib_1", document_id="doc_1",
        version_id="ver_x", source_title="Un libro",
        model="gemini-embedding-2", dimensions=3072, **kw,
    )


def test_an_untouched_chunk_carries_no_flags_at_all():
    """Absence is the ordinary state, and it has to stay cheap: every one of
    the 8,050 points already written says exactly this."""
    assert _writer()._override(chunk()) == {}
    assert _writer(overrides={0: override()})._override(chunk(index=1)) == {}


def test_a_hidden_chunk_is_marked_disabled_never_enabled_false():
    """Phrased as the exception on purpose. A positive `enabled` flag has to be
    present on every point to mean anything, so adopting one hides every point
    written before it unless a backfill runs and never misses — a corpus that
    vanishes from retrieval while every log line reads as healthy."""
    flags = _writer(overrides={0: override(text=None, disabled=True)})._override(chunk())
    assert flags == {"edited": True, "disabled": True}
    assert "enabled" not in flags


def test_an_edited_chunk_says_so_even_when_it_is_still_visible():
    """`edited` is what tells a reader the `char_span` no longer indexes any
    stream this product holds."""
    assert _writer(overrides={0: override()})._override(chunk()) == {"edited": True}


def test_the_writer_never_carries_the_edited_text_itself():
    """It is substituted upstream. A second copy here would be a second place
    for the vector and the payload to disagree."""
    flags = _writer(overrides={0: override()})._override(chunk())
    assert "text" not in flags


def test_a_stale_override_sets_no_flag_either():
    """The same rule as the text: if it does not fit the chunk, it is not about
    this chunk."""
    w = _writer(overrides={0: override(against="otra cosa")})
    assert w._override(chunk()) == {}


# --- 4. the exclusion is a must_not, and it is scope -------------------------


def test_hidden_chunks_are_excluded_by_absence_not_by_a_positive_flag():
    from docagent.qdrant import Excluded, Qdrant

    built = Qdrant.__new__(Qdrant)._filter(
        {"tenant_id": "tnt_1", "disabled": Excluded(True)}
    )
    assert built["must"] == [{"key": "tenant_id", "match": {"value": "tnt_1"}}]
    assert built["must_not"] == [{"key": "disabled", "match": {"value": True}}]


def test_the_exclusion_is_not_something_a_caller_may_request():
    """Scope, not a narrowing — the same two guards `tenant_id` carries: absent
    from the allowlist, and assigned afterwards regardless."""
    from brainworker.answering.retrieve import ALLOWED_FILTERS

    assert "disabled" not in ALLOWED_FILTERS
    assert "enabled" not in ALLOWED_FILTERS


# --- 5. an edited chunk keeps its locator ------------------------------------


def test_an_edited_chunk_trades_its_byte_range_for_a_marker():
    """`answer._verify` drops a citation whose chunk has no locator, so an
    empty one would make every edited chunk silently uncitable — the opposite
    of what editing is for."""
    from brainworker.graph.projection import ChunkNode, VersionNode, _locator

    version = VersionNode(
        library="lib_1", source_key="libros/x.pdf", content_sha256="s",
        title="Un libro", tenant_id="tnt_1",
    )
    plain = ChunkNode(ordinal=0, kind="cuerpo", text="t", char_start=0, char_end=9)
    edited = ChunkNode(ordinal=0, kind="cuerpo", text="t", char_start=0,
                       char_end=9, edited=True)

    assert _locator(version, plain) == "Un libro · [0:9]"
    assert _locator(version, edited) == "Un libro · editado"
    assert _locator(version, edited), "an edited chunk must never lose its locator"
