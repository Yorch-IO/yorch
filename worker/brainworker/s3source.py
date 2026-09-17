"""A customer's S3 bucket as a source of audio: the seam, and the judgement.

Two halves in one module, split the way `videosource.py` and
`activities/video.py` are split, and for the same reason: what is worth
asserting about a bucket is *arithmetic* — which bytes are an M4A, which
manifest row names which key, which prefix segment is the source — and a test
that needed an AWS account to check it would run rarely enough to be worth
nothing. Everything above the `--- network` rule below is pure and imports no
SDK. Everything below it imports boto3 inside the function, the house pattern,
so this module loads on a machine that has neither.

**Nothing here names a customer.** The first bucket this was built for carries
a manifest whose columns are `archivo`, `fecha_predica` and `fuente`; those
names arrive in `BucketSource.manifest_map` and appear in no source file. The
next customer's are called something else, and the design is what makes that a
configuration rather than a fork.

**The role is the customer's and the trust policy is the gate.** Access is
`sts:AssumeRole` into a role the customer created, presenting their tenant id
as the ExternalId. Nothing secret is stored: a role ARN buys nothing without
being the principal the trust policy names. The session that comes back lasts
an hour and cannot be extended — instance role → customer role is role
chaining, capped at 3600 s — so a long read re-assumes on `ExpiredToken` and
resumes rather than restarting.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from .pipeline import BucketObject, BucketSource

#: `s3://` bucket names: 3–63 characters of lowercase letters, digits, dots
#: and hyphens. Checked before the name is ever joined into a path or an ARN.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
#: An IAM role ARN. The account is twelve digits and the path may be nested.
_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")
#: What `manifest_map` may name. Anything else is refused at the route.
MANIFEST_FIELDS: frozenset[str] = frozenset(
    {"file", "title", "author", "recorded", "published", "source", "url"}
)
#: Bytes read from the head of every object at sync time. Enough for an ID3
#: tag of ordinary size plus the first frames, or a `moov` atom written first.
HEAD_BYTES = 65536
#: And from the tail, only when the head did not settle it — an M4A whose
#: `moov` atom was written after the media.
TAIL_BYTES = 1024 * 1024
#: How long a presigned link lives. Under the hour the assumed session lasts,
#: so the link never outlives the credentials that signed it.
PRESIGN_SECONDS = 3000


class UnusableBucketSource(ValueError):
    """A source that must not become a path segment, an ARN or a request."""


class BucketAccessError(RuntimeError):
    """An AWS refusal, translated to the kind a client's guidance map knows."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# --- identity ---------------------------------------------------------------


def normalise_prefix(prefix: str) -> str:
    """No leading slash, a trailing one when non-empty, never `//`."""
    p = (prefix or "").strip().lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    return re.sub(r"/{2,}", "/", p)


def checked(source: BucketSource) -> BucketSource:
    """The source, or a refusal. Runs before any of it is joined to anything."""
    if not _BUCKET_RE.match(source.bucket or ""):
        raise UnusableBucketSource(f"not an S3 bucket name: {source.bucket!r}")
    if source.role_arn and not _ROLE_ARN_RE.match(source.role_arn):
        raise UnusableBucketSource(f"not an IAM role ARN: {source.role_arn!r}")
    unknown = set(source.manifest_map) - MANIFEST_FIELDS
    if unknown:
        raise UnusableBucketSource(
            f"manifest_map names fields this reader has not heard of: "
            f"{sorted(unknown)}"
        )
    return source


def bucket_id_for(bucket: str, prefix: str) -> str:
    """Twelve hex characters over `bucket/prefix`. Opaque on purpose.

    A bucket name may carry dots and a prefix carries slashes; neither may
    become a path segment or a library id. The picker renders the name, so
    the id has no reader — same reasoning as a channel's.
    """
    basis = f"{bucket}/{normalise_prefix(prefix)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def library_id_for(bucket: str, prefix: str) -> str:
    """The library a bucket's objects are indexed into. One per (bucket, prefix).

    A bucket *is* a library for the reason a channel is: `retrieve.search`
    narrows by equality on `library_id`, so "ask only these recordings" has to
    be a library. Two prefixes of one bucket are two libraries.
    """
    return f"lib_s3_{bucket_id_for(bucket, prefix)}"


