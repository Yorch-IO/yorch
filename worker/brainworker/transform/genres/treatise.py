"""Treatise: a systematic, exhaustive exposition of a subject."""

from __future__ import annotations

from .base import BibliographyStyle, Genre, titles

NAME = "treatise"
PROMPT_VERSION = "treatise/1"

SYSTEM = """\
You are writing a TREATISE.

A treatise sets out a subject systematically and as completely as its material
allows. Its reader wants the whole of a thing in an order they can rely on, and
is willing to read slowly to get it. It is the most architectural of the prose
genres: the order of the parts is itself an argument about how the subject is
built.

STRUCTURE. Proceed from the general to the particular. State the subject, its
scope and its limits before treating any part of it. Divide the subject into
parts that do not overlap and that between them exhaust it; treat each part in
turn; treat no part before the parts it depends on. Where a distinction will
matter later, draw it explicitly when it is first needed and name it, so the
distinction can be referred to rather than re-explained.

ORGANISATION WITHIN A CHAPTER. Open by naming what this part of the subject is
and where it sits in the whole. Define the terms this part introduces. Set out
the substance in an order the reader can follow without holding two things in
mind at once. Close by stating what has been established, in a sentence that the
next chapter can build on.

TONE. Impersonal, measured, unhurried. The first person is used only for the
work's own procedure ("this chapter treats…"), never for opinion. No rhetorical
questions, no appeals to the reader, no exhortation.

LEVEL OF ANALYSIS. High and explicit. Distinctions are drawn and named.
Objections that the material raises are stated in their strongest form and
answered, or stated and left open with the reason they are left open. A claim's
grounds are given with the claim, not implied.

CONVENTIONS. Numbered or clearly titled divisions. Terminology fixed on first
use and never varied afterwards for the sake of variety — a treatise says the
same word for the same thing every time, and the monotony is the point.
Citations carried visibly, because a reader of a treatise is expected to check.
"""

OUTLINE_HINT = """\
Plan a treatise as a division of the subject, not as a tour of the source.

Open with a chapter that states the subject, its scope and its limits. Then give
one chapter to each part of the subject, ordered so that nothing is treated
before what it depends on. Where the source discusses one matter in several
places, gather it into the one chapter that part belongs to. Where a single
source chapter treats two independent parts, divide it.

Prefer fewer, fuller chapters over many thin ones. Every chapter's intent should
name what the chapter *establishes*, not what it *discusses*.
"""

CHAPTER_RATIO = 1.1
EXPANSION = 1.25

BIBLIOGRAPHY = BibliographyStyle(
    tone="scholarly", numbered=True, annotated=True, inline_marks=True
)


def validate(chapters: list) -> list[str]:
    """A treatise that never states its scope is a long essay.

    One check, and it is the one this genre's whole shape rests on: the reader
    of a treatise is promised the boundaries of the subject before its parts.
    """
    out: list[str] = []
    if len(chapters) < 2:
        out.append(
            "A treatise needs at least an opening chapter stating the subject "
            "and its limits, and one chapter per part of the subject."
        )
        return out
    opening = " ".join(titles(chapters)[:1] + [
        str(getattr(chapters[0], "intent", "") or "").lower()
    ])
    if not any(
        word in opening
        for word in ("scope", "subject", "introduction", "preliminar", "limits",
                     "definition", "plan", "outline", "matter")
    ):
        out.append(
            "The first chapter of a treatise must state the subject, its scope "
            "and its limits. Give it a title and an intent that say so."
        )
    return out


GENRE = Genre(
    name=NAME,
    prompt_version=PROMPT_VERSION,
    system=SYSTEM,
    outline_hint=OUTLINE_HINT,
    chapter_ratio=CHAPTER_RATIO,
    expansion=EXPANSION,
    bibliography=BIBLIOGRAPHY,
    validate=validate,
)
