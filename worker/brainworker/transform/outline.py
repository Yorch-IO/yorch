"""Reading, checking and, when all else fails, building a target outline.

Pure — no graph, no provider, no store. The decisions worth asserting in this
feature are here, which is the same split `radial.ts` and `force.ts` make on the
other side of the product: a rendered test can prove a node exists and nothing
about whether two labels overlap, and a graph test can prove a node ran and
nothing about whether the plan it produced covers the document.

**Coverage is the one mechanical guarantee this feature offers.** "Preserve the
source's ideas as strictly as possible" is a sentence in a prompt; *every source
chunk is assigned to at least one target chapter* is a property, and it is the
only one here a test can hold the model to. In Faithful mode it is required. In
Adaptive it is bounded, because condensing is what that mode is for and
discarding a quarter of a document is not — and the difference has to be a
number or it is not a rule.
"""

from __future__ import annotations

from .genres import Genre
from .reading import Passage, chars_for
from .types import (
    MAX_CHAPTER_SOURCE_CHARS,
    MAX_CHAPTERS,
    MAX_UNCOVERED_FRACTION,
    ChapterPlan,
    SourceChapter,
    SourceSpan,
)


def chapters_from(raw: object, chunk_count: int) -> list[ChapterPlan]:
    """A proposal, read defensively into `ChapterPlan`s.

    Everything is clamped rather than trusted: a span naming chunk 900 of a
    600-chunk document is a model's arithmetic slip, and clamping it produces a
    plan the validator can then judge on its merits. Raising here would turn a
    recoverable proposal into a refine round that costs a call.
    """
    rows = []
    if isinstance(raw, dict):
        rows = raw.get("chapters") or []
    if not isinstance(rows, list):
        return []

    out: list[ChapterPlan] = []
    for ordinal, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        spans: list[SourceSpan] = []
        for span in row.get("sources") or []:
            if not isinstance(span, dict):
                continue
            try:
                first = int(span.get("first", 0))
                last = int(span.get("last", first))
            except (TypeError, ValueError):
                continue
            if last < first:
                first, last = last, first
            first = max(0, min(first, max(0, chunk_count - 1)))
            last = max(0, min(last, max(0, chunk_count - 1)))
            spans.append(SourceSpan(first=first, last=last))
        out.append(
            ChapterPlan(
                ordinal=ordinal,
                title=title,
                intent=str(row.get("intent") or "").strip(),
                sources=spans,
            )
        )
    return out


def measure(chapters: list[ChapterPlan], passages: list[Passage]) -> None:
    """Fill each chapter's `chars` from the source material it claims.

    In place, because the count is derived from the spans and there is exactly
    one right answer: a second, separately computed figure is the shape of two
    records of one fact disagreeing in silence.
    """
    for chapter in chapters:
        chapter.chars = chars_for(passages, chapter.sources)


def coverage(
    chapters: list[ChapterPlan], chunk_count: int
) -> tuple[list[int], float]:
    """Source chunk indices no chapter claims, and what fraction that is."""
    claimed: set[int] = set()
    for chapter in chapters:
        for span in chapter.sources:
            claimed.update(range(span.first, span.last + 1))
    uncovered = [i for i in range(chunk_count) if i not in claimed]
    fraction = (len(uncovered) / chunk_count) if chunk_count else 0.0
    return uncovered, fraction


def validate(
    chapters: list[ChapterPlan],
    *,
    genre: Genre,
    mode: str,
    chunk_count: int,
    max_chapters: int,
    passages: list[Passage],
) -> list[str]:
    """Complaints about a proposed outline, addressed to the model that made it.

    English, because the prompts are, and phrased as instructions rather than as
    diagnostics — this list is fed straight back as the refine round's feedback,
    and "chapter 4 has no sources" is less useful there than "give chapter 4 the
    passages it treats".

    The genre's own checks run **last and always**, even when the structural
    checks already failed, so one refine round carries every complaint rather
    than revealing them one at a time over three rounds that each cost a call.
    """
    out: list[str] = []

    if not chapters:
        out.append("The outline is empty. Propose at least one chapter.")
        return out

    if max_chapters <= 0:
        max_chapters = MAX_CHAPTERS
    if len(chapters) > max_chapters:
        out.append(
            f"{len(chapters)} chapters is more than this work was quoted for. "
            f"Use at most {max_chapters}, gathering the thinnest ones together."
        )

    seen: set[str] = set()
    for chapter in chapters:
        if not chapter.title:
            out.append(f"Chapter {chapter.ordinal} has no title. Give it one.")
            continue
        key = chapter.title.casefold()
        if key in seen:
            out.append(
                f"Two chapters are both called {chapter.title!r}. Titles must be "
                "distinct so a reader can tell them apart in the contents."
            )
        seen.add(key)

    measure(chapters, passages)
    for chapter in chapters:
        if not chapter.sources:
            out.append(
                f"Chapter {chapter.ordinal} ({chapter.title!r}) names no source "
                "passages. Every chapter must say which of the source's material "
                "it is made from, as one or more {first, last} ranges."
            )
        elif chapter.chars > MAX_CHAPTER_SOURCE_CHARS:
            out.append(
                f"Chapter {chapter.ordinal} ({chapter.title!r}) draws on "
                f"{chapter.chars} characters of source material, more than the "
                f"{MAX_CHAPTER_SOURCE_CHARS} a single chapter can be written "
                "from. Split it into two or more chapters."
            )

    uncovered, fraction = coverage(chapters, chunk_count)
    if mode == "faithful" and uncovered:
        out.append(
            f"{len(uncovered)} of the source's {chunk_count} passages are "
            "assigned to no chapter. A faithful transformation must carry all of "
            "the source's material: assign every passage to some chapter, even "
            "where a chapter treats it briefly. The first few unassigned are "
            f"{uncovered[:12]}."
        )
    elif fraction > MAX_UNCOVERED_FRACTION:
        out.append(
            f"{fraction:.0%} of the source's passages are assigned to no "
            f"chapter, more than the {MAX_UNCOVERED_FRACTION:.0%} this mode "
            "allows. Condensing is permitted; leaving out this much is not."
        )

    out.extend(genre.validate(chapters))
    return out


