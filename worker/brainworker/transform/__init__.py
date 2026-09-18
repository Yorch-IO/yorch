"""Recasting an indexed document into another literary genre.

The vocabulary a client sends — eleven genre names, two modes, four research
purposes — and the live dump the TypeScript fork's parity spec compares itself
against.

**The wire carries names, never numbers.** A client sends `treatise`,
`faithful`, `["context"]`; this package is the only place that says what any of
them means. That is the rule `effort.py` states for its own levels and it buys
the same two things: a request cannot ask for two hundred chapters, and the paid
plane's fork stays a list of strings instead of a table that needs a live dump
to stay honest.

**The genre prompts are not in the dump and must not be.** They are code
constants on this side only — no plane renders one, no client shows one — so a
fork has nothing to keep in step, and putting several thousand characters of
prose through a parity spec would fail it on a rewording that changes nothing
anybody can observe.
"""

from __future__ import annotations

import dataclasses
import json

from .genres import GENRE_NAMES, GENRES, Genre
from .types import (
    DEFAULT_MODE,
    MAX_CHAPTERS,
    MAX_UNCOVERED_FRACTION,
    MODES,
    PURPOSES,
    TransformOptions,
    TransformRequest,
)

__all__ = [
    "DEFAULT_MODE",
    "GENRES",
    "GENRE_NAMES",
    "Genre",
    "MODES",
    "PURPOSES",
    "TransformOptions",
    "TransformRequest",
    "as_json",
]


def _fields(cls) -> list[dict]:
    """A dataclass's declared fields, as the parity spec reads them.

    `required` is what matters most here and it is not cosmetic: Temporal's
    converter **silently drops a key naming no field**, so a plane that
    hand-builds one of these payloads and misspells `tenant_id` gets a
    cross-tenant read with no error anywhere. `ChatTurn.tenant_id` is required
    for exactly that reason and a parity spec asserts Python refuses to supply
    one; `TransformRequest` is new, so it gets the rule rather than the excuse.
    """
    return [
        {
            "name": f.name,
            "default": (
                None if f.default is dataclasses.MISSING else f.default
            ),
            "required": (
                f.default is dataclasses.MISSING
                and f.default_factory is dataclasses.MISSING
            ),
        }
        for f in dataclasses.fields(cls)
    ]


def as_json() -> str:
    """The whole wire vocabulary, live at test time.

    Never a committed fixture, for the reason `queries.parity.spec.ts` records
    about its own: a snapshot only detects drift if something forces it to be
    refreshed, and nothing does.
    """
    return json.dumps(
        {
            "genres": list(GENRE_NAMES),
            "modes": list(MODES),
            "default_mode": DEFAULT_MODE,
            "purposes": list(PURPOSES),
            "max_chapters": MAX_CHAPTERS,
            "max_uncovered_fraction": MAX_UNCOVERED_FRACTION,
            "request_fields": _fields(TransformRequest),
            "option_fields": _fields(TransformOptions),
        },
        indent=2,
        sort_keys=True,
    )


# No `if __name__ == "__main__"` block: this is a package, so `python -m` cannot
# reach one. The parity spec invokes it the way every other dump in this
# codebase is invoked from TypeScript:
#
#     python -c "from brainworker.transform import as_json; print(as_json())"
