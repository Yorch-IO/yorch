"""Artifact store behaviour.

The properties worth pinning are the ones that make a reference safe to put in a
permanent Temporal history: it stays relative, it cannot escape the workspace,
and it notices when the bytes underneath it change.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from brainworker.artifacts import (
    KINDS,
    ArtifactError,
    ArtifactRef,
    ArtifactStore,
    ContentChanged,
    UnknownKind,
)

RUN = "ingest-1730000000000-abc123"


@pytest.fixture
def store(tmp_path: pathlib.Path) -> ArtifactStore:
    return ArtifactStore(tmp_path, RUN)


def test_a_reference_is_relative_to_the_workspace(store: ArtifactStore):
    """The worker sees /workspace and the app sees an app-data directory. An
    absolute path recorded by one is meaningless to the other."""
    ref = store.write_text("raw_text", "hola")
    assert ref.path == f"runs/{RUN}/raw.txt"
    assert not pathlib.PurePosixPath(ref.path).is_absolute()


def test_round_trips_text_json_and_jsonl(store: ArtifactStore):
    text = store.write_text("raw_text", "café\nsegunda línea")
    assert store.read_text(text) == "café\nsegunda línea"
    # Bytes, not characters: the accented text is longer encoded than decoded.
    assert text.bytes == len("café\nsegunda línea".encode())

    payload = {"pages": 175, "notes": ["a", "b"]}
    js = store.write_json("evidence", payload)
    assert store.read_json(js) == payload

    rows = [{"i": i, "text": f"chunk {i}"} for i in range(5)]
    jl = store.write_jsonl("chunks", rows)
    assert jl.rows == 5
    assert list(store.iter_jsonl(jl)) == rows


def test_json_hashing_is_stable_across_key_order(store: ArtifactStore):
    """A re-run that builds the same dict differently must not look like a
    content change, or every retry would invalidate references that are fine."""
    a = store.write_json("evidence", {"b": 2, "a": 1})
    b = store.write_json("evidence", {"a": 1, "b": 2})
    assert a.sha256 == b.sha256


def test_reading_a_rewritten_artifact_is_an_error(store: ArtifactStore):
    """Temporal retries activities. A retry that rewrites an artifact must not
    leave an earlier reference silently pointing at different content."""
    ref = store.write_text("corrected_text", "the original correction")
    store.resolve(ref).write_text("something else entirely", encoding="utf-8")

    with pytest.raises(ContentChanged):
        store.read_text(ref)
    # The escape hatch exists for deliberate re-reads, and must still work.
    assert store.read_text(ref, verify=False) == "something else entirely"


def test_jsonl_pages(store: ArtifactStore):
    ref = store.write_jsonl("chunks", [{"i": i} for i in range(100)])
    page = list(store.iter_jsonl(ref, offset=40, limit=10))
    assert [r["i"] for r in page] == list(range(40, 50))
    assert list(store.iter_jsonl(ref, offset=99, limit=10)) == [{"i": 99}]
    assert list(store.iter_jsonl(ref, offset=500, limit=10)) == []


def test_read_range_slices_bytes_not_characters(store: ArtifactStore):
    """Invariant #1: char_span holds byte offsets. Slicing decoded text by them
    lands in the wrong place on any non-ASCII document."""
    text = "áéíóú son cinco vocales acentuadas"
    ref = store.write_text("corrected_text", text)
    raw = text.encode("utf-8")

    # Each accented vowel is two bytes, so byte 10 is not character 10.
    assert store.read_range(ref, 0, 10) == raw[:10]
    assert store.read_range(ref, 0, 10).decode() != text[:10]


def test_unknown_kinds_are_refused(store: ArtifactStore):
    """A typo would otherwise create an orphan file nothing ever reads."""
    with pytest.raises(UnknownKind):
        store.write_text("chnuks", "oops")


@pytest.mark.parametrize("bad", ["", ".", "..", "../../etc", "a/b", "a\\b", ".hidden"])
def test_run_ids_cannot_escape_the_workspace(tmp_path: pathlib.Path, bad: str):
    with pytest.raises(ArtifactError):
        ArtifactStore(tmp_path, bad)


def test_a_reference_pointing_outside_the_workspace_is_refused(store: ArtifactStore):
    forged = ArtifactRef(kind="raw_text", path="../../../etc/passwd", sha256="x", bytes=1)
    with pytest.raises(ArtifactError):
        store.resolve(forged)


def test_a_missing_artifact_is_reported_not_returned_empty(store: ArtifactStore):
    ref = ArtifactRef(kind="raw_text", path=f"runs/{RUN}/raw.txt", sha256="x", bytes=0)
    with pytest.raises(ArtifactError, match="missing"):
        store.read_bytes(ref)


def test_index_lists_only_what_was_produced(store: ArtifactStore):
    store.write_text("raw_text", "one")
    store.write_jsonl("chunks", [{"i": 1}, {"i": 2}])

    index = {ref.kind: ref for ref in store.index()}
    assert set(index) == {"raw_text", "chunks"}
    assert index["chunks"].rows == 2
    assert index["raw_text"].rows is None
    # Hashes recomputed from disk must agree with the ones handed out on write.
    assert index["raw_text"].sha256 == store.write_text("raw_text", "one").sha256


def test_a_partial_write_leaves_no_readable_artifact(store: ArtifactStore):
    """Writes go to a sibling and rename, so an activity killed mid-write cannot
    leave a truncated file that later reads as complete."""
    store.write_text("raw_text", "complete")
    leftovers = list(store.run_dir.glob("*.partial"))
    assert leftovers == []


def test_every_kind_maps_to_a_distinct_filename():
    assert len(set(KINDS.values())) == len(KINDS)


def test_writing_is_atomic_from_a_readers_point_of_view(store: ArtifactStore):
    """Overwriting an artifact must never expose a half-written file: the rename
    is what makes the swap atomic."""
    first = store.write_json("scores", {"recall_at_5": 0.8})
    second = store.write_json("scores", {"recall_at_5": 0.92})
    assert first.sha256 != second.sha256
    assert json.loads(store.read_text(second))["recall_at_5"] == 0.92
