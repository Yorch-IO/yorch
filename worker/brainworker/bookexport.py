"""Turning one run's chunks into a book, and putting it where it can be fetched.

The half of the EPUB feature that touches the workspace. :mod:`brainworker.epub`
renders and knows nothing else; this reads the source run's artifact, calls it,
and writes the result — and it is one module rather than two call sites because
the artifact is written on three paths (an ingest, a video, and the standalone
run that builds one for a version indexed before the stage existed) and three
implementations of "which run holds the chunks" is three chances to disagree.

**The source run is not always the run being written to.** During an ingest the
two are the same; on the standalone path the chunks belong to whichever earlier
run produced them, and the book lands in the new run's own directory. That is
the same distinction :func:`activities.rebuild.load_rebuild_inputs` makes, and
for the same reason: `run_artifact` is keyed on `(run_id, name)`, so a run may
only write its own.
"""

from __future__ import annotations

import logging
import pathlib
import re

from typing import TYPE_CHECKING

from .artifacts import ArtifactRef, ArtifactStore
from . import epub

if TYPE_CHECKING:  # pragma: no cover - the catalog needs psycopg, the rest does not
    from .catalog import Catalog

log = logging.getLogger(__name__)

#: The artifact a book is made of. Named rather than inlined because
#: `activities/rebuild.py` already has a constant for the same string and the
#: two mean the same thing: the only artifact that carries the detected outline.
CHUNKS_ARTIFACT = "chunks"

#: What a downloaded file is called before the browser or the save dialog gets
#: a say. Latin-1 is not enough for a Spanish title and a header cannot carry
#: raw UTF-8, so the routes send an RFC 5987 ``filename*`` as well; this is the
#: plain fallback an old client reads.
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def filename_for(title: str, fallback: str = "libro") -> str:
    """A filename from a title, keeping the accents a reader expects.

    The *display* name. Transliteration is deliberately not attempted — a book
    called «Teología» should save as `Teología.epub` on every filesystem this
    product runs on — and `ascii_filename_for` is the separate, uglier answer
    for the header that cannot carry it.
    """
    name = " ".join(str(title or "").split()).strip(" .")
    name = name.replace("/", "-").replace("\\", "-")
    return f"{name or fallback}.epub"


def ascii_filename_for(title: str, fallback: str = "libro") -> str:
    """The same name with everything a header cannot carry removed."""
    name = _SAFE.sub("-", filename_for(title, fallback)[: -len(".epub")]).strip("-")
    return f"{name or fallback}.epub"


def source_run_for(catalog: "Catalog", version_id: str) -> str | None:
    """Which run's chunks a book for this version is made of.

    `latest_run_with_artifact` and not the activating run, for the reason it
    already records: a later re-index that succeeded produced the chunks the
    standing index was built from, and an older run's `chunks.jsonl` would
    describe a different cutting of the same document.
    """
    return catalog.latest_run_with_artifact(version_id, CHUNKS_ARTIFACT)


def chunks_ref(catalog: "Catalog", run_id: str) -> ArtifactRef | None:
    for row in catalog.artifacts(run_id):
        if row["name"] == CHUNKS_ARTIFACT:
            return ArtifactRef(
                kind=CHUNKS_ARTIFACT,
                path=row["rel_path"],
                sha256=row["sha256"],
                bytes=row["size_bytes"],
            )
    return None


def read_rows(
    workspace: pathlib.Path, source_run_id: str, chunks: ArtifactRef
) -> list[dict]:
    """The source run's chunks, digest checked.

    Verified rather than merely read, and that matters more here than anywhere
    else this artifact is opened: the standalone path runs long after the run
    that produced the file, and a `chunks.jsonl` rewritten since would make the
    book describe a cutting the index no longer holds — silently, because every
    row in it would still be perfectly well-formed.
    """
    return ArtifactStore(workspace, source_run_id).read_jsonl(chunks)


def is_timed(rows: list[dict]) -> bool:
    """Whether these chunks came from something with a clock.

    Asked of the rows rather than of the run's kind, because the rows are what
    gets rendered and `start_s` is written by exactly one writer
    (`indexing.chunk_row`) for exactly one reason. A run kind would be a second
    answer to a question the data already answers.
    """
    return any(r.get("start_s") is not None for r in rows)


def build_from_rows(
    rows: list[dict],
    *,
    version_id: str,
    title: str,
    author: str | None,
    language: str,
) -> bytes:
    """The archive, with no store and no catalog in sight. Pure."""
    book = epub.Book(
        title=title or epub.UNTITLED_CHAPTER,
        author=author or None,
        language=language or "es",
        identifier=epub.identifier_for(version_id),
        chapters=epub.chapters_from_rows(rows, timed=is_timed(rows)),
    )
    return epub.build(book)


def write_book(
    workspace: pathlib.Path,
    run_id: str,
    rows: list[dict],
    *,
    version_id: str,
    title: str,
    author: str | None,
    language: str,
) -> ArtifactRef:
    """Render and write ``book.epub`` into ``run_id``'s own directory.

    Its own, and not the source run's: `run_artifact` is keyed on
    `(run_id, name)`, so a run may only write artifacts under its own id. During
    an ingest the two ids are the same; on the standalone path the chunks belong
    to an earlier run and only the book is new.
    """
    data = build_from_rows(
        rows,
        version_id=version_id,
        title=title,
        author=author,
        language=language,
    )
    ref = ArtifactStore(workspace, run_id).write_bytes("epub", data)
    log.info(
        "built %s for %s from %d chunks (%d bytes)",
        ref.path, version_id, len(rows), ref.bytes,
    )
    return ref


def excerpt(rows: list[dict], limit: int) -> str:
    """The opening of the document, as a model should read it.

    Headings are folded back in, because a chunk row does not contain them — the
    chunker *consumes* a heading paragraph — and the title of a book is very
    often exactly the heading that was consumed. Reading only `text` would hand
    the model the one part of the front matter that had the title removed from
    it.
    """
    out: list[str] = []
    size = 0
    seen_chapter = ""
    seen_section = ""
    for row in rows:
        for key, current in (("chapter", seen_chapter), ("section", seen_section)):
            value = str(row.get(key) or "").strip()
            if value and value != current:
                out.append(value)
                size += len(value) + 1
                if key == "chapter":
                    seen_chapter = value
                else:
                    seen_section = value
        text = str(row.get("text") or "").strip()
        if text:
            out.append(text)
            size += len(text) + 1
        if size >= limit:
            break
    return "\n\n".join(out)[:limit]
