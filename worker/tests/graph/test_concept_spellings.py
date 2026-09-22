"""The display name is the majority spelling, and it converges.

Whichever document reached a shared concept first used to own its label for the
whole corpus with no tie-break: measured by replaying all 41 semantics
artifacts in timestamp order, **231 of 13,005 concepts (1.8%)** carried a name
that is not the majority spelling — `SEÑOR` over 20 mentions of `Señor`,
`Darío Silva Silva` over 12 of the book's own `Darío Silva-Silva`. Both graph
screens render those.

Against a real Memgraph, because what is under test is a write that has to be
idempotent under a Temporal retry *and* revisable by a re-index, which is a
property of the statement rather than of the arithmetic.
"""

from __future__ import annotations

import uuid

import pytest

from brainworker.graph import projection as proj
from brainworker.graph.schema import concept_id

TENANT = "tnt_" + "0" * 20 + "1"


@pytest.fixture
def name_of(graph):
    def read(canonical: str) -> str | None:
        rows = graph.write(
            "MATCH (k:Concept {id: $id}) RETURN k.name AS name, k.spellings AS s",
            {"id": concept_id(canonical, TENANT)},
        )
        return rows[0]["name"] if rows else None

    return read


def concept(name: str, spellings: dict[str, int]) -> dict:
    return {"name": name, "type": None, "descriptions": [], "spellings": spellings}


def test_the_majority_spelling_wins_over_the_one_seen_first(graph, name_of):
    """The recorded case: a heading in capitals, then prose."""
    word = f"señor {uuid.uuid4().hex[:8]}"
    shouted, proper = word.upper(), word.capitalize()

    proj.project_concepts(graph, [concept(shouted, {shouted: 4})],
                          tenant=TENANT, version_id="ver_first")
    assert name_of(word) == shouted, "the first version's spelling, so far"

    proj.project_concepts(graph, [concept(proper, {proper: 20})],
                          tenant=TENANT, version_id="ver_second")
    assert name_of(word) == proper, "20 mentions must outvote 4"


def test_a_retry_revises_a_version_s_vote_rather_than_doubling_it(graph, name_of):
    """`SET list = list + new` is the one write in this module a retry would
    double, and a *count* cannot be deduplicated by value — so the entries are
    keyed by version and the version's previous ones are dropped first."""
    word = f"estado {uuid.uuid4().hex[:8]}"
    lower, upper = word, word.capitalize()

    proj.project_concepts(graph, [concept(upper, {upper: 9})],
                          tenant=TENANT, version_id="ver_a")
    for _ in range(3):
        proj.project_concepts(graph, [concept(lower, {lower: 4})],
                              tenant=TENANT, version_id="ver_b")
    assert name_of(word) == upper, "three retries of 4 must not beat 9"


def test_a_re_index_revises_the_same_version_s_vote(graph, name_of):
    """Re-indexing under a different cutting is a revised vote, not a second
    one — the same property the retry needs, reached deliberately."""
    word = f"alma {uuid.uuid4().hex[:8]}"
    a, b = word.capitalize(), word.upper()

    proj.project_concepts(graph, [concept(a, {a: 5})], tenant=TENANT, version_id="ver_x")
    proj.project_concepts(graph, [concept(b, {b: 3})], tenant=TENANT, version_id="ver_y")
    assert name_of(word) == a
    # `ver_x` re-indexed, now seeing the other spelling twice as often.
    proj.project_concepts(graph, [concept(b, {b: 9})], tenant=TENANT, version_id="ver_x")
    assert name_of(word) == b


def test_one_run_that_saw_both_spellings_votes_for_both(graph, name_of):
    """One entry per (version, spelling), not per (version, concept): a run's
    own first-seen winner must not be laundered into a count of one."""
    word = f"iglesia {uuid.uuid4().hex[:8]}"
    lower, upper = word, word.capitalize()

    proj.project_concepts(
        graph, [concept(lower, {lower: 3, upper: 10})],
        tenant=TENANT, version_id="ver_only",
    )
    assert name_of(word) == upper, "the run's own minority spelling cannot win"


def test_a_caller_with_no_version_leaves_the_name_alone(graph, name_of):
    """A replay of a `semantics.json` written before spellings existed carries
    none, and skipping the tally is the same "an absent field takes its
    default" rule every payload here follows."""
    word = f"gracia {uuid.uuid4().hex[:8]}"
    first, other = word.capitalize(), word.upper()

    proj.project_concepts(graph, [concept(first, {first: 1})],
                          tenant=TENANT, version_id="ver_1")
    proj.project_concepts(graph, [concept(other, {other: 50})], tenant=TENANT)
    assert name_of(word) == first, "no version id, no vote, no rename"


def test_a_tie_is_broken_the_same_way_every_time(graph, name_of):
    """An arbitrary winner is what this replaces; an arbitrary winner that
    *moves* between projections would be worse."""
    word = f"fe {uuid.uuid4().hex[:8]}"
    a, b = word.capitalize(), word.upper()

    proj.project_concepts(graph, [concept(a, {a: 2})], tenant=TENANT, version_id="v1")
    proj.project_concepts(graph, [concept(b, {b: 2})], tenant=TENANT, version_id="v2")
    settled = name_of(word)
    for _ in range(3):
        proj.project_concepts(graph, [concept(b, {b: 2})], tenant=TENANT, version_id="v2")
        assert name_of(word) == settled
