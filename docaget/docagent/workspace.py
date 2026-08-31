"""Where the engine's stateful assets live.

Three directories outlive a run and are worth money: `profiles/` holds what a
document family learned, `cache/correct/` holds corrections already paid for,
and `cache/embed/` holds vectors already paid for. Until now all three resolved
against the process CWD, and the only way to relocate them was `os.chdir` —
which the Docker image does with `WORKDIR /workspace` and `brainworker.config`
does with `configure()`.

**That is why this exists.** `os.chdir` is process-global, and the Temporal
worker runs activities concurrently: two organisations' documents can be in
flight in one process, and they must not share a profile directory. A profile
reused across organisations by structural fingerprint carries its
`header_patterns`, which derive from a book's running header and are often its
title — the leak `CLAUDE.md` records as the open one of the three.

So every function that touches one of these takes a root, and this dataclass is
what a caller passes around instead of four separate paths. `Workspace.cwd()`
reproduces exactly the previous behaviour, which is what the CLI keeps using.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

#: The CWD-relative defaults, named here so `cwd()` and the modules' own
#: fallbacks cannot drift apart.
DEFAULT_PROFILES = pathlib.Path("profiles")
DEFAULT_CORRECT_CACHE = pathlib.Path("cache/correct")
DEFAULT_EMBED_CACHE = pathlib.Path("cache/embed")


@dataclass(frozen=True)
class Workspace:
    profiles: pathlib.Path
    correct_cache: pathlib.Path
    embed_cache: pathlib.Path

    @classmethod
    def cwd(cls) -> "Workspace":
        """Today's behaviour: everything relative to the process CWD."""
        return cls(
            profiles=DEFAULT_PROFILES,
            correct_cache=DEFAULT_CORRECT_CACHE,
            embed_cache=DEFAULT_EMBED_CACHE,
        )

    @classmethod
    def under(cls, root: pathlib.Path | str) -> "Workspace":
        """All three under one root, in the layout the CLI already writes.

        The caller decides what `root` is. The worker passes a *tenant's* root
        for `profiles`, and the volume root for the two caches — the correction
        cache is content-addressed (`sha256(prompt_version + text)`), so sharing
        an entry requires already holding that paragraph. That is a saving, not
        a channel, and the same is true of an embedding.
        """
        root = pathlib.Path(root)
        return cls(
            profiles=root / "profiles",
            correct_cache=root / "cache" / "correct",
            embed_cache=root / "cache" / "embed",
        )
