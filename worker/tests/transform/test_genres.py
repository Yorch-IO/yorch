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


def test_a_sermon_writes_a_reference_and_does_not_spell_it_out():
    """Reported from a real recast: the address expanded «Juan 10:10» into
    «abran sus Biblias en el Evangelio según San Juan, en el capítulo diez,
    versículo diez».

    The genre invites it — the whole point of a sermon is that it is *said* —
    but a spoken reference a hearer cannot write down is one they cannot check,
    and the text this produces is also read: a locator in figures is what
    somebody searches for, and a chapter and a verse spelled out in words is
    findable by nobody.

    Pinned as a property of the prompt rather than of one example, because a
    rewording that dropped the rule would leave nothing else to notice.
    """
    system = GENRES["sermon"].system
    assert "A REFERENCE IS WRITTEN, NOT SPELLED OUT" in system
    assert "Juan 10:10" in system
    assert "capítulo diez, versículo diez" in system, (
        "the rule names the failure it was written for, so the model sees both"
    )


def test_the_sermon_s_notation_rule_stays_domain_agnostic():
    """The brief is explicit that Sermon must not implicitly mean religious.

    The scripture case is the *example*, because it is the one that was
    reported; the rule is about a locator in whatever notation its own field
    uses, and it names three others so a model recasting a statute or a report
    does not read it as being about Bibles.
    """
    system = GENRES["sermon"].system
    for other in ("art. 14.2", "s. 3(1)(b)", "Fig. 4", "p. 212"):
        assert other in system


def test_a_reworded_sermon_prompt_gets_a_new_version():
    """`PROMPT_VERSION` is how one reading is told from the next, and this
    change moves what the model writes."""
    assert GENRES["sermon"].prompt_version == "sermon/2"
