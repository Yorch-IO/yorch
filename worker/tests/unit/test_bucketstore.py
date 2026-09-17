"""The bucket catalogue on disk, and the reconciliation a sync does over it."""

from __future__ import annotations

import json
import pathlib

import pytest

from brainworker.bucketstore import BucketStore, StoredBucket, merge_objects
from brainworker.pipeline import BucketObject, BucketSource


def src(**kw) -> BucketSource:
    base = dict(bucket="tenant-bucket", prefix="audios/",
                role_arn="arn:aws:iam::123456789012:role/reader",
                manifest_key="metadatos/m.csv", manifest_map={"file": "archivo"})
    base.update(kw)
    return BucketSource(**base)


def obj(key: str, **kw) -> BucketObject:
    base = dict(key=key, etag="e1", size=100, last_modified="2026-01-01T00:00:00+00:00")
    base.update(kw)
    return BucketObject(**base)


@pytest.fixture
def store(tmp_path: pathlib.Path) -> BucketStore:
    return BucketStore(tmp_path / "buckets")


def test_register_then_read_round_trips_the_source_and_names_the_library(store):
    stored = store.register(src())
    assert stored.library_id.startswith("lib_s3_")
    assert stored.library_name == "s3://tenant-bucket/audios/"
    again = store.read(stored.bucket_id)
    assert again is not None
    assert again.source == src()
    assert again.synced_at == "" and again.complete is False
    assert [b.bucket_id for b in store.list()] == [stored.bucket_id]


def test_registering_again_keeps_the_counts_a_sync_wrote(store):
    stored = store.register(src())
    store.write_objects(stored.bucket_id, [obj("audios/a.mp3")], complete=True, estimated=1)
    updated = store.register(src(language="es-ES"))
    assert updated.object_count == 1 and updated.estimated == 1 and updated.complete is True
    assert updated.source.language == "es-ES"


def test_objects_round_trip_including_warnings_and_flags(store):
    stored = store.register(src())
    rows = [obj("audios/a.mp3", duration_s=61, duration_estimated=True,
                warnings=["container_unknown"], available=False)]
    store.write_objects(stored.bucket_id, rows, complete=False)
    back = store.objects(stored.bucket_id)
    assert back == rows
    assert store.object(stored.bucket_id, "audios/a.mp3") == rows[0]
    assert store.object(stored.bucket_id, "audios/none.mp3") is None


def test_a_bad_id_never_becomes_a_path(store):
    with pytest.raises(ValueError):
        store.dir_for("../etc")
    with pytest.raises(ValueError):
        store.dir_for("")
    with pytest.raises(ValueError):
        store.read("abc")


def test_a_corrupt_bucket_file_is_skipped_by_the_listing(store):
    stored = store.register(src())
    (store.root / stored.bucket_id / "bucket.json").write_text("{not json", encoding="utf-8")
    assert store.list() == []
    assert store.read(stored.bucket_id) is None


def test_a_file_from_the_future_with_unknown_fields_still_reads(store):
    stored = store.register(src())
    path = store.root / stored.bucket_id / "bucket.json"
    payload = json.loads(path.read_text())
    payload["something_new"] = 1
    payload["source"]["another_field"] = "x"
    path.write_text(json.dumps(payload))
    back = store.read(stored.bucket_id)
    assert back is not None and back.source.bucket == "tenant-bucket"


def test_writes_are_atomic(store):
    stored = store.register(src())
    store.write_objects(stored.bucket_id, [obj("audios/a.mp3")], complete=True)
    assert not list((store.root / stored.bucket_id).glob("*.partial"))


def test_forget_drops_the_catalogue_only(store):
    stored = store.register(src())
    store.write_objects(stored.bucket_id, [obj("audios/a.mp3")], complete=True)
    assert store.forget(stored.bucket_id) is True
    assert store.read(stored.bucket_id) is None
    assert store.forget(stored.bucket_id) is False


# --- merge -------------------------------------------------------------------


def test_merge_adds_replaces_on_changed_bytes_and_keeps_the_rest():
    known = [obj("audios/a.mp3", title="A", duration_s=10),
             obj("audios/b.mp3", etag="old", title="B")]
    found = [obj("audios/a.mp3"), obj("audios/b.mp3", etag="new"), obj("audios/c.mp3")]
    merged, added, changed, absent = merge_objects(known, found, complete=True)
    assert (added, changed, absent) == (1, 1, 0)
    by = {o.key: o for o in merged}
    assert by["audios/a.mp3"].title == "A" and by["audios/a.mp3"].duration_s == 10, \
        "unchanged bytes keep what was read"
    assert by["audios/b.mp3"].title == "" and by["audios/b.mp3"].etag == "new", \
        "changed bytes drop what was read from the old ones"
    assert "audios/c.mp3" in by


def test_a_key_the_complete_listing_did_not_find_is_marked_absent_never_deleted():
    known = [obj("audios/gone.mp3", title="Gone")]
    merged, added, changed, absent = merge_objects(known, [], complete=True)
    assert absent == 1
    assert merged[0].available is False and merged[0].title == "Gone"


def test_a_partial_listing_says_nothing_about_what_it_did_not_reach():
    known = [obj("audios/gone.mp3")]
    merged, _, _, absent = merge_objects(known, [], complete=False)
    assert absent == 0 and merged[0].available is True


def test_a_reupload_of_an_absent_key_lines_back_up():
    known = [obj("audios/back.mp3", title="Back", available=False)]
    merged, added, changed, absent = merge_objects(known, [obj("audios/back.mp3")], complete=True)
    assert (added, changed, absent) == (0, 1, 0)
    assert merged[0].available is True and merged[0].title == "Back"


def test_a_bucket_can_be_registered_into_a_library_that_already_exists(store):
    """The `lib_s3_…` a bucket derives is a default, not a rule.

    "These recordings belong on the shelf I already have" is a real request,
    and the derived id cannot express it. What it buys is one corpus for
    retrieval and one graph; what it costs is that the recordings can no longer
    be asked on their own, because the library is the unit of narrowing.
    """
    derived = store.register(src())
    assert derived.library_id.startswith("lib_s3_")
    assert derived.library_name == "s3://tenant-bucket/audios/"

    onto = store.register(src(), library_id="lib_teologia")
    assert onto.library_id == "lib_teologia"
    # And the *name* is left empty rather than defaulted, because
    # `ensure_library` reads an empty name as "leave it alone" — the one thing
    # that stops this renaming somebody's library to `s3://bucket/prefix`.
    assert onto.library_name == ""
    assert store.read(onto.bucket_id).library_id == "lib_teologia"


def test_an_explicit_name_still_wins_over_both(store):
    stored = store.register(src(), library_name="Archivo de prédicas", library_id="lib_teologia")
    assert (stored.library_id, stored.library_name) == ("lib_teologia", "Archivo de prédicas")