def library_name_for(bucket: str, prefix: str) -> str:
    return f"s3://{bucket}/{normalise_prefix(prefix)}"


def source_key_for(bucket: str, key: str) -> str:
    """`document.source_key` for an object: `s3/<bucket>/<key>`.

    The bucket is in it because a library is one (bucket, prefix), and the
    key is verbatim because two keys differing only in case are two objects.
    """
    return f"s3/{bucket}/{key}"


def canonical_url_for(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def identity_basis(bucket: str, key: str, etag: str, size: int) -> str:
    """The string `content_sha256` is derived from. A proxy, like a video's.

    ETag plus size identifies an S3 object *version* without reading it — an
    ETag is the MD5 for a single-part upload and a part-wise digest for a
    multipart one, and either changes when the bytes do. So a re-import of an
    unchanged object short-circuits at `link_duplicate` before a cent is
    spent, and a re-uploaded object is a new version. The real sha256 of the
    bytes is recorded after the fetch, where it is free.
    """
    return "\n".join(["s3", bucket, key, etag.strip('"'), str(size)])


def content_sha256_for(bucket: str, key: str, etag: str, size: int) -> str:
    return hashlib.sha256(
        identity_basis(bucket, key, etag, size).encode("utf-8")
    ).hexdigest()


# --- what a key says ---------------------------------------------------------


def is_candidate(key: str, source: BucketSource) -> bool:
    """Whether a listed key is an object worth cataloguing.

    Not a directory marker, not the manifest, and not under the archive
    prefix — the transcripts this product writes back must never be listed
    as audio to transcribe.
    """
    if not key or key.endswith("/"):
        return False
    if source.manifest_key and key == source.manifest_key:
        return False
    archive = normalise_prefix(source.archive_prefix)
    if archive and key.startswith(archive):
        return False
    prefix = normalise_prefix(source.prefix)
    if archive and prefix and key.startswith(prefix + archive):
        return False
    return True


def relative_key(key: str, prefix: str) -> str:
    p = normalise_prefix(prefix)
    return key[len(p):] if p and key.startswith(p) else key


def basename(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def stem(key: str) -> str:
    name = basename(key)
    return name.rsplit(".", 1)[0] if "." in name else name


def source_of(key: str, prefix: str) -> str:
    """The first path segment under the prefix, or "" for an object at its root.

    The manifest's own source column outranks this; it exists so a bucket
    with no manifest still has *something* the source filter can narrow by,
    and folders are how people already sort recordings.
    """
    rel = relative_key(key, prefix)
    return rel.split("/", 1)[0] if "/" in rel else ""


_KEY_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[_\-T .]|$)")


def date_from_key(key: str) -> str:
    """`YYYY-MM-DD` at the start of the basename, or "". Never a guess beyond that."""
    m = _KEY_DATE_RE.match(basename(key))
    if not m:
        return ""
    return parse_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}") or ""


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y%m%d")


def parse_date(value: str) -> str | None:
    """An ISO date, or `None` for a value this reader will not vouch for.

    Day-first for the slashed forms, because the corpora this serves are
    Spanish. A value that parses under none of them is *not* dropped by the
    caller — it goes into the row's warnings, so a manifest full of
    American-order dates is a visible fact rather than a silent absence.
    """
    v = (value or "").strip()
    if not v:
        return None
    if "T" in v or " " in v:
        v = re.split(r"[T ]", v, maxsplit=1)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# --- the manifest -------------------------------------------------------------


@dataclass
class Manifest:
    """A CSV the customer keeps beside their audio, read through the mapping."""

    #: The row for each *file* value the mapping produced, verbatim.
    rows: dict[str, dict[str, str]] = field(default_factory=dict)
    #: File values that appeared on more than one row. A join against one of
    #: these is refused rather than resolved by guessing.
    duplicates: set[str] = field(default_factory=set)
    columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows) + len(self.duplicates)


