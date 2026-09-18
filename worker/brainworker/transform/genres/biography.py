"""Biography: the life of a person, told from what the record supports.

Biography of **any** person — a founder, a chemist, a grandmother, a defendant,
a maintainer of a library. Nothing here assumes the subject is famous, historic
or admirable, and the genre's rules are the same either way.
"""

from __future__ import annotations

from .base import BibliographyStyle, Genre

NAME = "biography"
PROMPT_VERSION = "biography/1"

SYSTEM = """\
You are writing a BIOGRAPHY.

A biography gives an account of one person's life, or of a definite part of it,
from what the record supports. It is narrative in shape and evidential in
substance: it reads forward like a story and rests at every point on something
somebody could check.

STRUCTURE. Chronological by default, because a life is lived in order and a
reader follows it most easily that way. Departures from chronology are allowed
where a theme genuinely runs through the life — a lifelong quarrel, a
recurring illness — but the departure is signalled and the thread is returned.
Open where the life's shape begins to be legible, which is not always at birth.
Close where the record closes, not where a moral would.

ORGANISATION WITHIN A CHAPTER. A period, a place, or a single episode. Set the
circumstances, then the person in them, then what changed. Quote the subject's
own words where the material carries them; a life told entirely in the third
person keeps the reader at a distance the genre cannot afford.

TONE. Warm but unsentimental, and never a case for the defence or the
prosecution. Where the record is unflattering, it is reported in the same voice
as the rest.

LEVEL OF ANALYSIS. Interpretive but bounded. You may say what a decision seems
to have cost the subject; you must not say what they felt unless the record says
so. Motive is the biographer's standing temptation and the genre's standing
failure: attribute a motive only where the material attributes one, and where it
does not, describe the act and let it stand.

WHAT THIS GENRE MUST NOT DO. Invent a date, a place, a relative, a conversation,
a journey or an illness. A gap in the record is reported as a gap — "nothing in
the record says how the intervening two years were spent" — and a reader is
better served by that sentence than by a plausible one. This is not a stylistic
preference; it is the difference between a biography and a novel about a real
person.

CONVENTIONS. Dates given where the record gives them and not where it does not.
Names given in full on first appearance. Sources carried, because a biography's
authority is entirely borrowed from them.
"""

OUTLINE_HINT = """\
Plan a biography as the periods of a life, in order.

Read the source for what it establishes about the person: when and where things
happened, who else was involved, what changed and what the person themselves
said. Let the chapters be the periods those facts fall into. Where the source is
not about a person at all, or names no person it gives enough of, say so in the
notes — a biography planned from material that will not support one is the
genre's characteristic failure, and it is better refused at the outline than
discovered at chapter three.

Where the source supports only part of a life, plan only that part and let the
title say so.
"""

CHAPTER_RATIO = 0.8
EXPANSION = 1.15

BIBLIOGRAPHY = BibliographyStyle(
    tone="formal", numbered=False, annotated=True, inline_marks=True
)


def validate(chapters: list) -> list[str]:
    """A biography needs a subject, and one only.

    The weakest useful check, deliberately: whether the source supports a life
    at all is a judgement the planner makes and records in its notes, and a
    keyword rule here would refuse perfectly good outlines about people whose
    titles do not happen to contain a name.
    """
    out: list[str] = []
    if len(chapters) < 2:
        out.append(
            "A biography needs at least two chapters: one life told in a single "
            "undivided block is a sketch, not a biography."
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
