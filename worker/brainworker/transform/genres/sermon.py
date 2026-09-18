"""Sermon: an address that urges an audience to act on a conviction.

Not a religious genre here, though it may of course be used for religious
material. A sermon in the general literary sense is an address whose purpose is
not to inform but to move: a commencement speech, a charge to a profession, an
appeal at a meeting. What defines it is that it asks something of the people
listening, and rests that asking on a text or a body of material it expounds.
"""

from __future__ import annotations

from .base import BibliographyStyle, Genre, intents, titles

NAME = "sermon"
PROMPT_VERSION = "sermon/2"

SYSTEM = """\
You are writing a SERMON.

A sermon is an address delivered to people who are present, which expounds
material and then asks something of them. It is distinguished from a lecture by
its end: a lecture wants the audience to understand, a sermon wants them to act
or to be changed. Everything in it serves that end, including the exposition.

STRUCTURE. Open with the concrete — a situation the hearers recognise. Bring the
material to bear on it: state what the source says, plainly, and expound it.
Draw out what follows for the people listening. Close with the specific thing
being asked, said once and said clearly.

ORGANISATION WITHIN A CHAPTER. One address, or one clear movement of one. Take
up one passage or one claim from the material and stay with it. Move from what
it says, to what it means, to what it asks. Illustrations serve the point and
are dropped once made.

TONE. Direct address in the second person. Spoken rhythm — short sentences, some
repetition, some deliberate pauses on a line of its own. Warmth without
flattery. Urgency without alarm. The speaker is under the material too, never
above the audience.

LEVEL OF ANALYSIS. Enough exposition to make the claim land, and no more. A
hearer cannot follow a distinction three levels deep; where the material is
subtle, choose the one thing worth saying and say it well.

WHAT THIS GENRE MUST NOT DO. Assert as fact what the material does not support,
and this genre is where that temptation is strongest, because rhetoric rewards
confidence. A vivid claim is not licensed by being moving. If the material
supports a smaller claim, make the smaller claim — it will hold, and the larger
one would not. Do not invent an anecdote, a statistic, a case or a person to
illustrate; illustrate from what the source gives, or illustrate in the
conditional ("suppose somebody were to…"), which asks nothing of the facts.

CONVENTIONS. The passage or claim being expounded stated plainly near the
opening, so hearers know what is being addressed. Delivered prose, no headings
inside an address, no visible citation marks — but every source named in the
closing chapter, so what was said can be checked afterwards.

A REFERENCE IS WRITTEN, NOT SPELLED OUT. Give a locator in the notation its own
field uses — `Juan 10:10`, `art. 14.2`, `s. 3(1)(b)`, `Fig. 4`, `p. 212` — and never expand it into speech. Write «Juan 10:10», not «abran sus Biblias en el Evangelio según San Juan, en el capítulo diez, versículo diez». You may name the work in words where that is how it is introduced — "en la carta a los Tesalonicenses", "en el informe de la comisión" — but the locator beside it is always in figures.

This holds even though the address is meant to be *said*. A spoken reference that a hearer cannot write down is a reference they cannot check, and the text you are producing is also read: a locator in figures is what somebody searches for, and prose spelling out a chapter and a verse is not findable by anybody.
"""

OUTLINE_HINT = """\
Plan a sermon, or a series of them, as addresses — each with one thing to say
and one thing to ask.

Find in the material the claims that actually bear on how somebody might live or
act, and give each its own address. Order them so the series builds: what is
true, then what it means for us, then what it asks.

Every chapter's intent must name both halves: what the address expounds, and
what it asks of the hearer. An intent naming only the first is a lecture.
"""

CHAPTER_RATIO = 1.0
EXPANSION = 1.15

BIBLIOGRAPHY = BibliographyStyle(
    tone="note", numbered=False, annotated=False, inline_marks=False
)


def validate(chapters: list) -> list[str]:
    """A sermon asks something. One that does not is a lecture."""
    out: list[str] = []
    text = " ".join(titles(chapters) + intents(chapters))
    if not any(
        word in text
        for word in ("ask", "call", "urge", "invite", "appeal", "charge",
                     "act", "do", "change", "respond", "live", "commit",
                     "take up", "turn")
    ):
        out.append(
            "A sermon asks something of the people listening. No chapter's "
            "intent says what is being asked. Give each address both halves: "
            "what it expounds and what it calls for."
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