def parse_manifest(data: bytes, mapping: dict[str, str]) -> Manifest:
    """Read the CSV. Delimiter sniffed, BOM tolerated, nothing assumed about columns.

    `mapping["file"]` is either a column name or a template like
    `{folder}/{filename}` — the second form is what lets a manifest that
    spreads a key over two columns name the object exactly, instead of leaving
    the join to a basename that two folders might share.
    """
    out = Manifest()
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    out.columns = [c.strip() for c in (reader.fieldnames or [])]
    file_spec = (mapping.get("file") or "").strip()
    if not file_spec:
        out.warnings.append("manifest_map names no `file` column; nothing can be joined")
        return out
    missing = [
        c for c in _columns_named(file_spec, mapping) if c not in out.columns
    ]
    if missing:
        out.warnings.append(
            f"the manifest has no column called {missing}; it has {out.columns}"
        )
    seen: dict[str, int] = {}
    for row in reader:
        clean = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        file_value = _render(file_spec, clean)
        if not file_value:
            continue
        seen[file_value] = seen.get(file_value, 0) + 1
        if seen[file_value] == 1:
            out.rows[file_value] = clean
        else:
            out.rows.pop(file_value, None)
            out.duplicates.add(file_value)
    return out


def _columns_named(spec: str, mapping: dict[str, str]) -> list[str]:
    cols = re.findall(r"\{([^}]+)\}", spec) if "{" in spec else [spec]
    for name, col in mapping.items():
        if name != "file" and col:
            cols.append(col)
    return cols


def _render(spec: str, row: dict[str, str]) -> str:
    if "{" not in spec:
        return row.get(spec, "")
    try:
        return spec.format(**row).strip().strip("/")
    except (KeyError, IndexError):
        return ""


def join_manifest(
    objects: list[BucketObject], manifest: Manifest, source: BucketSource
) -> tuple[list[BucketObject], int, int]:
    """Attach each manifest row to the object it names. Never by guessing.

    A row's file value matches an object by its key relative to the prefix
    when the value holds a `/`, else by basename. Two objects sharing a
    basename with nothing but a basename to tell them apart are **both left
    unjoined and both warned** — the first corpus has two recordings of one
    day with one title, told apart by their key, and a join that picked one
    would index one sermon under the other's date.

    Returns the objects and the two counts the screen reports: rows that
    named no object, objects no row named.
    """
    mapping = source.manifest_map
    by_rel: dict[str, list[int]] = {}
    by_base: dict[str, list[int]] = {}
    for i, obj in enumerate(objects):
        by_rel.setdefault(relative_key(obj.key, source.prefix), []).append(i)
        by_base.setdefault(basename(obj.key), []).append(i)

    matched_rows = 0
    joined: set[int] = set()
    for file_value, row in manifest.rows.items():
        if "/" in file_value:
            hits = by_rel.get(file_value.lstrip("/"), [])
        else:
            hits = by_base.get(file_value, [])
        if not hits:
            continue
        if len(hits) > 1:
            for i in hits:
                objects[i].warnings.append(
                    f"manifest row {file_value!r} could name {len(hits)} objects; "
                    "not joined — give `file` a template that includes the folder"
                )
            continue
        matched_rows += 1
        joined.add(hits[0])
        _apply_row(objects[hits[0]], row, mapping)

    for file_value in manifest.duplicates:
        hits = by_rel.get(file_value.lstrip("/"), []) if "/" in file_value else by_base.get(file_value, [])
        for i in hits:
            objects[i].warnings.append(
                f"manifest has more than one row for {file_value!r}; none applied"
            )

    unmatched_rows = manifest.count - matched_rows
    unmatched_objects = len(objects) - len(joined)
    return objects, unmatched_rows, unmatched_objects


