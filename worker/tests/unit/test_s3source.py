"""The pure half of the bucket seam: ids, keys, the manifest and the join.

No boto3 anywhere in this file. What is asserted is the judgement — which
row names which object, which key is a candidate, what a date means — and
the one property the design rests on: that nothing here knows a customer's
column names.
"""

from __future__ import annotations

import pytest

from brainworker.pipeline import BucketObject, BucketSource
from brainworker import s3source
from brainworker.s3source import (
    BucketAccessError,
    Manifest,
    UnusableBucketSource,
    bucket_id_for,
    canonical_url_for,
    checked,
    content_sha256_for,
    date_from_key,
    describe,
    identity_basis,
    is_candidate,
    join_manifest,
    library_id_for,
    library_name_for,
    normalise_prefix,
    parse_date,
    parse_manifest,
    source_key_for,
    source_of,
    _translate,
)


def src(**kw) -> BucketSource:
    base = dict(bucket="tenant-bucket", prefix="audios/", role_arn="arn:aws:iam::123456789012:role/reader")
    base.update(kw)
    return BucketSource(**base)


def obj(key: str, **kw) -> BucketObject:
    base = dict(key=key, etag="abc", size=100, last_modified="2026-01-01T00:00:00+00:00")
    base.update(kw)
    return BucketObject(**base)


# --- identity ---------------------------------------------------------------


def test_the_library_is_one_per_bucket_and_prefix():
    a = library_id_for("tenant-bucket", "audios")
    b = library_id_for("tenant-bucket", "audios/")
    c = library_id_for("tenant-bucket", "otros/")
    assert a == b, "a trailing slash is not a different prefix"
    assert a != c
    assert a.startswith("lib_s3_") and len(a) == len("lib_s3_") + 12
    assert bucket_id_for("tenant-bucket", "audios").isalnum()


def test_the_name_is_readable_and_the_id_is_not():
    assert library_name_for("tenant-bucket", "audios") == "s3://tenant-bucket/audios/"
    assert library_name_for("tenant-bucket", "") == "s3://tenant-bucket/"


def test_identity_is_a_proxy_over_etag_and_size_not_the_bytes():
    """So a re-import of an unchanged object stops at `link_duplicate` before
    a cent is spent, and a re-uploaded one is a new version."""
    basis = identity_basis("b", "audios/x.mp3", '"etag1"', 100)
    assert basis.split("\n") == ["s3", "b", "audios/x.mp3", "etag1", "100"]
    same = content_sha256_for("b", "audios/x.mp3", "etag1", 100)
    assert same == content_sha256_for("b", "audios/x.mp3", '"etag1"', 100)
    assert same != content_sha256_for("b", "audios/x.mp3", "etag2", 100)
    assert same != content_sha256_for("b", "audios/x.mp3", "etag1", 101)


def test_source_key_carries_the_bucket_and_the_key_verbatim():
    assert source_key_for("b", "audios/A.mp3") == "s3/b/audios/A.mp3"
    assert source_key_for("b", "audios/a.mp3") != source_key_for("b", "audios/A.mp3")
    assert canonical_url_for("b", "audios/a.mp3") == "s3://b/audios/a.mp3"


def test_a_bad_bucket_name_or_arn_is_refused_before_anything_is_built():
    with pytest.raises(UnusableBucketSource):
        checked(src(bucket="Not A Bucket"))
    with pytest.raises(UnusableBucketSource):
        checked(src(bucket="../etc"))
    with pytest.raises(UnusableBucketSource):
        checked(src(role_arn="arn:aws:iam::12:role/short-account"))
    with pytest.raises(UnusableBucketSource):
        checked(src(manifest_map={"fecha": "fecha_predica"}))
    assert checked(src()) is not None
    assert checked(src(role_arn="")) is not None, "empty means this process's own credentials"


def test_normalise_prefix():
    assert normalise_prefix("") == ""
    assert normalise_prefix("/audios") == "audios/"
    assert normalise_prefix("audios//sub/") == "audios/sub/"


# --- keys -------------------------------------------------------------------


def test_candidates_exclude_markers_the_manifest_and_the_archive():
    s = src(manifest_key="metadatos/manifiesto.csv", archive_prefix="transcripciones/")
    assert is_candidate("audios/a.mp3", s)
    assert not is_candidate("audios/", s), "a directory marker"
    assert not is_candidate("metadatos/manifiesto.csv", s)
    assert not is_candidate("transcripciones/audios/a.mp3.json", s)
    assert not is_candidate("audios/transcripciones/a.json", s), "the archive under the prefix"
    assert not is_candidate("", s)


