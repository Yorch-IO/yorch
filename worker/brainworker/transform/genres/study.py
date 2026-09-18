"""Study: a focused investigation that states a method and reports findings."""

from __future__ import annotations

from .base import BibliographyStyle, Genre, intents, titles

NAME = "study"
PROMPT_VERSION = "study/1"

SYSTEM = """\
You are writing a STUDY.

A study investigates a defined question about a defined body of material, states
how it went about it, and reports what it found. It is the genre that makes its
own method visible, and that is its whole claim on a reader's trust: somebody
else should be able to see what was examined, how, and what was concluded —
and to disagree with the last part while accepting the first two.

STRUCTURE. A study has a settled shape and should keep it. State the question.
State what material was examined and how it was selected — including what was
left out, and why. Set out the findings, organised by what they are about rather
than by the order they were discovered in. Discuss what the findings do and do
not support. State the limitations plainly, as facts about the study rather than
as apologies. Conclude on the question that was asked, and no wider.

ORGANISATION WITHIN A CHAPTER. One finding or one aspect per chapter. Open with
what this part examines. Give the evidence. Say what it supports. Distinguish,
visibly and every time, between what the material shows and what you infer from
it — the distinction is the genre.

TONE. Precise, unemphatic, and willing to report an inconvenient result. Hedging
where the evidence is thin, and no hedging where it is not: a study that hedges
everything has told the reader nothing about which parts are firm.

LEVEL OF ANALYSIS. Detailed on evidence, disciplined on inference. Quantities
where the material carries quantities. Counter-evidence stated rather than
absorbed. Where two sources disagree, both are reported and the disagreement is
the finding.

CONVENTIONS. Clear section headings naming their content. Findings numbered or
otherwise individually referable. Citations carried visibly and precisely — a
study's reader is expected to go and look.
"""

OUTLINE_HINT = """\
Plan a study as: question, material and method, findings, discussion,
limitations, conclusion.

The findings are where the source material mostly lands, and they are organised
by subject rather than by the source's order. The method chapter says what was
examined — and where library material is consulted, that it was consulted and
how it was selected belongs here too.

Give limitations a chapter of their own, however short. A study that buries its
limitations in a closing paragraph has made them easy to miss, which is the one
thing the genre exists not to do.
"""

CHAPTER_RATIO = 0.9
EXPANSION = 1.1

BIBLIOGRAPHY = BibliographyStyle(
    tone="scholarly", numbered=True, annotated=True, inline_marks=True
)

_METHOD = ("method", "material", "approach", "procedure", "how", "selection", "scope")
_LIMITS = ("limitation", "limits", "caveat", "threat", "weakness", "constraint")


def validate(chapters: list) -> list[str]:
    """A study without a method chapter or a limitations chapter is a report.

    Both are checked, because both are the genre's promise to the reader: one
    says what was examined, the other says what the result does not cover, and a
    study missing either has quietly widened its own claim.
    """
    text = [f"{t} {i}" for t, i in zip(titles(chapters), intents(chapters))]
    out: list[str] = []
    if not any(any(w in line for w in _METHOD) for line in text):
        out.append(
            "A study must state what material it examined and how it was "
            "selected. Add a chapter for the material and the method."
        )
    if not any(any(w in line for w in _LIMITS) for line in text):
        out.append(
            "A study must state its own limitations in a chapter of their own, "
            "not in a closing aside. Add one."
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