def _apply_row(obj: BucketObject, row: dict[str, str], mapping: dict[str, str]) -> None:
    def col(name: str) -> str:
        c = mapping.get(name, "")
        return row.get(c, "") if c else ""

    if title := col("title"):
        obj.title = title
    if author := col("author"):
        obj.author = author
    if source := col("source"):
        obj.source = source
    if url := col("url"):
        obj.url = url
    for name, attr in (("recorded", "recorded_at"), ("published", "published_at")):
        raw = col(name)
        if not raw:
            continue
        parsed = parse_date(raw)
        if parsed is None:
            obj.warnings.append(f"manifest {name} date {raw!r} did not parse; left empty")
        else:
            setattr(obj, attr, parsed)


def describe(obj: BucketObject, source: BucketSource) -> BucketObject:
    """Fill what the key alone can say, without overwriting what a manifest said."""
    if not obj.title:
        obj.title = stem(obj.key)
    if not obj.recorded_at:
        obj.recorded_at = date_from_key(obj.key)
    if not obj.source:
        obj.source = source_of(obj.key, source.prefix)
    return obj


# --- the bytes ----------------------------------------------------------------


def sniff_container(head: bytes) -> str:
    """What the first bytes say the container is, or "" when they say nothing.

    The key's extension is never consulted. Values are the ones
    `TRANSCRIBE_FORMATS` maps, because the answer's only reader is
    Transcribe's `MediaFormat`.
    """
    if len(head) < 12:
        return ""
    if head[:3] == b"ID3":
        return "mp3"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0 and (head[1] & 0x06) != 0:
        return "mp3"
    return ""


# --- network ------------------------------------------------------------------
#
# Everything below imports boto3 inside the function. `_client` in
# activities/video.py explains why; here it also keeps the pure half above
# importable by the tests that never touch AWS.


def customer_session(source: BucketSource, external_id: str):
    """A boto3 session inside the customer's role, or this deployment's own.

    Empty `role_arn` means "the bucket is readable by whatever credentials
    this process already holds" — the developer's profile on a workstation,
    the instance role on a host whose account owns the bucket. Production
    customers always name a role.
    """
    import boto3

    if not source.role_arn:
        return boto3.Session()
    sts = boto3.client("sts")
    try:
        got = sts.assume_role(
            RoleArn=source.role_arn,
            RoleSessionName=_session_name(external_id),
            ExternalId=external_id,
            DurationSeconds=3600,
        )
    except Exception as e:  # noqa: BLE001 - translated below
        raise _translate(e, "sts") from e
    creds = got["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _session_name(external_id: str) -> str:
    name = re.sub(r"[^\w+=,.@-]", "-", f"brain-{external_id}")
    return name[:64]


def bucket_region(session, bucket: str) -> str:
    """Where the bucket lives. `None` from S3 means the original region."""
    try:
        got = session.client("s3").get_bucket_location(Bucket=bucket)
    except Exception as e:  # noqa: BLE001
        raise _translate(e, "s3") from e
    return got.get("LocationConstraint") or "us-east-1"


def s3_client(session, source: BucketSource):
    region = source.region or bucket_region(session, source.bucket)
    return session.client("s3", region_name=region)


def list_pages(s3, bucket: str, prefix: str) -> Iterator[list[dict[str, Any]]]:
    """One page of `ListObjectsV2` at a time, so a caller can save each."""
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=normalise_prefix(prefix)):
            yield [
                {
                    "key": o["Key"],
                    "etag": (o.get("ETag") or "").strip('"'),
                    "size": int(o.get("Size") or 0),
                    "last_modified": _iso(o.get("LastModified")),
                }
                for o in (page.get("Contents") or [])
            ]
    except Exception as e:  # noqa: BLE001
        raise _translate(e, "s3") from e


def head_bytes(s3, bucket: str, key: str, first: int = HEAD_BYTES) -> bytes:
    return _ranged(s3, bucket, key, f"bytes=0-{first - 1}")


def tail_bytes(s3, bucket: str, key: str, last: int = TAIL_BYTES) -> bytes:
    return _ranged(s3, bucket, key, f"bytes=-{last}")


def body_bytes(s3, bucket: str, key: str, at: int, length: int = HEAD_BYTES) -> bytes:
    """`length` bytes from `at` — the frames after an ID3 tag the head did not reach."""
    return _ranged(s3, bucket, key, f"bytes={at}-{at + length - 1}")


