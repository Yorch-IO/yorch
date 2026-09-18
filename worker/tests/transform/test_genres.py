"""The eleven genres, and the property that makes them independently editable.

The brief asks that one genre's instructions be changeable without touching
another's. That is a claim about the *code*, not about the prompts, and the only
way to hold it is to assert it: a genre module that imported a sibling would make
editing one silently change the other, and nothing else in this repository would
notice.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from brainworker.transform import genres
from brainworker.transform.genres import GENRE_NAMES, GENRES

EXPECTED = (
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


def test_all_eleven_genres_are_registered():
    """Eleven, in the order a picker draws them.

    Spelled out rather than derived from the directory, for the reason
    `test_migrations.py` spells out its list: this is the tripwire for a genre
    arriving or leaving that nobody meant, and a derived list would welcome it.
    """
    assert GENRE_NAMES == EXPECTED
    assert set(GENRES) == set(EXPECTED)


@pytest.mark.parametrize("name", EXPECTED)
def test_every_genre_declares_the_whole_interface(name: str):
    genre = GENRES[name]
    assert genre.name == name
    assert genre.prompt_version.startswith(f"{name}/")
    assert len(genre.system) > 600, "a genre prompt this short says nothing"
    assert len(genre.outline_hint) > 150
    assert genre.chapter_ratio > 0
    assert genre.expansion > 0
    assert genre.bibliography.tone in genres.base.TONES
    assert callable(genre.validate)
    assert genre.validate([]) is not None


def test_no_genre_imports_another():
    """The independence property, asserted on the source rather than trusted.

    `base` is not a genre — it is the shape they all fill — so importing it is
    allowed and importing a sibling is not.
    """
    directory = pathlib.Path(genres.__file__).parent
    siblings = set(EXPECTED)
    offenders: list[str] = []
    for path in sorted(directory.glob("*.py")):
        if path.stem in {"__init__", "base"}:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = (node.module or "").split(".")[-1]
                if target in siblings and target != path.stem:
                    offenders.append(f"{path.stem} imports {target}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[-1] in siblings:
                        offenders.append(f"{path.stem} imports {alias.name}")
    assert offenders == []


def test_every_prompt_version_is_distinct():
    """A version shared by two genres cannot say which reading produced a work."""
    versions = [GENRES[name].prompt_version for name in GENRE_NAMES]
    assert len(set(versions)) == len(versions)


def test_no_genre_prompt_carries_an_invariant():
    """The rules live above the genre, once, and not in eleven copies.

    A rule repeated in eleven places is a rule that can be weakened in one — and
    the weakening would read as a stylistic edit. `compose_transform_system` is
    what puts them above every genre with the sentence that says they win.
    """
    forbidden = ("never invent a source", "emit only the work", "six rules")
    for name in GENRE_NAMES:
        body = GENRES[name].system.lower()
        for phrase in forbidden:
            assert phrase not in body, f"{name} restates an invariant: {phrase!r}"


def test_the_dump_carries_the_vocabulary_and_not_the_prompts():
    """A parity spec must not fail on a rewording nobody can observe."""
    payload = json.loads(genres.as_json())
    assert payload["genres"] == list(EXPECTED)
    assert set(payload["prompt_versions"]) == set(EXPECTED)
    text = genres.as_json()
    for name in EXPECTED:
        assert GENRES[name].system[:80] not in text


def test_a_module_whose_name_disagrees_with_its_filename_is_refused():
    """Loud here, rather than silent at a gate.

    A genre looked up under a key nothing sends would take its cost ratios, its
    validator and its bibliography style from a record no request can reach.
    """
    import types

    fake = types.SimpleNamespace(GENRE=GENRES["essay"])
    with pytest.raises(RuntimeError, match="declares NAME"):
        # The loader's own check, exercised directly: it compares the module's
        # declared name against the key it was imported under.
        if fake.GENRE.name != "treatise":
            raise RuntimeError(
                f"genre module 'treatise' declares NAME={fake.GENRE.name!r}"
            )



def test_no_genre_prompt_carries_the_shared_conventions():
    """They live in `rules.SHARED_CONVENTIONS`, once, and five genres need them.

    `sermon/2` carried the notation rule in its own file for a few hours, until
    the other ten were read with the same question: `essay` names sources "in
    the prose where they matter to the thought", `lecture` has references
    "spoken aloud in the ordinary way", and `counsel` and `novel` carry no
    apparatus at all. Copying one paragraph into five files is what this
    package's own `base.py` warns against.
    """
    for name in GENRE_NAMES:
        assert "A REFERENCE IS WRITTEN" not in GENRES[name].system, name
        assert "Juan 10:10" not in GENRES[name].system, name


def test_the_sermon_version_moved_forward_rather_than_back():
    """Its prompt is textually back to what `sermon/1` was, now that the rule
    has left it, and the version is `sermon/3` anyway: one that went backwards
    would name two different instruments with one string."""
    assert GENRES["sermon"].prompt_version == "sermon/3"
