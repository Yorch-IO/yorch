"""What a customer's bucket holds, kept on the workspace rather than in the catalog.

`channelstore`'s twin, and the same three decisions apply for the same
reasons. **It is not a table** because it is derived data: refetchable for a
listing request per thousand objects, changing whenever the bucket does,
joined against by nothing. **Whether an object is indexed is not in it** —
that is Postgres, through `document.source_key = s3/<bucket>/<key>` inside the
bucket's library, joined on every read, because a second record of one fact is
a second record that can disagree with the first in silence. **A bucket is a
library**, one per (bucket, prefix), because `retrieve.search` narrows by
equality on `library_id` and "ask only these recordings" has to be askable.

Layout, under `Paths.for_tenant(t).buckets`:

```
<bucket_id>/bucket.json     the source, when it was synced, the counts
<bucket_id>/objects.json    every object known about it
```

Saved page by page during a sync, so an interruption at page 40 of a hundred
leaves 40 pages. A later sync reconciles: an etag that changed is a changed
row, a key that vanished is marked `available: false` and never deleted, so a
re-upload lines back up with its history.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone

from .pipeline import BucketObject, BucketSource
from .s3source import bucket_id_for, checked, library_id_for, library_name_for

BUCKET_FILE = "bucket.json"
OBJECTS_FILE = "objects.json"
SCHEMA = 1


@dataclass(frozen=True)
class StoredBucket:
    """A bucket as the store holds it."""

    bucket_id: str
    source: BucketSource
    library_id: str
    library_name: str
    #: ISO 8601, UTC. When the catalogue was last written.
    synced_at: str = ""
    object_count: int = 0
    #: Whether the last sync walked the listing to its end. A sync that died
    #: mid-way leaves this false, and the next one starts over rather than
    #: trusting a catalogue that is "the first N pages".
    complete: bool = False
    #: Objects whose duration was estimated from size rather than read.
    estimated: int = 0
    manifest_rows: int = 0
    unmatched_rows: int = 0
    unmatched_objects: int = 0
    warnings: list[str] = field(default_factory=list)
    schema: int = SCHEMA


@dataclass(frozen=True)
class BucketStore:
    """The catalogues under one organisation's workspace."""

    root: pathlib.Path

    def dir_for(self, bucket_id: str) -> pathlib.Path:
        if not bucket_id or not bucket_id.isalnum() or len(bucket_id) != 12:
            # Twelve hex characters, minted by `bucket_id_for`. Anything else
            # is not an id this store made and must not become a path.
            raise ValueError(f"not a bucket id: {bucket_id!r}")
        return self.root / bucket_id

    # -- reading

    def list(self) -> list[StoredBucket]:
        """Every bucket this organisation has registered, most recent first.

        Reads only `bucket.json`. A directory whose file is missing or
        unreadable is skipped rather than raising, for the reason
        `ChannelStore.list` gives: the remedy is syncing again, which is free.
        """
        if not self.root.is_dir():
            return []
        out: list[StoredBucket] = []
        for entry in sorted(self.root.iterdir()):
            if entry.is_dir():
                stored = _read_bucket(entry / BUCKET_FILE)
                if stored is not None:
                    out.append(stored)
        return sorted(out, key=lambda s: s.synced_at, reverse=True)

    def read(self, bucket_id: str) -> StoredBucket | None:
        return _read_bucket(self.dir_for(bucket_id) / BUCKET_FILE)

    def objects(self, bucket_id: str) -> list[BucketObject]:
        payload = _load(self.dir_for(bucket_id) / OBJECTS_FILE)
        if payload is None:
            return []
        rows = payload.get("objects") or []
        return [_object_of(r) for r in rows if isinstance(r, dict)]

    def object(self, bucket_id: str, key: str) -> BucketObject | None:
        for obj in self.objects(bucket_id):
            if obj.key == key:
                return obj
        return None

    # -- writing

    def register(
        self,
        source: BucketSource,
        *,
        library_name: str = "",
        library_id: str = "",
    ) -> StoredBucket:
        """Create or update the bucket file. Keeps the counts a sync wrote.

        `library_id` overrides the `lib_s3_…` this bucket derives, for a
        customer who wants the recordings on a shelf they already have. Note
        what it does to the *name*: a derived library is named after the bucket,
        and an existing one must keep the name it has — so the default name is
        only applied when the id is derived too. `ensure_library` reads an empty
        name as "leave it alone", which is what stops this renaming somebody's
        library to `s3://bucket/prefix`.
        """
        checked(source)
        bucket_id = bucket_id_for(source.bucket, source.prefix)
        current = self.read(bucket_id)
        derived = library_id_for(source.bucket, source.prefix)
        chosen = library_id.strip() or derived
        stored = StoredBucket(
            bucket_id=bucket_id,
            source=source,
            library_id=chosen,
            library_name=(
                library_name
                or (library_name_for(source.bucket, source.prefix) if chosen == derived else "")
            ),
            synced_at=current.synced_at if current else "",
            object_count=current.object_count if current else 0,
            complete=current.complete if current else False,
            estimated=current.estimated if current else 0,
            manifest_rows=current.manifest_rows if current else 0,
            unmatched_rows=current.unmatched_rows if current else 0,
            unmatched_objects=current.unmatched_objects if current else 0,
            warnings=list(current.warnings) if current else [],
        )
        self._write_bucket(stored)
        return stored

    def write_objects(
        self,
        bucket_id: str,
        objects: list[BucketObject],
        *,
        complete: bool,
        now: datetime | None = None,
        **counts: int | list[str],
    ) -> StoredBucket:
        """Write the catalogue and refresh the bucket file's counts.

        Called once per page by the sync with the list it is accumulating,
        rather than re-reading the file each time — `ChannelStore.write`'s
        reasoning.
        """
        current = self.read(bucket_id)
        if current is None:
            raise ValueError(f"bucket {bucket_id!r} is not registered")
        directory = self.dir_for(bucket_id)
        directory.mkdir(parents=True, exist_ok=True)
        _dump(
            directory / OBJECTS_FILE,
            {"schema": SCHEMA, "bucket_id": bucket_id,
             "objects": [asdict(o) for o in objects]},
        )
        stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        stored = StoredBucket(
            bucket_id=bucket_id,
            source=current.source,
            library_id=current.library_id,
            library_name=current.library_name,
            synced_at=stamp,
            object_count=len(objects),
            complete=complete,
            estimated=int(counts.get("estimated", current.estimated)),
            manifest_rows=int(counts.get("manifest_rows", current.manifest_rows)),
            unmatched_rows=int(counts.get("unmatched_rows", current.unmatched_rows)),
            unmatched_objects=int(counts.get("unmatched_objects", current.unmatched_objects)),
            warnings=list(counts.get("warnings", current.warnings)),  # type: ignore[arg-type]
        )
        self._write_bucket(stored)
        return stored

    def forget(self, bucket_id: str) -> bool:
        """Drop the catalogue. The library's documents are untouched — removal
        of what was indexed is the Library screen's own explicit verb."""
        directory = self.dir_for(bucket_id)
        if not directory.is_dir():
            return False
        for name in (BUCKET_FILE, OBJECTS_FILE):
            path = directory / name
            if path.exists():
                path.unlink()
        try:
            directory.rmdir()
        except OSError:
            pass
        return True

    def _write_bucket(self, stored: StoredBucket) -> None:
        directory = self.dir_for(stored.bucket_id)
        directory.mkdir(parents=True, exist_ok=True)
        _dump(directory / BUCKET_FILE, asdict(stored))