def test_source_is_the_first_folder_under_the_prefix():
    assert source_of("audios/ivoox-x/2020-01-01_t.mp3", "audios/") == "ivoox-x"
    assert source_of("audios/2020-01-01_t.mp3", "audios/") == ""
    assert source_of("audios/a/b/c.mp3", "audios") == "a"


def test_a_date_at_the_start_of_the_key_is_read_and_nothing_else_is_guessed():
    assert date_from_key("audios/x/1995-04-02_Si-Se-Humillare.mp3") == "1995-04-02"
    assert date_from_key("audios/x/1995-04-02.mp3") == "1995-04-02"
    assert date_from_key("audios/x/Si-Se-Humillare-1995.mp3") == ""
    assert date_from_key("audios/x/1995-13-40_bad.mp3") == "", "not a date"


def test_parse_date_is_day_first_for_slashes_and_refuses_what_it_cannot_vouch_for():
    assert parse_date("1995-04-02") == "1995-04-02"
    assert parse_date("02/04/1995") == "1995-04-02"
    assert parse_date("1995/04/02") == "1995-04-02"
    assert parse_date("2019-06-01T10:00:00Z") == "2019-06-01"
    assert parse_date("") is None
    assert parse_date("abril de 1995") is None


def test_describe_fills_from_the_key_without_overwriting_the_manifest():
    s = src()
    o = describe(obj("audios/ivoox-x/1995-04-02_Titulo.mp3"), s)
    assert (o.title, o.recorded_at, o.source) == ("1995-04-02_Titulo", "1995-04-02", "ivoox-x")
    o2 = describe(obj("audios/ivoox-x/1995-04-02_Titulo.mp3", title="Mi título",
                      recorded_at="1994-01-01", source="podcast"), s)
    assert (o2.title, o2.recorded_at, o2.source) == ("Mi título", "1994-01-01", "podcast")


# --- the manifest -------------------------------------------------------------


CSV = (
    "origen_dir,fecha,titulo,archivo,bytes,url_mp3,fuente\n"
    "ivoox-x,1995-04-02,Si Se Humillare,1995-04-02_Si-Se-Humillare-a1.mp3,100,https://f/1,ivoox\n"
    "ivoox-x,1995-04-02,Si Se Humillare,1995-04-02_Si-Se-Humillare-b2.mp3,100,https://f/2,ivoox\n"
    "anchor-y,2021-05-06,Domingo,2021-05-06_Domingo.mp3,200,https://f/3,anchor\n"
)


def test_the_manifest_reads_through_the_mapping_and_knows_no_column_itself():
    """The column names live in the mapping the request carried. Nothing in
    `s3source` spells `archivo` or `fecha_predica`."""
    import inspect

    src_text = inspect.getsource(s3source)
    body = src_text.split('"""', 2)[2]  # everything after the module docstring
    for word in ("archivo", "fecha_predica", "fuente", "url_mp3"):
        assert word not in body, f"{word!r} is hard-coded in s3source"

    m = parse_manifest(CSV.encode(), {"file": "archivo", "title": "titulo",
                                      "recorded": "fecha", "source": "fuente", "url": "url_mp3"})
    assert m.count == 3 and not m.warnings
    assert m.rows["2021-05-06_Domingo.mp3"]["titulo"] == "Domingo"


def test_a_bom_and_a_semicolon_are_tolerated():
    data = "﻿archivo;titulo\na.mp3;Uno\nb.mp3;Dos\n".encode("utf-8")
    m = parse_manifest(data, {"file": "archivo", "title": "titulo"})
    assert set(m.rows) == {"a.mp3", "b.mp3"}
    assert m.columns == ["archivo", "titulo"]


def test_a_missing_column_is_a_warning_not_a_crash():
    m = parse_manifest(CSV.encode(), {"file": "fichero", "title": "titulo"})
    assert m.rows == {}
    assert any("fichero" in w for w in m.warnings)


def test_a_file_template_can_spread_the_key_over_two_columns():
    m = parse_manifest(CSV.encode(), {"file": "{origen_dir}/{archivo}"})
    assert "ivoox-x/1995-04-02_Si-Se-Humillare-a1.mp3" in m.rows
    assert "anchor-y/2021-05-06_Domingo.mp3" in m.rows


def test_two_rows_naming_one_file_are_set_aside_and_joined_to_nothing():
    dup = CSV + "ivoox-x,1996-01-01,Otra,2021-05-06_Domingo.mp3,1,https://f/9,ivoox\n"
    m = parse_manifest(dup.encode(), {"file": "archivo", "title": "titulo"})
    assert "2021-05-06_Domingo.mp3" in m.duplicates
    assert "2021-05-06_Domingo.mp3" not in m.rows
    assert m.count == 3


