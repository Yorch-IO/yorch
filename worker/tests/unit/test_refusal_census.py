"""The census's one decision: its scope is production's scope.

`scripts/refusal_census.py` counts questions the dense floor refuses although
the corpus demonstrably holds their answer. Everything else in it is a store
read that holds no decision — the split `test_audit_version.py` and
`test_probe_retrieval.py` already use — but the *filters* it searches under are
a decision, and a silent one: a scope key that `retrieve.search` assigns and
the census omits does not fail anything. It widens the census's candidate pool,
so fewer questions are refused, so the census under-reports and reads as good
news.

`disabled` is the live instance of that. It arrived with chunk editing, after
this measurement's shape was designed, and a census that ignored it would count
refusals the product would never make.

So this scans `retrieve.search` for the keys it assigns unconditionally and
requires the census to carry each one. A source scan rather than a call,
because reaching `search` needs a Qdrant, a provider and money, and the
property under test is which keys exist — not what they match.
"""

from __future__ import annotations

import ast
import pathlib

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "refusal_census.py"
RETRIEVE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "brainworker" / "answering" / "retrieve.py"
)


def _scope_keys_assigned_by_search() -> set[str]:
    """Every literal key `search` writes into `filters` at its own top level.

    Top level only, deliberately: a key assigned inside an `if` is a narrowing
    the caller asked for, and this is about the scope every question is
    confined to regardless.
    """
    tree = ast.parse(RETRIEVE.read_text("utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "search"
    )
    keys = set()
    for node in fn.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "filters"
                and isinstance(target.slice, ast.Constant)
            ):
                keys.add(target.slice.value)
    return keys


def _census_scope_keys() -> dict[str, set[str]]:
    """The literal keys each scope dict in `_scopes` is built with."""
    tree = ast.parse(SCRIPT.read_text("utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_scopes"
    )
    found: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        # `library = {...}` — the keys are literals in the dict.
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            name = node.targets[0].id  # type: ignore[union-attr]
            found[name] = {
                k.value for k in node.value.keys
                if isinstance(k, ast.Constant)
            }
        # `version_only["disabled"] = …` — assigned after a copy, because the
        # rest of that scope comes from `version_scope`.
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and isinstance(node.targets[0].slice, ast.Constant)
        ):
            found.setdefault(node.targets[0].value.id, set()).add(
                node.targets[0].slice.value
            )
    return found


def test_search_still_assigns_the_scope_keys_this_test_knows_about():
    """Guards the guard: a rename in `search` must not leave this scanning for
    a key that no longer exists and passing because it found nothing to check."""
    assert _scope_keys_assigned_by_search() >= {"tenant_id", "library_id", "disabled"}


#: What naming a version already implies. A `version_id` belongs to exactly one
#: library in exactly one organisation, so a version scope that omits those two
#: is narrower than production rather than wider — which is the direction that
#: cannot manufacture a refusal. Anything *not* in here has to be carried by
#: every scope, because nothing else in the filter implies it.
IMPLIED_BY_A_VERSION = {"tenant_id", "library_id"}


def test_every_scope_the_census_searches_under_excludes_what_a_person_hid():
    """`disabled` is implied by nothing, so both scopes must carry it.

    This is the key that would silently widen the census: a hidden chunk that
    reaches the probe clears the floor for a question the product would have
    refused, so the census reports a refusal rate lower than the real one and
    the feature it is deciding looks less necessary than it is.
    """
    unimplied = _scope_keys_assigned_by_search() - IMPLIED_BY_A_VERSION
    assert unimplied, "every key search assigns is now implied by a version id; re-read this"
    scopes = _census_scope_keys()
    assert scopes, "no scope dicts found in _scopes; the scan is looking at the wrong shape"
    for name, keys in scopes.items():
        missing = unimplied - keys
        assert not missing, (
            f"refusal_census._scopes[{name}] omits {sorted(missing)}, which "
            f"retrieve.search assigns and a version id does not imply — the "
            f"census would count refusals the product never makes"
        )


def test_the_library_scope_is_exactly_the_scope_a_question_meets():
    """The headline number is the library-scope one, because `retrieve.search`
    is scoped to a library. It must reproduce every key production assigns,
    including the two a version scope may leave out."""
    assert _census_scope_keys()["library"] >= _scope_keys_assigned_by_search()
