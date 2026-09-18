"""Counsel: guidance addressed to somebody with a decision in front of them.

General advice, not pastoral advice. The reader might be choosing a supplier, a
treatment, a migration strategy, a school or a way of handling a colleague.
Nothing here assumes a domain or an authority relationship.
"""

from __future__ import annotations

from .base import BibliographyStyle, Genre, intents, titles

NAME = "counsel"
PROMPT_VERSION = "counsel/1"

SYSTEM = """\
You are writing COUNSEL.

Counsel is guidance written for somebody who has a decision or a difficulty in
front of them and has to act. It is the most reader-directed of these genres:
every part of it is answerable to the question "what does this let them do?"

STRUCTURE. Begin with the situation as the reader experiences it, not with the
background. Set out what actually bears on the decision. Give the guidance
plainly. Where the right course depends on circumstances, name the circumstances
and give the guidance for each rather than hedging into uselessness. Close with
what to do first.

ORGANISATION WITHIN A CHAPTER. One question or one difficulty per chapter. Name
it in the reader's own terms. Say what is true about it. Say what follows for
them. Where there is a common mistake, name it and say why it is tempting —
guidance that only says the right thing leaves the reader unable to recognise
the wrong one.

TONE. Direct, plain, and respectful of the reader's intelligence and of their
right to decide. Second person is natural here. No condescension, no moralising,
no assumed values beyond those the material itself supplies.

LEVEL OF ANALYSIS. As deep as the decision requires and no deeper. Reasoning is
given because a reader who understands why can adapt the advice to a case you
did not anticipate — but the reasoning serves the advice, not the other way
round.

WHAT THIS GENRE MUST NOT DO. Give guidance the material does not support. Where
the source establishes a fact but not a recommendation, say what the fact
implies and label it as your reading. Where the reader's situation may be
outside what the material covers, say so; the most useful thing counsel can do
at its own limit is name the limit.

CONVENTIONS. Short paragraphs. Concrete cases. Steps where the guidance is a
sequence. No visible citation apparatus in the body — a reader deciding
something does not want footnotes — but every source is named in the closing
bibliography, so the guidance can be traced back.
"""

OUTLINE_HINT = """\
Plan counsel around the reader's questions, not around the source's topics.

Ask what somebody would actually come to this material wanting to decide, and
let each chapter answer one of those questions. The order is the order the
questions arise for the reader: what is this, does it apply to me, what do I do,
what if my case is different, what do I do first.

Chapter intents should be written as the reader's question, or as the guidance
given. An intent that reads "covers the background" belongs in a different
genre.
"""

CHAPTER_RATIO = 0.7
EXPANSION = 0.85

BIBLIOGRAPHY = BibliographyStyle(
    tone="plain", numbered=False, annotated=False, inline_marks=False
)


def validate(chapters: list) -> list[str]:
    """Counsel that never tells anybody what to do is an essay."""
    out: list[str] = []
    text = " ".join(titles(chapters) + intents(chapters))
    if not any(
        word in text
        for word in ("what to do", "how to", "decide", "choose", "advice",
                     "guidance", "steps", "start", "first", "practice", "apply",
                     "should", "handle", "avoid")
    ):
        out.append(
            "Counsel must reach something the reader can act on. No chapter's "
            "title or intent says what the reader should do. Rewrite the "
            "outline around the decisions this material can help with."
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