# --- the join -------------------------------------------------------------------


def test_the_join_is_by_basename_and_applies_the_mapped_fields():
    s = src(manifest_map={"file": "archivo", "title": "titulo", "recorded": "fecha",
                          "source": "fuente", "url": "url_mp3"})
    m = parse_manifest(CSV.encode(), s.manifest_map)
    objects = [obj("audios/ivoox-x/1995-04-02_Si-Se-Humillare-a1.mp3"),
               obj("audios/anchor-y/2021-05-06_Domingo.mp3"),
               obj("audios/anchor-y/no-row.mp3")]
    joined, unmatched_rows, unmatched_objects = join_manifest(objects, m, s)
    a, d, n = joined
    assert (a.title, a.recorded_at, a.source, a.url) == ("Si Se Humillare", "1995-04-02", "ivoox", "https://f/1")
    assert d.title == "Domingo"
    assert n.title == "" and n.warnings == []
    assert unmatched_rows == 1, "the b2 row named no object"
    assert unmatched_objects == 1


def test_two_objects_sharing_a_basename_are_both_refused_never_guessed():
    """The first corpus has two recordings of one day with one title. A join
    that picked one would index one sermon under the other's date."""
    csv_ = "archivo,titulo\nsame.mp3,Uno\n"
    s = src(manifest_map={"file": "archivo", "title": "titulo"})
    m = parse_manifest(csv_.encode(), s.manifest_map)
    objects = [obj("audios/x/same.mp3"), obj("audios/y/same.mp3")]
    joined, _, unmatched = join_manifest(objects, m, s)
    assert all(o.title == "" for o in joined)
    assert all(any("could name 2 objects" in w for w in o.warnings) for o in joined)
    assert unmatched == 2


def test_a_file_value_with_a_slash_joins_on_the_key_relative_to_the_prefix():
    csv_ = "ruta,titulo\nx/same.mp3,Uno\ny/same.mp3,Dos\n"
    s = src(manifest_map={"file": "ruta", "title": "titulo"})
    m = parse_manifest(csv_.encode(), s.manifest_map)
    joined, _, unmatched = join_manifest([obj("audios/x/same.mp3"), obj("audios/y/same.mp3")], m, s)
    assert [o.title for o in joined] == ["Uno", "Dos"]
    assert unmatched == 0


def test_a_manifest_date_that_does_not_parse_is_a_warning_on_the_row():
    csv_ = "archivo,fecha\na.mp3,abril de 1995\n"
    s = src(manifest_map={"file": "archivo", "recorded": "fecha"})
    m = parse_manifest(csv_.encode(), s.manifest_map)
    joined, _, _ = join_manifest([obj("audios/a.mp3")], m, s)
    assert joined[0].recorded_at == ""
    assert any("did not parse" in w for w in joined[0].warnings)


def test_a_duplicated_row_warns_the_object_it_would_have_named():
    dup = "archivo,titulo\na.mp3,Uno\na.mp3,Dos\n"
    s = src(manifest_map={"file": "archivo", "title": "titulo"})
    m = parse_manifest(dup.encode(), s.manifest_map)
    joined, _, _ = join_manifest([obj("audios/a.mp3")], m, s)
    assert joined[0].title == ""
    assert any("more than one row" in w for w in joined[0].warnings)


# --- refusals -------------------------------------------------------------------


class _ClientError(Exception):
    def __init__(self, code: str, msg: str = "refused") -> None:
        super().__init__(msg)
        self.response = {"Error": {"Code": code, "Message": msg}}


def test_aws_refusals_translate_to_kinds_a_guidance_map_knows():
    assert _translate(_ClientError("AccessDenied"), "sts").kind == "bucket_role_refused"
    assert _translate(_ClientError("AccessDenied"), "s3").kind == "bucket_forbidden"
    assert _translate(_ClientError("NoSuchBucket", "the bucket does not exist"), "s3").kind == "bucket_not_found"
    assert _translate(_ClientError("NoSuchKey"), "s3").kind == "object_not_found"
    assert _translate(_ClientError("ExpiredToken"), "s3").kind == "bucket_session_expired"
    assert _translate(_ClientError("PermanentRedirect"), "s3").kind == "bucket_wrong_region"
    assert _translate(RuntimeError("network down"), "s3").kind == "bucket_unavailable"
    already = BucketAccessError("object_too_large", "x")
    assert _translate(already, "s3") is already
