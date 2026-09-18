"""Turning the composed chapters into the one file a person reads.

Pure, and the reader of the run's artifacts is the same one that writes them —
`draft_row` and `chapter_of` are a pair, here rather than beside their callers,
for the reason `indexing.chunk_row` and `StoredChunk.from_row` are a pair: the
file is written by one activity and read by two, and two spellings of one row
shape is one chance to disagree.

**What this file contains is the work and nothing else.** No preamble, no note
on the transformation, no detected source genre, no mode, no counts. All of that
is in the run — in `plan.json`, in `transform-report.json`, in the run trail and
in the ledger — and none of it is in the document, because the brief asks for a
finished work and a reader receiving a finished work should not be reading
somebody's process notes at the top of it.
"""

from __future__ import annotations

from dataclasses import asdict

from . import bibliography
from .genres import Genre
from .types import CitedSource, ComposedChapter


def draft_row(chapter: ComposedChapter) -> dict:
    """One row of `draft.jsonl`. Declared fields only, through `asdict`.

    Through `asdict` deliberately: a field set as a loose attribute is one
    `asdict` never writes, which is how `Answer.style_effort` was missing from
    every response with no error anywhere.
    """
    return asdict(chapter)


def chapter_of(row: dict) -> ComposedChapter:
    return ComposedChapter(
        ordinal=int(row.get("ordinal", 0)),
        title=str(row.get("title") or ""),
        body=str(row.get("body") or ""),
        cited=[
            CitedSource(
                chunk_id=str(c.get("chunk_id") or ""),
                document_id=str(c.get("document_id") or ""),
                version_id=str(c.get("version_id") or ""),
                title=str(c.get("title") or ""),
                locator=str(c.get("locator") or ""),
                claim=str(c.get("claim") or ""),
            )
            for c in (row.get("cited") or [])
            if isinstance(c, dict)
        ],
    )


def assemble(
    rows: list[dict],
    *,
    genre: Genre,
    language: str,
    work_title: str,
    references: list[str],
    source_title: str = "",
    source_author: str = "",
) -> str:
    """The finished work, as Markdown, bibliography included.

    Chapters are emitted in `ordinal` order rather than in file order. The file
    is rewritten whole by each composing activity and its rows therefore arrive
    in the order they were written, which is the same thing today and would stop
    being the same thing the day anything composes out of order — and the
    failure would be a book with its chapters shuffled, which reads as a corrupt
    export rather than as a scheduling change.
    """
    chapters = sorted((chapter_of(row) for row in rows), key=lambda c: c.ordinal)

    out: list[str] = []
    if work_title.strip():
        out.append(f"# {work_title.strip()}")
        out.append("")

    cited: list[CitedSource] = []
    for chapter in chapters:
        title = chapter.title.strip()
        if title:
            out.append(f"## {title}")
            out.append("")
        body = chapter.body.strip()
        if body:
            out.append(body)
            out.append("")
        cited.extend(chapter.cited)

    out.append(
        bibliography.render(
            style=genre.bibliography,
            language=language,
            references=references,
            sources=cited,
            source_title=source_title,
            source_author=source_author,
        )
    )
    return "\n".join(out).strip() + "\n"


def characters(rows: list[dict]) -> int:
    """How long the work is, counting only what reaches the page."""
    return sum(len(str(row.get("body") or "")) for row in rows)


def cited_versions(rows: list[dict]) -> int:
    """How many distinct library works the finished document rests on."""
    seen = {
        str(c.get("version_id") or c.get("document_id") or "")
        for row in rows
        for c in (row.get("cited") or [])
        if isinstance(c, dict)
    }
    seen.discard("")
    return len(seen)
