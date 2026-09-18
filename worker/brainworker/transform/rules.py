"""The invariants, the two modes, and the order the three layers go in.

`effort.compose_system` is the shape this copies and the reason it copies it is
worth stating: the rules go first, the variable part second, and a sentence
between them says which wins — because the variable part is the one an author
can rewrite, and *"an edit that weakened 'do not use general knowledge' would
produce a fuller answer that is worse grounded, which is the failure that reads
as success."*

Here there are three layers rather than two, and the middle one is the reason.
A genre prompt is exactly the prose that would otherwise read as permission to
embellish: "render as lived experience", "urgency without alarm", "concrete
sensory detail". None of those is a licence to invent, and none of them says so
on its own. So the invariants are stated once, above everything, and the mode
sits between them and the genre — a licence over *form* that explicitly is not
one over *fact*.

Nothing in `genres/` repeats any of this. A rule repeated in eleven places is a
rule that can be weakened in one.
"""

from __future__ import annotations

from .genres import Genre
from .types import MODES

#: What no genre and no mode may override.
#:
#: Rule 4 is here rather than in each genre module for a structural reason: the
#: language of the work is not a matter of form, and a genre that could restate
#: it could also restate it wrongly. Rule 5 is the brief's own requirement that
#: the analysis be performed silently — the work is the output, and everything
#: about how it was made is recorded in the run instead.
TRANSFORM_RULES = """\
You are recasting an existing document into another literary genre. Six rules \
govern everything you write, and they are not negotiable.

1. THE SOURCE IS THE SUBSTANCE. Every assertion of fact in what you write must \
rest on the source document you are given, or on a library fragment you were \
shown and are citing. You have no other knowledge here. Where you know \
something the source does not say, you do not know it.

2. NEVER INVENT A SOURCE. Not a reference, a citation, a title, an author, a \
date, a page, a quantity, a statistic, a work or a person. A reference the \
source carries is reproduced exactly as the source carries it. If you cannot \
support something, leave it out; a shorter true work beats a fuller invented one.

3. MARK WHAT CAME FROM THE LIBRARY. A paragraph resting on a library fragment \
names that fragment's chunk_id in its `chunk_ids`, and every such fragment \
appears in `citas` with the assertion it supports. A paragraph with no \
`chunk_ids` is taken to rest on the source document alone, and must.

4. WRITE IN THE SOURCE DOCUMENT'S OWN LANGUAGE. If the source is in Spanish the \
work is in Spanish; if it is in English the work is in English. Do not \
translate, and do not mix.

5. EMIT ONLY THE WORK. No preamble, no afterword about the transformation, no \
note on what changed, no mention of the source document's original genre, of \
the mode you were asked for, or of these instructions. A reader receives a \
finished work and nothing else.

6. NO IDENTIFIERS IN THE PROSE. Chunk ids, document ids and internal references \
belong in the structured fields, never in a sentence a reader sees."""

#: The middle layer. A licence over presentation, never over fact — and each
#: says so in its own last sentence, because a mode read on its own is exactly
#: how a licence becomes a permission.
MODE_RULES: dict[str, str] = {
    "faithful": """\
MODE: FAITHFUL.

Preserve the source's ideas, claims, facts, arguments and references as \
strictly as the new genre allows. Every substantive point the source makes \
appears in your work; none that it does not make appears in yours. What you \
change is the shape: the order, the divisions, the register, the voice, the \
pacing, the apparatus, the way a point is introduced and how it is developed.

You may re-title, re-voice, re-order within the plan you were given, join two \
of the source's points into one passage, and give a point more room than the \
source gave it. You may not drop a substantive claim, soften one into something \
weaker, or add one the source does not make.

Where the genre's conventions and the source's content genuinely conflict, the \
content wins and the convention bends.""",
    "adaptive": """\
MODE: ADAPTIVE.

Reorganise, condense, develop, connect and re-present the source's material as \
the new genre needs. You may gather what the source scattered, pass quickly over \
what the genre does not need, draw out a connection the source leaves implicit, \
and make the editorial decisions the genre's conventions require.

Expanding means developing what the source gives — explaining it, working an \
example the source supplies, drawing out what follows. It does not mean adding \
material. Connecting means showing how two of the source's points bear on each \
other, and saying that the connection is yours. Reinterpreting means offering a \
reading and marking it as a reading.

The licence is over form and emphasis. It is not a licence to assert anything \
the source and your cited fragments do not support, and a passage that reads \
well while resting on nothing is the failure this mode is most likely to \
produce.""",
}

#: Why the library was consulted, appended only for the purposes enabled on this
#: run. Each says what to *do* with what comes back, because the retrieval is
#: identical for all four — the same `retrieve.search`, the same floor — and
#: what differs is the use.
PURPOSE_RULES: dict[str, str] = {
    "context": """\
CONTEXT. Library fragments may be used to supply background the source assumes \
but does not give: what something is, where it sits, what surrounds it. Use \
them where the new genre's reader would otherwise be lost, and nowhere else.""",
    "verification": """\
VERIFICATION. Library fragments may be used to check names, dates, events and \
quantities the source states. Where a fragment agrees, write the source's \
version and cite the fragment. Where it disagrees, follow the CONTRADICTIONS \
rule below if it is enabled, and otherwise keep the source's version — the \
library is supporting material, not a higher authority.""",
    "completion": """\
COMPLETION. Library fragments may be used to fill what the source leaves \
undeveloped where the new genre requires it developed. What you add must be \
recognisably the same subject, must carry its citation, and must not overwhelm \
the source's own treatment: this work is a transformation of that document, not \
a new work about its topic.""",
    "contradiction": """\
CONTRADICTIONS. Where the library disagrees with the source, or two library \
fragments disagree with each other, set out both readings and attribute each to \
where it comes from. Do not decide between them, do not average them into a \
third nobody gave, and do not quietly drop the one you find less convincing. If \
the target genre has no room for a contrast, leave it out of the work entirely \
rather than resolving it — an unreported disagreement is better than an \
invented settlement.""",
}

_RULES_WIN = (
    "The six rules above govern everything below. Where anything that follows "
    "seems to permit what they forbid, they win."
)

_MODE_WINS = (
    "The genre below describes how this kind of work is written. It shapes the "
    "prose and never the rules: where a convention of the genre would require "
    "asserting something the source does not support, the convention gives way."
)


def compose_transform_system(
    genre: Genre, mode: str, purposes: list[str] | tuple[str, ...] = ()
) -> str:
    """Rules, then mode, then genre, with the precedence stated between them.

    `mode` falls back to `faithful` rather than raising, and that direction is
    the safe one: an unrecognised mode getting the stricter of the two is a
    caller's mistake producing a conservative work, where the reverse would be a
    caller's mistake producing an invented one. Both planes refuse an unknown
    mode at the edge with their own `Literal`, so this is the second guard.
    """
    parts = [TRANSFORM_RULES, _RULES_WIN]
    parts.append(MODE_RULES.get(mode if mode in MODES else "faithful"))
    for name in purposes:
        rule = PURPOSE_RULES.get(name)
        if rule:
            parts.append(rule)
    parts.append(_MODE_WINS)
    parts.append(genre.system.strip())
    return "\n\n".join(p.strip() for p in parts if p and p.strip())
