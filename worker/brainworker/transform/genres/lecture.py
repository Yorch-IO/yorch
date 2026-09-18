"""Lecture: material taught aloud to an audience that cannot re-read it."""

from __future__ import annotations

from .base import BibliographyStyle, Genre, intents, titles

NAME = "lecture"
PROMPT_VERSION = "lecture/1"

SYSTEM = """\
You are writing a LECTURE.

A lecture is material prepared to be delivered aloud to people who are hearing
it once, in order, and cannot turn back a page. Every decision in the genre
follows from that: what a reader could look up, a listener must be told; what a
reader could skim, a listener must be walked through.

STRUCTURE. Say at the start what the session will cover and why it is worth an
hour. Take up the matter in an order that never requires something not yet
explained. Signpost constantly — "three things follow from that; here is the
first" — because a listener has no headings. Recapitulate at each junction, in
one sentence, before moving on. End by naming what was covered and what it
opens onto.

ORGANISATION WITHIN A CHAPTER. One session, or one clear segment of one. Open by
connecting to what came before. Develop one idea at a time, each with an example
before the abstraction where the material allows. Pause at the natural place a
listener would have a question, and answer it.

TONE. Spoken, but prepared. Second person to the audience is natural. Sentences
short enough to be said in one breath. Repetition is a virtue here and not a
fault: the important thing is said more than once, in more than one way.

LEVEL OF ANALYSIS. Thorough on the two or three ideas that carry the session,
and explicitly selective about the rest — "there is a great deal more here and
I am going to pass over most of it" is a legitimate and useful sentence.

CONVENTIONS. Delivered prose, not bullet points. Examples named and worked.
References spoken aloud in the ordinary way — "the report of the inquiry puts it
like this" — rather than set as apparatus, with the full list kept for the
closing bibliography so an attendee can follow it up afterwards.
"""

OUTLINE_HINT = """\
Plan a lecture — or a course of them — as sessions, each deliverable in one
sitting.

Judge each chapter by whether it can be said aloud in the time a session lasts:
roughly 6,000 to 10,000 characters of delivered prose. A chapter far outside
that is not a session. Open the first with an orientation and close the last
with what it opens onto.

Chapter intents should say what the audience will be able to do or understand by
the end of that session, not what the session mentions.
"""

CHAPTER_RATIO = 1.0
EXPANSION = 1.3

BIBLIOGRAPHY = BibliographyStyle(
    tone="plain", numbered=False, annotated=False, inline_marks=False
)


def validate(chapters: list) -> list[str]:
    """A lecture opens by orienting its audience, who cannot skim ahead."""
    out: list[str] = []
    if not chapters:
        return out
    opening = f"{titles(chapters)[0]} {intents(chapters)[0]}"
    if not any(
        word in opening
        for word in ("introduction", "orientation", "overview", "what we",
                     "opening", "why", "plan", "cover", "begin", "outline")
    ):
        out.append(
            "A lecture must open by telling the audience what the session "
            "covers and why — they cannot look ahead. Give the first chapter a "
            "title and intent that orient the listener."
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
