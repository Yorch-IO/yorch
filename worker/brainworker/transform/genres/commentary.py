"""Commentary: a work that follows another work, explaining as it goes.

Commentary here is the **general** literary form, not the biblical one. A
commentary may follow a statute, a treaty, a piece of software, a philosophical
text, a musical score, a set of accounts or an argument somebody made. Nothing
in this module assumes a domain, and the one thing it does assume — that there
is a subject text to follow — is what distinguishes the form from a study.
"""

from __future__ import annotations

from .base import BibliographyStyle, Genre

NAME = "commentary"
PROMPT_VERSION = "commentary/1"

SYSTEM = """\
You are writing a COMMENTARY.

A commentary follows a subject — a text, a work, an argument, a body of
practice — in that subject's own order, and explains, interprets, analyses or
responds to it passage by passage. Its structure is borrowed: the subject
supplies the sequence, and the commentary supplies the understanding. Its reader
has the subject in front of them and wants help with it.

STRUCTURE. Proceed in the subject's own order and do not reorder it. Take up one
passage at a time. For each: quote or name the passage precisely enough that a
reader can find it, then comment. Where several passages are best treated
together, say so and treat them together — but do not silently skip. A passage
that needs no comment is passed over in a sentence saying so, not in silence.

ORGANISATION WITHIN A CHAPTER. Lemma first, comment second, and the two visibly
distinct. The comment moves outward in a settled order: what the passage says,
then what is difficult in it, then what it means, then what follows from it and
what does not. Where the passage is disputed, set out the readings and say what
each rests on.

TONE. Attentive and subordinate. The commentary exists for the subject, not the
other way round, and it never becomes an essay of its own using the subject as a
pretext. Address the reader as somebody working through the material.

LEVEL OF ANALYSIS. Close. Individual words, figures and clauses are fair game
where they carry weight. Do not comment on what the passage plainly says without
adding something; do not pass over a difficulty because it is hard.

CONVENTIONS. A visible lemma for each unit — the passage's own words or a precise
reference to it — followed by the comment. Cross-references to other passages of
the same subject where they illuminate. Citations carried visibly: a commentary's
reader checks by construction.
"""

OUTLINE_HINT = """\
Plan a commentary as the subject's own divisions, in the subject's own order.

Use the source's structure directly: one chapter per major division, and within
each, units small enough to comment on. Do not gather distant passages into
thematic chapters — that is a study, not a commentary. Do not reorder.

Each chapter's intent should name the span it covers and what is difficult in
it. In Adaptive mode you may merge two very short adjacent divisions, but the
order is never yours to change.
"""

CHAPTER_RATIO = 1.4
EXPANSION = 1.6

BIBLIOGRAPHY = BibliographyStyle(
    tone="scholarly", numbered=True, annotated=True, inline_marks=True
)


def validate(chapters: list) -> list[str]:
    """A commentary's spans must run in the subject's order, and not overlap.

    This is the one genre whose structural rule is mechanically checkable, and
    it is the rule that distinguishes it from a study: if the chapters do not
    follow the source's order, what has been planned is a thematic treatment
    wearing a commentary's name.
    """
    out: list[str] = []
    last = -1
    for chapter in chapters:
        spans = list(getattr(chapter, "sources", ()) or ())
        if not spans:
            out.append(
                f"Chapter {getattr(chapter, 'ordinal', '?')} "
                f"({getattr(chapter, 'title', '')!r}) comments on nothing. Every "
                "chapter of a commentary must name the passages it follows."
            )
            continue
        first = min(int(getattr(s, "first", 0)) for s in spans)
        if first < last:
            out.append(
                "A commentary follows its subject in the subject's own order. "
                f"Chapter {getattr(chapter, 'ordinal', '?')} goes back to source "
                f"passage {first} after passage {last} was already covered. "
                "Reorder the chapters to run forward, or plan a study instead."
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
