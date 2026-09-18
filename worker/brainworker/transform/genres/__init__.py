"""The eleven target genres, as a registry.

Ordered as the brief orders them, and the order is what a picker draws. It runs
roughly from the most architectural to the most performed, which is also the
order in which the invention rules tighten: a treatise invents nothing because
it has no occasion to, and a novel invents nothing because it is told not to.

**A genre is added by writing a module and naming it in `_MODULES`.** Nothing
else in this package enumerates them — the estimate reads `chapter_ratio` off
the record, the bibliography reads `BIBLIOGRAPHY`, the planner reads `validate`
— so the failure mode of forgetting one is a missing genre rather than a genre
that half exists.
"""

from __future__ import annotations

import json
from importlib import import_module

from .base import BibliographyStyle, Genre, intents, titles

#: Module names, in the order a picker draws them.
_MODULES: tuple[str, ...] = (
    "treatise",
    "essay",
    "commentary",
    "study",
    "biography",
    "history",
    "counsel",
    "novel",
    "sermon",
    "lecture",
    "chronicle",
)


def _load() -> dict[str, Genre]:
    out: dict[str, Genre] = {}
    for name in _MODULES:
        module = import_module(f".{name}", __name__)
        genre: Genre = module.GENRE
        if genre.name != name:
            # A module whose `NAME` and filename disagree is one whose prompt
            # version, cost ratios and validator would be looked up under a key
            # nothing sends. Loud here rather than silent at a gate.
            raise RuntimeError(
                f"genre module {name!r} declares NAME={genre.name!r}"
            )
        out[name] = genre
    return out


GENRES: dict[str, Genre] = _load()

#: The vocabulary the wire carries. Names, never the numbers behind them — the
#: rule `effort.py` states for its own levels: a client that sent the figures
#: could ask for anything, and the paid plane would have to police a table it
#: does not own.
GENRE_NAMES: tuple[str, ...] = tuple(GENRES)


def get(name: str) -> Genre:
    """The genre, or `KeyError`.

    Deliberately not total, unlike `effort.budget_for`. A level this product
    does not know still has a sensible answer — the default — because a question
    asked at an unknown effort is still a question. A *genre* this product does
    not know has no sensible answer: silently writing an essay for somebody who
    asked for a chronicle is worse than refusing, and both planes validate the
    name at the edge with their own `Literal`, where a caller can be told.
    """
    return GENRES[name]


def as_json() -> str:
    """The genre vocabulary, for the TypeScript fork's parity spec.

    Live at test time rather than into a committed fixture, for the reason
    `queries.parity.spec.ts` records about its own: a snapshot only detects
    drift if something forces it to be refreshed, and nothing does.

    The prompts themselves are **not** in this dump. They are code constants on
    this side only — no plane and no client renders one — so a fork has nothing
    to keep in step, and putting several thousand characters of prose through a
    parity spec would make the spec fail on a rewording that changes no
    behaviour anybody can see.
    """
    return json.dumps(
        {
            "genres": list(GENRE_NAMES),
            "prompt_versions": {
                name: GENRES[name].prompt_version for name in GENRE_NAMES
            },
        },
        indent=2,
        sort_keys=True,
    )


if __name__ == "__main__":  # pragma: no cover - a dump, not behaviour
    print(as_json())


__all__ = [
    "GENRES",
    "GENRE_NAMES",
    "BibliographyStyle",
    "Genre",
    "as_json",
    "get",
    "intents",
    "titles",
]
