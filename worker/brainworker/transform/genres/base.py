"""What a genre module is, and nothing about any particular genre.

Eleven modules sit beside this one and each is meant to be editable on its own:
changing how a Commentary is written must not touch how an Essay is. That is a
property worth asserting rather than intending, so
`tests/transform/test_genres.py` imports each module in isolation and fails if
one names another. Importing *this* module is not naming another genre — it is
naming the shape they all fill.

**No genre carries an invariant.** The rule that a reference may not be invented,
that a fact must rest on the source or on a verified citation, and that the work
is written in the source document's own language all live in
`transform/rules.py` and are composed **above** the genre, with the sentence that
says the rules win. That is `effort.compose_system`'s arrangement and it is here
for the same reason: how a work is written is a matter of form, and that it may
not invent is not — and a genre prompt is exactly the prose that would otherwise
read as permission to embellish.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

#: The bibliography's tone, as a key rather than as prose.
#:
#: A genre module cannot hold a heading for every language a corpus might be in,
#: and the work is written in the source document's own language — so the words
#: live in one table in `transform/bibliography.py`, per tone and per language,
#: and a genre chooses which register it wants. Four tones cover the eleven.
TONES: tuple[str, ...] = ("formal", "scholarly", "plain", "note")


@dataclass(frozen=True)
class BibliographyStyle:
    """How the closing chapter is set, for one genre.

    Rendered by a pure function with no model in the path, which is what makes
    the bibliography unfabricatable rather than merely well-instructed: every
    entry is a catalog row or a verified locator, and this record says only how
    to arrange them.
    """

    tone: str
    #: Numbered entries, as a treatise or a study would set them.
    numbered: bool = False
    #: Print the claim each library source was cited for. Useful where a reader
    #: is expected to check; noise where the work is read straight through.
    annotated: bool = False
    #: Whether the body itself carries visible citation marks. False does **not**
    #: mean the sources go unrecorded — the closing chapter still names every one
    #: of them — it means a novel does not put a footnote mark in its prose.
    inline_marks: bool = True


@dataclass(frozen=True)
class Genre:
    """One target genre: how to write it, how to plan it, and what it costs.

    `chapter_ratio` and `expansion` are **projections, and they are guesses**
    until a real run says otherwise. They are declared here rather than inferred
    so that the first measurement has somewhere to land, which is this
    repository's convention: a constant whose basis is a guess says so, and
    names the run that would replace it.
    """

    name: str
    #: Bumped when `system` or `outline_hint` changes in a way that could move a
    #: reading. Recorded beside the model id in the plan artifact, for the reason
    #: `channel/synthesis.py` records its own: a reading whose instrument is
    #: unrecorded cannot be compared with the next one.
    prompt_version: str
    system: str
    outline_hint: str
    #: Target chapters per source chapter. A commentary tends to divide more
    #: finely than its source; an essay gathers.
    chapter_ratio: float
    #: Output characters per source character. The estimate's dominant term.
    expansion: float
    bibliography: BibliographyStyle
    #: Complaints about a proposed outline, in English, addressed to the model
    #: that proposed it. Empty means the outline satisfies this genre. Structural
    #: checks every genre shares — coverage, uniqueness, the chapter cap — live
    #: in `transform/planning.py`, because a genre that forgot one of them would
    #: be a genre that silently accepted a broken plan.
    validate: Callable[[list], list[str]]


def titles(chapters: list) -> list[str]:
    """The proposed titles, lowercased and stripped. A shared reading helper.

    Here rather than in each module because it is not a rule about any genre; a
    genre that wants to ask "does any chapter open the work" should not have to
    reimplement how to read a title.
    """
    return [str(getattr(c, "title", "") or "").strip().lower() for c in chapters]


def intents(chapters: list) -> list[str]:
    return [str(getattr(c, "intent", "") or "").strip().lower() for c in chapters]


def excess(chapters: list, preferred: int) -> int:
    """How many more chapters than this genre likes, allowing for the material.

    A genre that prefers few chapters — an essay pursues one line of thought and
    twelve stages is already generous — cannot have that preference honoured on
    a 420,000-character book: a twelve-chapter outline would give each chapter
    35,000 characters of source material, and `outline.validate` refuses a
    chapter carrying more than one call can be written from. The two rules would
    then refuse every possible outline, and every run of that genre against a
    long document would silently fall back.

    So the preference is a floor rather than a ceiling: a genre may always ask
    for as few chapters as it likes, and never for fewer than the material
    forces. Computed from the chapters' own measured `chars`, which
    `outline.measure` has already filled by the time a genre validator runs.
    """
    from ..types import MAX_CHAPTER_SOURCE_CHARS

    total = sum(int(getattr(c, "chars", 0) or 0) for c in chapters)
    forced = -(-total // MAX_CHAPTER_SOURCE_CHARS) if total else 1
    return max(0, len(chapters) - max(preferred, forced))
