"""Bulk data lives on disk; Temporal payloads carry references to it.

Temporal caps a payload at roughly 2 MB and keeps workflow history for the whole
namespace retention period. A 175-page book extracts to ~600 KB of text and
chunks to more than that, so passing content between activities would blow the
limit on the large documents and permanently store a copy of every customer
document on the smaller ones. Neither is acceptable, and the second is worse
because it looks like it works.

So every activity that produces bulk output writes it here and returns an
``ArtifactRef``: a relative path, a content hash and a size. Small enough for a
payload, and the hash makes a stale reference detectable rather than silently
wrong — which matters because Temporal retries activities, and a retry that
rewrites an artifact would otherwise leave an earlier reference pointing at
content that no longer exists at that path.

Paths are relative to the workspace root on purpose. The worker container sees
it as ``/workspace`` and the desktop app sees it as an application-data
directory; an absolute path recorded by one is meaningless to the other.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

#: Every artifact a run may produce, and the filename it takes.
#:
#: An allowlist rather than free-form names: a typo in a kind would otherwise
#: create an orphan file that nothing ever reads and no one notices, and the UI
#: needs a fixed vocabulary to offer the right viewer for each one.
KINDS: dict[str, str] = {
    # Extraction, before any learned rules are applied.
    "raw_text": "raw.txt",
    "evidence": "evidence.json",
    # Rule learning.
    "proposal": "proposal.json",
    "validation": "validation.json",
    # Extraction with the profile's rules applied.
    "extracted_text": "extracted.txt",
    "structured_chunks": "structured-chunks.jsonl",
    # Correction.
    "corrected_text": "corrected.txt",
    "correction_report": "correction-report.json",
    # Chunking. The preview is what the approval gate showed; when correction is
    # enabled the final set differs, and keeping both is what lets the second
    # gate show precisely what changed.
    "preview_chunks": "chunks.preview.jsonl",
    "chunks": "chunks.jsonl",
    # Gate, measurement and accounting.
    "estimate": "estimate.json",
    # What semantic extraction projected, so a rebuild can replay it instead of
    # paying for the same generation calls again. Written by the stage that
    # already holds the data; documents indexed before this kind existed have no
    # file here, and a rebuild of one restores structure and vectors only.
    "semantics": "semantics.json",
    # Which rules produced this index, and where they came from. On the run
    # rather than in the catalog: provenance of one run belongs with that run's
    # artifacts, and it needs no schema migration to live there.
    "profile": "profile.json",
    "evalset": "evalset.json",
    "scores": "scores.json",
    #: A *tuning candidate's* measurement, kept apart from the baseline's.
    #:
    #: Both are produced by the same activity in the same run, and writing both
    #: to `scores` meant the second overwrote the first — so a candidate that was
    #: measured and then reverted left the run describing an index that no longer
    #: existed. Found by running a real tuning round: `scores.json` said 676
    #: chunks and recall@5 0.8375 while the collection and `chunks.jsonl` both
    #: held the reverted 600. Promoted over `scores` only when the candidate is
    #: kept, which is the one case where it describes the index that stands.
    "scores_candidate": "scores.candidate.json",
    #: What a tuning round tried and what it concluded. Written even when the
    #: conclusion is "nothing beat the noise margin", because that *is* the
    #: result: a round that refused every candidate has measured something, and
    #: without the record the next run would spend the same money to learn it
    #: again.
    "tuning": "tuning.json",
    "ledger": "ledger.json",
    "events": "events.jsonl",
}

TEXT_ENCODING = "utf-8"
_READ_BLOCK = 1 << 20


class ArtifactError(RuntimeError):
    """A reference could not be honoured."""


class UnknownKind(ArtifactError):
    pass


class ContentChanged(ArtifactError):
    """The bytes on disk no longer hash to what the reference recorded."""


@dataclass(frozen=True)
class ArtifactRef:
    """What travels in a Temporal payload in place of the content itself."""

    kind: str
    #: Workspace-relative, POSIX separators, e.g. ``runs/ingest-123/chunks.jsonl``.
    path: str
    sha256: str
    bytes: int
    #: Line count for JSONL artifacts, so the UI can page without reading first.
    rows: int | None = None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    """Reads and writes one run's artifacts."""

    def __init__(self, workspace: pathlib.Path, run_id: str) -> None:
        self.workspace = pathlib.Path(workspace).resolve()
        self.run_id = run_id
        self.run_dir = self._safe_run_dir(run_id)

    def _safe_run_dir(self, run_id: str) -> pathlib.Path:
        # run_id reaches here from a workflow id. It becomes a path segment, so
        # a value like "../../etc" would write outside the volume.
        if not run_id or run_id.startswith(".") or "/" in run_id or "\\" in run_id:
            raise ArtifactError(f"unusable run id for a path segment: {run_id!r}")
        candidate = (self.workspace / "runs" / run_id).resolve()
        if not candidate.is_relative_to(self.workspace / "runs"):
            raise ArtifactError(f"run id escapes the workspace: {run_id!r}")
        return candidate

    def _path_for(self, kind: str) -> pathlib.Path:
        filename = KINDS.get(kind)
        if filename is None:
            raise UnknownKind(f"unknown artifact kind {kind!r}; known: {sorted(KINDS)}")
        return self.run_dir / filename

    def _relative(self, path: pathlib.Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    # --- writing -------------------------------------------------------------

    def write_bytes(self, kind: str, data: bytes, *, rows: int | None = None) -> ArtifactRef:
        path = self._path_for(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling and rename: an activity killed mid-write would
        # otherwise leave a truncated file that hashes fine on the next read
        # because the reference was never returned.
        tmp = path.with_name(path.name + ".partial")
        tmp.write_bytes(data)
        tmp.replace(path)
        return ArtifactRef(
            kind=kind,
            path=self._relative(path),
            sha256=_sha256(data),
            bytes=len(data),
            rows=rows,
        )

    def write_text(self, kind: str, text: str) -> ArtifactRef:
        return self.write_bytes(kind, text.encode(TEXT_ENCODING))

    def write_json(self, kind: str, payload: Any) -> ArtifactRef:
        # sort_keys so an unchanged payload produces an unchanged hash; without
        # it a re-run would look like a content change on dict ordering alone.
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        return self.write_text(kind, body)

    def write_jsonl(self, kind: str, rows: Iterable[Any]) -> ArtifactRef:
        lines = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows]
        body = "".join(f"{line}\n" for line in lines)
        return self.write_bytes(kind, body.encode(TEXT_ENCODING), rows=len(lines))

    # --- reading -------------------------------------------------------------

    def resolve(self, ref: ArtifactRef) -> pathlib.Path:
        path = (self.workspace / ref.path).resolve()
        if not path.is_relative_to(self.workspace):
            raise ArtifactError(f"reference escapes the workspace: {ref.path!r}")
        if not path.is_file():
            # The workspace root is named, not just the relative path, because
            # the interesting failure is not "the file was pruned" — it is "the
            # file exists, under the *other* workspace". One catalog can hold
            # rows for runs written under two of them (see the defect list), and
            # a message with only `runs/…/chunks.jsonl` in it sends the reader
            # looking for a deleted file that is sitting right there.
            raise ArtifactError(
                f"artifact is missing: {ref.path} (looked under {self.workspace})"
            )
        return path

    def read_bytes(self, ref: ArtifactRef, *, verify: bool = True) -> bytes:
        data = self.resolve(ref).read_bytes()
        if verify:
            actual = _sha256(data)
            if actual != ref.sha256:
                raise ContentChanged(
                    f"{ref.path} hashes to {actual[:12]}… but the reference "
                    f"recorded {ref.sha256[:12]}… — it was rewritten after the "
                    "reference was taken"
                )
        return data

    def read_text(self, ref: ArtifactRef, *, verify: bool = True) -> str:
        return self.read_bytes(ref, verify=verify).decode(TEXT_ENCODING)

    def read_json(self, ref: ArtifactRef, *, verify: bool = True) -> Any:
        return json.loads(self.read_text(ref, verify=verify))

    def iter_jsonl(self, ref: ArtifactRef, *, offset: int = 0, limit: int | None = None) -> Iterator[Any]:
        """Stream rows without loading the file.

        A book's chunks run to tens of megabytes; the UI shows fifty at a time,
        so materialising the list to slice it would defeat the purpose of
        storing them out of line in the first place.
        """
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("offset and limit must not be negative")
        taken = 0
        with self.resolve(ref).open("r", encoding=TEXT_ENCODING) as fh:
            for index, line in enumerate(fh):
                if index < offset:
                    continue
                if limit is not None and taken >= limit:
                    return
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
                taken += 1

    def read_range(self, ref: ArtifactRef, start: int, end: int) -> bytes:
        """A byte slice, for showing the passage a chunk's ``char_span`` names.

        Byte offsets, not character offsets: invariant #1. Slicing decoded text
        by these numbers gives the wrong region on any non-ASCII document, which
        is exactly the mistake that made an early audit report zero matches.
        """
        if start < 0 or end < start:
            raise ValueError(f"bad range [{start}, {end})")
        with self.resolve(ref).open("rb") as fh:
            fh.seek(start)
            return fh.read(end - start)

    # --- housekeeping --------------------------------------------------------

    def exists(self, kind: str) -> bool:
        return self._path_for(kind).is_file()

    def index(self) -> list[ArtifactRef]:
        """Every artifact this run has actually produced.

        Hashes are recomputed by reading, so this is for a UI listing or a
        catalog reconciliation, not for a hot path.
        """
        refs: list[ArtifactRef] = []
        for kind, filename in KINDS.items():
            path = self.run_dir / filename
            if not path.is_file():
                continue
            digest = hashlib.sha256()
            rows = 0
            with path.open("rb") as fh:
                while block := fh.read(_READ_BLOCK):
                    digest.update(block)
                    rows += block.count(b"\n")
            refs.append(
                ArtifactRef(
                    kind=kind,
                    path=self._relative(path),
                    sha256=digest.hexdigest(),
                    bytes=path.stat().st_size,
                    rows=rows if filename.endswith(".jsonl") else None,
                )
            )
        return refs
