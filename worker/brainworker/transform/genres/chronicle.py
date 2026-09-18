"""Chronicle: a record of events in the order they happened, told plainly.

Distinct from history, and the distinction is the genre's whole value: a
chronicle records, a history explains. Where a history argues that one thing
caused another, a chronicle sets both down in order and leaves the causal claim
to its reader.
"""

from __future__ import annotations

from .base import BibliographyStyle, Genre

NAME = "chronicle"
PROMPT_VERSION = "chronicle/1"

SYSTEM = """\
You are writing a CHRONICLE.

A chronicle records what happened, in the order it happened, as plainly as the
record allows. It is the most restrained of these genres. Its value to a reader
is precisely that it has not been shaped: somebody who wants to know what
occurred, without an argument wrapped around it, comes here.

STRUCTURE. Strictly chronological. Entries, not chapters of argument: a period,
a date or an occasion, and what took place. No foreshadowing — an entry does not
know what comes after it. No retrospective grouping: if two things happened in
the same month they are recorded in that month, whether or not they are related.

ORGANISATION WITHIN A CHAPTER. A span of time. Within it, entries in order, each
opening with when and where. An entry states what happened, who was involved,
and what was decided or produced. Where the record gives a figure, a name or a
document, it goes in the entry.

TONE. Plain, even, and short-sentenced. No emphasis, no scene-setting, no
adjectives doing evaluative work. The chronicler's voice is nearly absent, and
where it does appear it appears to say something about the record — "the account
for this month is missing" — never about the events.

LEVEL OF ANALYSIS. Minimal by design. You may note that one event followed
another; you may not say that it followed *from* it. Where the material itself
draws a connection, report that the material draws it and attribute it.

WHAT THIS GENRE MUST NOT DO. Fill a gap. A period the record does not cover is
recorded as uncovered, and that is a fact about the record worth having. Invent
a date, reorder for effect, or merge two occasions into one because they
resemble each other.

CONVENTIONS. Dated or otherwise ordered headings. Short entries. Names and
figures exactly as the record gives them. Sources attached to the entry they
support, because a chronicle's only claim is accuracy and a reader must be able
to test it.
"""

OUTLINE_HINT = """\
Plan a chronicle as spans of time, in order, with no thematic gathering at all.

Read the source for datable or otherwise orderable occasions and let the
chapters be consecutive spans of them — years, phases, sessions, releases,
whatever unit the material itself carries. Every chapter's source material must
come after the previous chapter's.

If the source carries no order at all, say so in the notes: material that cannot
be put in sequence cannot be made into a chronicle, and forcing it produces a
work whose central claim — this is the order things happened in — is invented.
"""

CHAPTER_RATIO = 1.2
EXPANSION = 0.8

BIBLIOGRAPHY = BibliographyStyle(
    tone="note", numbered=False, annotated=True, inline_marks=True
)


def validate(chapters: list) -> list[str]:
    """Strict order. A chronicle that loops has stopped being one."""
    out: list[str] = []
    last = -1
    for chapter in chapters:
        spans = list(getattr(chapter, "sources", ()) or ())
        if not spans:
            out.append(
                f"Chapter {getattr(chapter, 'ordinal', '?')} records nothing. "
                "Every span of a chronicle must name the material it records."
            )
            continue
        first = min(int(getattr(s, "first", 0)) for s in spans)
        if first < last:
            out.append(
                "A chronicle is strictly in order and never returns to material "
                f"already recorded. Chapter {getattr(chapter, 'ordinal', '?')} "
                f"goes back to {first} after {last}. Reorder, or choose history "
                "instead, which may follow a strand across periods."
            )
            break
        last = max(int(getattr(s, "last", 0)) for s in spans)
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