def _ranged(s3, bucket: str, key: str, rng: str) -> bytes:
    try:
        return s3.get_object(Bucket=bucket, Key=key, Range=rng)["Body"].read()
    except Exception as e:  # noqa: BLE001
        raise _translate(e, "s3") from e


def get_bytes(s3, bucket: str, key: str, limit: int = 50 * 1024 * 1024) -> bytes:
    """A whole small object — the manifest, a transcript. Refused past `limit`."""
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"]
        data = body.read(limit + 1)
    except Exception as e:  # noqa: BLE001
        raise _translate(e, "s3") from e
    if len(data) > limit:
        raise BucketAccessError("object_too_large", f"{key!r} exceeds {limit} bytes")
    return data


def head_object(s3, bucket: str, key: str) -> dict[str, Any] | None:
    """The object's metadata, or `None` when there is no such key."""
    try:
        got = s3.head_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001
        err = _translate(e, "s3")
        if err.kind == "object_not_found":
            return None
        raise err from e
    return {
        "etag": (got.get("ETag") or "").strip('"'),
        "size": int(got.get("ContentLength") or 0),
        "metadata": dict(got.get("Metadata") or {}),
    }


def download(
    s3, bucket: str, key: str, path: str,
    on_bytes: Callable[[int], None] | None = None,
) -> tuple[str, int]:
    """Stream the object to disk, hashing as it goes. Nothing is buffered.

    Returns the sha256 of the bytes and their count — the real content hash
    that `identity_basis` could not have without reading, recorded beside the
    proxy so a person can check the archive against it.
    """
    h = hashlib.sha256()
    size = 0
    try:
        with open(path, "wb") as f:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"]
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
                if on_bytes is not None:
                    on_bytes(len(chunk))
    except Exception as e:  # noqa: BLE001
        raise _translate(e, "s3") from e
    return h.hexdigest(), size


def put_bytes(s3, bucket: str, key: str, data: bytes, content_type: str) -> None:
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    except Exception as e:  # noqa: BLE001
        raise _translate(e, "s3") from e


def presigned_get(s3, bucket: str, key: str, expires: int = PRESIGN_SECONDS) -> str:
    try:
        return s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires
        )
    except Exception as e:  # noqa: BLE001
        raise _translate(e, "s3") from e


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value or "")


def _translate(e: Exception, service: str) -> BucketAccessError:
    """One kind per thing a person can act on, never the SDK's own vocabulary.

    The paid plane's `ControlException` kinds and both clients' guidance maps
    are keyed on these; an untranslated `ClientError` would render as a kind
    the map has never heard of, which is worse than no kind.
    """
    if isinstance(e, BucketAccessError):
        return e
    code = ""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        code = str((resp.get("Error") or {}).get("Code") or "")
    message = str(e)
    if service == "sts":
        if code in ("AccessDenied", "AccessDeniedException"):
            return BucketAccessError(
                "bucket_role_refused",
                "AWS refused to assume the customer's role: check the trust "
                "policy names this deployment's host role and the ExternalId "
                "equals the organisation's tenant id",
            )
        if code in ("ExpiredToken", "ExpiredTokenException"):
            return BucketAccessError("bucket_session_expired", message)
        return BucketAccessError("bucket_role_refused", message)
    if code in ("AccessDenied", "AllAccessDisabled", "403"):
        return BucketAccessError(
            "bucket_forbidden",
            "the role can be assumed but S3 refused the request: the role's "
            f"policy lacks a permission for it ({message})",
        )
    if code in ("NoSuchBucket", "404") and "bucket" in message.lower():
        return BucketAccessError("bucket_not_found", message)
    if code in ("NoSuchKey", "404"):
        return BucketAccessError("object_not_found", message)
    if code in ("ExpiredToken", "ExpiredTokenException"):
        return BucketAccessError("bucket_session_expired", message)
    if code in ("PermanentRedirect", "AuthorizationHeaderMalformed"):
        return BucketAccessError(
            "bucket_wrong_region",
            f"the bucket is in another region than the one requested ({message})",
        )
    return BucketAccessError("bucket_unavailable", message)