def fallback(
    source: list[SourceChapter],
    passages: list[Passage],
    *,
    max_chapters: int,
) -> list[ChapterPlan]:
    """The source's own chapters, adopted when no proposal validated.

    Not a failure, and the precedent says so in as many words: rule-learning
    falls back to built-in rules after three attempts rather than refusing,
    because *"blocking there would refuse to publish documents whose only fault
    is being ordinary."* A transformation composed against the source's own
    divisions is a real transformation — the genre still governs every sentence
    — and it covers the document by construction, which is the property that
    matters most.

    Two shapes are handled. A chapter with more material than one call can be
    written from is split at passage boundaries; a document with more chapters
    than the cap has consecutive ones merged, which is the direction that keeps
    the order intact.
    """
    if not source:
        if not passages:
            return []
        return [
            ChapterPlan(
                ordinal=1,
                title="",
                intent="",
                sources=[SourceSpan(first=passages[0].index, last=passages[-1].index)],
            )
        ]

    split: list[ChapterPlan] = []
    for chapter in source:
        split.extend(_split(chapter, passages))

    merged = _merge(split, max(1, max_chapters or MAX_CHAPTERS))
    for ordinal, chapter in enumerate(merged, start=1):
        chapter.ordinal = ordinal
    measure(merged, passages)
    return merged


def _split(chapter: SourceChapter, passages: list[Passage]) -> list[ChapterPlan]:
    """One source chapter as one or more target chapters, at passage boundaries."""
    inside = [p for p in passages if chapter.first <= p.index <= chapter.last]
    if not inside:
        return []
    parts: list[list[Passage]] = [[]]
    size = 0
    for passage in inside:
        if parts[-1] and size + len(passage.text) > MAX_CHAPTER_SOURCE_CHARS:
            parts.append([])
            size = 0
        parts[-1].append(passage)
        size += len(passage.text)

    out: list[ChapterPlan] = []
    for i, part in enumerate(parts, start=1):
        title = chapter.title
        if len(parts) > 1:
            # Numbered rather than invented: the source gave one title and this
            # is one of its parts, which is a true thing to say about it.
            title = f"{chapter.title} ({i}/{len(parts)})" if chapter.title else ""
        out.append(
            ChapterPlan(
                ordinal=0,
                title=title,
                intent="",
                sources=[SourceSpan(first=part[0].index, last=part[-1].index)],
            )
        )
    return out


def _merge(chapters: list[ChapterPlan], cap: int) -> list[ChapterPlan]:
    """Fold consecutive chapters together until there are no more than `cap`.

    The smallest adjacent pair first, so merging costs the least structure it
    can, and never across a pair whose combined material a chapter cannot be
    written from — a cap satisfied by producing an unwritable chapter has moved
    the failure rather than fixed it.
    """
    out = list(chapters)
    while len(out) > cap:
        best = -1
        best_size = None
        for i in range(len(out) - 1):
            size = out[i].chars + out[i + 1].chars
            if size > MAX_CHAPTER_SOURCE_CHARS:
                continue
            if best_size is None or size < best_size:
                best, best_size = i, size
        if best < 0:
            # Nothing can be merged without producing an unwritable chapter.
            # Returning the over-long list is the honest outcome: the validator
            # will report it, and a plan that says so beats one that lies.
            break
        left, right = out[best], out[best + 1]
        left.sources = left.sources + right.sources
        left.chars = left.chars + right.chars
        if right.title and right.title not in left.title:
            left.title = f"{left.title}; {right.title}" if left.title else right.title
        del out[best + 1]
    return out
