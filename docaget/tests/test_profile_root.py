"""Profiles resolve against a root the caller names, not the process CWD.

`CLAUDE.md` lists three things tenancy does not cover, and calls profiles "the
open one": a profile reused across organisations by structural fingerprint
carries its `header_patterns`, which derive from a book's running header and are
often its title. Closing it needed exactly this — an explicit root instead of a
`chdir`, because `os.chdir` is process-global and the Temporal worker runs
activities for more than one organisation in one process.
"""

from __future__ import annotations

import pathlib

from docagent import profiles as prof
from docagent.workspace import Workspace


def a_profile(slug: str, fp: str = "abcd1234", learned_at: float = 1.0) -> prof.Profile:
    return prof.Profile(
        fingerprint=fp,
        slug=slug,
        extractor="pdf_text",
        learned_from="libros/uno.pdf",
        learned_at=learned_at,
    )


def test_two_roots_do_not_see_each_others_profiles(tmp_path):
    """The property the leak was: same structure, different organisation."""
    theirs, mine = tmp_path / "acme", tmp_path / "preprod"
    a_profile("suyo").save(theirs)

    assert prof.load("abcd1234", theirs) is not None
    assert prof.load("abcd1234", mine) is None
    assert prof.all_profiles(mine) == []


def test_the_newest_still_wins_within_one_root(tmp_path):
    """More than one file can carry a fingerprint. Returning whichever sorted
    first made the family's rules depend on the title of the book that learned
    them — a stale experiment beat a profile with eight measured revisions."""
    root = tmp_path / "profiles"
    a_profile("viejo", learned_at=1.0).save(root)
    a_profile("nuevo", learned_at=2.0).save(root)

    assert prof.load("abcd1234", root).slug == "nuevo"


def test_no_root_means_the_working_directory_so_the_cli_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a_profile("por-omision").save()

    assert (tmp_path / "profiles" / "por-omision.json").exists()
    assert prof.load("abcd1234").slug == "por-omision"


def test_a_monkeypatched_module_constant_still_takes_effect(tmp_path, monkeypatch):
    """Resolved at call time, never captured — which is how the suite keeps a
    test from writing into the real `profiles/`."""
    monkeypatch.setattr(prof, "PROFILE_DIR", tmp_path / "elsewhere")
    a_profile("desviado").save()

    assert (tmp_path / "elsewhere" / "desviado.json").exists()


def test_the_workspace_names_all_three_stateful_directories(tmp_path):
    ws = Workspace.under(tmp_path)

    assert ws.profiles == tmp_path / "profiles"
    assert ws.correct_cache == tmp_path / "cache" / "correct"
    assert ws.embed_cache == tmp_path / "cache" / "embed"


def test_the_cwd_workspace_reproduces_the_previous_constants():
    """The three literals as they were before any of this became a parameter, so
    an existing checkout keeps reading the profiles and caches it already has.

    Compared against the literals rather than against the modules' constants:
    `conftest` monkeypatches `vertex.EMBED_CACHE_DIR` away from the real cache
    for the whole session, which is itself the behaviour being preserved.
    """
    ws = Workspace.cwd()

    assert ws.profiles == pathlib.Path("profiles")
    assert ws.correct_cache == pathlib.Path("cache/correct")
    assert ws.embed_cache == pathlib.Path("cache/embed")

    from docagent import correct

    assert prof.PROFILE_DIR == ws.profiles
    assert correct.CACHE_DIR == ws.correct_cache
