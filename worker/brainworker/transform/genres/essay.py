"""Essay: one line of thought, pursued in the author's own voice."""

from __future__ import annotations

from .base import BibliographyStyle, Genre, excess

NAME = "essay"
PROMPT_VERSION = "essay/1"

SYSTEM = """\
You are writing an ESSAY.

An essay pursues a single line of thought. It is not a survey and it is not a
summary: it selects, and what it leaves out is as much a decision as what it
keeps. Its reader wants to be taken somewhere, and will follow a voice they
trust rather than a structure they can index.

STRUCTURE. One governing thought, announced early — not always as a thesis
sentence, but the reader should know within the first page what question is
being pursued. Each subsequent part advances that thought rather than adding a
new topic beside it. The ending arrives at something: a position, a
reconsideration, a question sharpened by the passage through the material. It
does not summarise what was just said.

ORGANISATION WITHIN A CHAPTER. Movements, not compartments. A part may open on
a particular — an example, an observation, a difficulty — and work outward.
Transitions carry the argument: the last sentence of a part should make the next
part's subject feel necessary rather than merely next.

TONE. A considered, personal voice. The first person is permitted and is often
right. Concrete before abstract. Sentences vary in length; a short one after
three long ones is doing work. No headings that read as a report's.

LEVEL OF ANALYSIS. Selective depth. Go far into the two or three things that
matter to the line of thought and pass lightly over the rest. Handle an
objection where it genuinely threatens the argument, and do not manufacture
objections for symmetry.

CONVENTIONS. Prose, largely unbroken. Chapters titled evocatively rather than
descriptively where the material allows. Sources named in the prose where they
matter to the thought — "as the record of the inquiry has it" — rather than
carried as apparatus, with the full account kept for the closing bibliography.
"""

OUTLINE_HINT = """\
Plan an essay as a path, not as a division.

Find the one question the source material is really about, and let the outline
be the stages of pursuing it. It is normal for an essay to use much less of the
source than a treatise would — but in Faithful mode every part of the source
must still belong to some stage of the path, even if that stage treats it
briefly.

Prefer four to nine parts. A part whose intent reads "discusses X" is not a
stage of an argument; rewrite it as what that stage *does* to the thought.
"""

CHAPTER_RATIO = 0.6
EXPANSION = 0.9

BIBLIOGRAPHY = BibliographyStyle(
    tone="plain", numbered=False, annotated=False, inline_marks=False
)


PREFERRED_PARTS = 12


def validate(chapters: list) -> list[str]:
    """An essay with thirty parts is a treatise wearing a lighter tone.

    Measured against the material, not against a flat number: see
    `base.excess`. A very long source genuinely needs more parts than an essay
    would like, and refusing every outline it could possibly have is not a
    preference, it is a deadlock.
    """
    out: list[str] = []
    over = excess(chapters, PREFERRED_PARTS)
    if over:
        out.append(
            f"{len(chapters)} parts is {over} more than this material needs. An "
            "essay pursues one line of thought; gather the thinnest parts "
            "together into stages of it."
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
