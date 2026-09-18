"""History: how something came to be as it is, told from the record.

Any historical subject — an institution, a technology, a dispute, a piece of
legislation, a neighbourhood, a codebase. The genre is defined by its handling
of time and evidence, not by its period or its field.
"""

from __future__ import annotations

from .base import BibliographyStyle, Genre

NAME = "history"
PROMPT_VERSION = "history/1"

SYSTEM = """\
You are writing a HISTORY.

A history gives an account of how a subject came to be as it is. It is organised
by time, it distinguishes what happened from what it meant, and it is explicit
about where its account comes from — because the past is reachable only through
a record, and the record is partial.

STRUCTURE. Chronological, in periods the material itself marks out rather than
in round numbers. Each period is established before it is interpreted: what
happened, then why it mattered, then what it changed. Causes are argued, not
asserted, and a cause the record does not support is offered as a possibility
and labelled as one.

ORGANISATION WITHIN A CHAPTER. Open by placing the period — when, where, and
what the situation was at its start. Narrate the developments in order. Draw out
what changed by the end. Where a development runs across periods, carry it
forward explicitly rather than restarting it.

TONE. Narrative but disciplined. The past tense throughout. No anachronism: the
people in the account did not know how it turned out, and a history that writes
as though they did has stopped explaining and started judging.

LEVEL OF ANALYSIS. Causal and contextual. Distinguish the immediate occasion
from the underlying conditions. Where the record is thin, say so in the
narrative rather than filling the gap — "the accounts for these years do not
survive" is a historical statement and a useful one.

WHERE SOURCES DISAGREE. This is the genre's central skill. Set out each account,
say who says it and on what basis, and where the disagreement cannot be settled
from the material, leave it unsettled and say why. Do not average two accounts
into a third that nobody gave.

CONVENTIONS. Dates and places given where the record gives them. Periods named.
Sources carried visibly and attributed by name in the prose where an account is
contested, because "one source says" and "the sources agree" are different
claims and a reader must be able to tell which is being made.
"""

OUTLINE_HINT = """\
Plan a history as periods, in order, with the divisions the material itself
marks.

Look for the turning points the source treats as turning points, and let the
chapters fall between them. Open with a chapter establishing the situation
before the story begins — a history that starts at the first event has left the
reader unable to see what changed. Close at the point the record closes.

Where the source treats several strands, decide whether they are one story told
in periods or several stories told in parallel, and say which in the chapter
intents. Do not silently interleave them.
"""

CHAPTER_RATIO = 0.9
EXPANSION = 1.2

BIBLIOGRAPHY = BibliographyStyle(
    tone="scholarly", numbered=False, annotated=True, inline_marks=True
)


def validate(chapters: list) -> list[str]:
    """A history runs forward, and it establishes before it narrates."""
    out: list[str] = []
    if len(chapters) < 2:
        out.append(
            "A history needs at least a chapter establishing the situation and "
            "one narrating what changed."
        )
        return out
    last = -1
    for chapter in chapters:
        spans = list(getattr(chapter, "sources", ()) or ())
        if not spans:
            continue
        first = min(int(getattr(s, "first", 0)) for s in spans)
        if first < last:
            out.append(
                "A history is told in order. Chapter "
                f"{getattr(chapter, 'ordinal', '?')} returns to source material "
                f"at {first} after {last} was already used. Either reorder the "
                "chapters, or say in the intent that this chapter follows a "
                "strand across periods already narrated."
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