# --- reconciling ------------------------------------------------------------------


def merge_objects(
    known: list[BucketObject], found: list[BucketObject], *, complete: bool
) -> tuple[list[BucketObject], int, int, int]:
    """What the catalogue holds after a sync: the union, keyed on the key.

    A found object replaces a known one when its etag or size moved — the
    bytes changed, so everything read from them is stale — and keeps the
    known row's *manifest* fields otherwise untouched: title, dates and
    source are re-applied by the join that runs after this. A known object
    the sync did not find is marked unavailable **only when the listing was
    complete**; a partial listing is not a statement about what it did not
    reach.

    Returns the merged list and the three counts: added, changed, absent.
    """
    by_key = {o.key: o for o in known}
    added = changed = 0
    seen: set[str] = set()
    for obj in found:
        seen.add(obj.key)
        old = by_key.get(obj.key)
        if old is None:
            added += 1
            by_key[obj.key] = obj
        elif old.etag != obj.etag or old.size != obj.size:
            changed += 1
            by_key[obj.key] = obj
        else:
            if not old.available:
                changed += 1
            by_key[obj.key] = BucketObject(
                **{**asdict(old), "available": True,
                   "last_modified": obj.last_modified or old.last_modified},
            )
    absent = 0
    if complete:
        for key, old in list(by_key.items()):
            if key not in seen and old.available:
                absent += 1
                by_key[key] = BucketObject(**{**asdict(old), "available": False})
    merged = sorted(by_key.values(), key=lambda o: o.key)
    return merged, added, changed, absent


# --- internals ---------------------------------------------------------------------


_OBJECT_FIELDS = {f.name for f in fields(BucketObject)}
_SOURCE_FIELDS = {f.name for f in fields(BucketSource)}


def _object_of(row: dict) -> BucketObject:
    kw = {k: v for k, v in row.items() if k in _OBJECT_FIELDS}
    kw.setdefault("key", "")
    kw.setdefault("etag", "")
    kw.setdefault("size", 0)
    kw.setdefault("last_modified", "")
    kw["warnings"] = list(kw.get("warnings") or [])
    return BucketObject(**kw)


def _source_of(row: dict) -> BucketSource | None:
    if not isinstance(row, dict) or not row.get("bucket"):
        return None
    kw = {k: v for k, v in row.items() if k in _SOURCE_FIELDS}
    kw["manifest_map"] = dict(kw.get("manifest_map") or {})
    try:
        return BucketSource(**kw)
    except TypeError:
        return None


def _read_bucket(path: pathlib.Path) -> StoredBucket | None:
    payload = _load(path)
    if payload is None:
        return None
    source = _source_of(payload.get("source"))
    if source is None:
        return None
    try:
        return StoredBucket(
            bucket_id=str(payload.get("bucket_id") or ""),
            source=source,
            library_id=str(payload.get("library_id") or ""),
            library_name=str(payload.get("library_name") or ""),
            synced_at=str(payload.get("synced_at") or ""),
            object_count=int(payload.get("object_count") or 0),
            complete=bool(payload.get("complete", False)),
            estimated=int(payload.get("estimated") or 0),
            manifest_rows=int(payload.get("manifest_rows") or 0),
            unmatched_rows=int(payload.get("unmatched_rows") or 0),
            unmatched_objects=int(payload.get("unmatched_objects") or 0),
            warnings=list(payload.get("warnings") or []),
            schema=int(payload.get("schema") or SCHEMA),
        )
    except (TypeError, ValueError):
        return None


def _load(path: pathlib.Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _dump(path: pathlib.Path, payload: dict) -> None:
    # Write to a sibling and rename, for the reason `ArtifactStore.write_bytes`
    # gives: a process killed mid-write must not leave a truncated file that
    # parses as far as it goes.
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
