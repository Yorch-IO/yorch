"""Novel: the source's material dramatised, with the invention bounded.

This is the genre with a real tension in it, and the tension is named rather
than smoothed over. A novel is defined by invention; this product is defined by
not inventing. The resolution is that the licence is over **presentation** and
never over **fact**: how a scene is staged, ordered, paced and voiced is the
novelist's; what happened, to whom, when and with what outcome is the source's.

A reader should be able to take any factual assertion in the finished work and
find it in the source document or in a verified citation. Everything else — the
weather, the room, the order of two sentences in a conversation the source
records — is craft. A work that cannot be written under that constraint is a
work this material does not support, and saying so is the correct outcome.
"""

from __future__ import annotations

from .base import BibliographyStyle, Genre

NAME = "novel"
PROMPT_VERSION = "novel/1"

SYSTEM = """\
You are writing a NOVEL.

A novel renders material as lived experience: scene rather than summary, people
rather than positions, time passing rather than topics covered. The reader is to
be carried, and the craft is entirely in the carrying.

STRUCTURE. Chapters as movements of a story, not as divisions of a subject. A
beginning that puts somebody somewhere doing something. Development through
scenes that change the situation. An ending that lands, rather than concludes.

ORGANISATION WITHIN A CHAPTER. Scene. A place, a time, people present, something
at stake. Show through action, speech and consequence rather than through
exposition. Where exposition is unavoidable, place it in somebody's noticing.

TONE. Whatever the material's own register suggests, held consistently. Past
tense, close third person unless the material strongly suggests otherwise.
Concrete sensory detail where the source supplies it. No authorial commentary,
no morals drawn for the reader, no headings that explain.

THE LICENCE, AND ITS LIMIT. This is the part of this genre that matters most.

You may invent: how a scene is staged; the order in which recorded things are
revealed; where a scene breaks; the rhythm of a conversation the source records;
what a character notices, provided the noticing asserts nothing new; connective
narration that moves between recorded moments without adding to them.

You may not invent: an event, a decision, an outcome, a date, a place, a
journey, an illness, a relationship, a document, a quantity, or a person. You
may not put into a character's mouth a claim the source does not attribute to
them. You may not give a character an interior state the material does not
support — a character may be silent, may hesitate, may look away; a character
may not privately believe something the record does not say they believed.

Where the story needs a bridge the material does not supply, narrate across the
gap rather than filling it: "By the time the accounts resume, three months had
passed and the arrangement had changed." That sentence is honest and a novel can
carry it. An invented scene in its place is not.

CONVENTIONS. No headings inside a chapter, no bullet points, no citation marks
in the prose — they would break the thing the genre is for. The sources are
named in full in the closing chapter instead, so the work can be traced back
without a single mark on the page.
"""

OUTLINE_HINT = """\
Plan a novel as a sequence of scenes the source actually supports.

Read the material for moments: a decision taken, a meeting held, a thing
discovered, a disagreement, a departure. Those are chapters. Material that is
purely expository — definitions, classifications, tables — either becomes
something a character does or says, or it is carried in the narration; note in
the chapter intent which.

Chapter intents should name the scene and what changes in it. An intent reading
"explores the theme of X" is an essay's intent, not a novel's.

If the source supports no moments at all — if it is a reference work with no
event in it anywhere — say so in the notes. A novel built on such material can
only be invented, and inventing it is the one thing this work may not do.
"""

CHAPTER_RATIO = 1.3
EXPANSION = 1.5

BIBLIOGRAPHY = BibliographyStyle(
    tone="note", numbered=False, annotated=False, inline_marks=False
)


def validate(chapters: list) -> list[str]:
    """A novel's chapters are scenes, and a scene is not a topic.

    Checked on the vocabulary of exposition because that is the failure this
    genre actually has: an outline that promises chapters "examining" and
    "discussing" is an essay that will be written in a warmer voice, which is
    the cheapest and least honest way to appear to have changed genre.
    """
    out: list[str] = []
    expository = ("examines", "discusses", "explores the", "analysis of",
                  "overview of", "introduction to", "summary of", "covers the")
    offenders = [
        getattr(c, "ordinal", "?")
        for c in chapters
        if any(w in str(getattr(c, "intent", "") or "").lower() for w in expository)
    ]
    if offenders:
        out.append(
            "Chapters "
            + ", ".join(str(o) for o in offenders)
            + " are planned as expository topics, not as scenes. A novel's "
            "chapter has a place, a time, people and something at stake. "
            "Rewrite those intents as what happens in them."
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
