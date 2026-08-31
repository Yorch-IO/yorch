"""On-disk cache for embedding vectors, keyed by content and vector space.

Lifted out of `vertex.py` so that two callers can share one implementation: the
engine's own `Vertex`, and the Temporal worker's provider adapter, which reaches
Vertex AI through the google-genai SDK instead and had no cache at all.

**The root is a parameter.** Every other stateful directory in this package
resolves against the process CWD, which is fine for a CLI and wrong for a worker
that runs activities concurrently — `os.chdir` is process-global. `Workspace`
carries the roots; this module never looks one up.

**The model and the dimensions are part of the key, not decoration.** The same
text embedded as `RETRIEVAL_QUERY` is a different vector from the same text
embedded as `RETRIEVAL_DOCUMENT`, and two models of equal width produce vectors
whose cosine means nothing to each other. Keying on all four is what stops a
cache surviving a model change and silently mixing two vector spaces — the
failure `doc/RUNBOOK_INDEXACION.md` calls "log sano, colección que acepta,
ranking corrupto".

One file per vector rather than the single JSON the correction cache uses: 3,072
float32 is 12 KB, so a 600-chunk book is ~7 MB and re-serialising one dict per
write would dominate the run.

Why it exists at all: the scarce resource is not the money but the quota.
`online_prediction_requests_per_base_model` is metered `1/min/{project}/{base_model}`,
measured at ~6 embeddings/minute sustained, so a run that dies at 586 of 600 and
then re-spends 586 units of quota to reach the same wall never converges. With
the cache, each attempt asks for strictly less than the one before.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import struct

#: What a cached entry holds: the vector and the token count the API billed for
#: it. A truncated embedding is deliberately never stored — it is a warning
#: about the input, not a result worth reusing.
Entry = tuple[list[float], int]


def key(model: str, dimensions: int, task_type: str, text: str) -> str:
    raw = f"{model}\x00{dimensions}\x00{task_type}\x00{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read(
    root: pathlib.Path, model: str, dimensions: int, task_type: str, text: str
) -> Entry | None:
    """The cached vector, or None for any reason at all.

    A truncated file — an interrupted write from before `os.replace` ran — reads
    as a miss rather than as a short vector, because a short vector would reach
    Qdrant and be accepted.
    """
    path = root / f"{key(model, dimensions, task_type, text)}.f32"
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    if len(blob) != 4 + dimensions * 4:
        return None
    tokens = int.from_bytes(blob[:4], "little")
    return list(struct.unpack(f"<{dimensions}f", blob[4:])), tokens


def write(
    root: pathlib.Path,
    model: str,
    dimensions: int,
    task_type: str,
    text: str,
    values: list[float],
    tokens: int,
) -> None:
    """Store a vector. Failing to cache must never fail the run that paid for it."""
    if len(values) != dimensions:
        return
    try:
        os.makedirs(root, exist_ok=True)
        path = root / f"{key(model, dimensions, task_type, text)}.f32"
        # Write-then-rename with the pid in the temporary name: `embed_many` runs
        # a worker pool and two processes may share a workspace, so a reader must
        # never see half a vector and two writers must not collide on one temp.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(tokens.to_bytes(4, "little") + struct.pack(f"<{dimensions}f", *values))
        os.replace(tmp, path)
    except OSError:
        pass
