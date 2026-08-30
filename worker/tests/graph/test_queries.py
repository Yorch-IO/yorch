"""The template registry and its validator.

The validator is the enforcement boundary for question answering — Memgraph does
not enforce read-only (see `test_client.py`) — so these tests are the closest
thing this project has to an authorisation test suite.
"""

from __future__ import annotations

import pytest

from brainworker.graph import queries as q
from brainworker.graph.queries import Param, Template, TemplateError
from brainworker.graph.schema import LEGACY_TENANT_ID as LEGACY


def tpl(cypher: str, **kw) -> Template:
    """A throwaway template that already satisfies the tenant rule.

    The rule has its own tests below; every *other* test here is about a
    different property — a forbidden keyword, an unbounded pattern, a clamp —
    and making each of them carry the predicate by hand would bury what they
    are actually asserting. `raw` is the escape hatch for the tests that are
    about the rule itself.
    """
    # A real clause, not a comment: `validate_template` strips comments before
    # it looks for parameters, so a `// $tenant_id` would leave the declared
    # parameter looking unused — which is its own, different error.
    cypher = f"WITH $tenant_id AS _scope\n{cypher.lstrip()}"
    params = tuple(kw.pop("params", ())) + (Param("tenant_id", "string"),)
    return Template(id="probe", summary="test", cypher=cypher, params=params, **kw)


def raw(cypher: str, **kw) -> Template:
    """A template exactly as written, for the tests about the tenant rule."""
    return Template(id="probe", summary="test", cypher=cypher, **kw)


# -- the shipped registry ---------------------------------------------------


def test_every_shipped_template_validates():
    """Import already asserts this; stating it as a test names the guarantee."""
    for t in q.TEMPLATES:
        q.validate_template(t)


def test_template_ids_are_unique():
    assert len({t.id for t in q.TEMPLATES}) == len(q.TEMPLATES)


def test_the_catalogue_never_shows_the_planner_any_cypher():
    """Sending query text invites the model to edit it, and costs tokens."""
    blob = repr(q.catalogue()).upper()
    for keyword in ("MATCH", "RETURN", "UNWIND", "WHERE"):
        assert keyword not in blob


def test_an_unknown_template_is_refused_by_name():
    with pytest.raises(TemplateError) as e:
        q.get("delete_everything")
    assert "delete_everything" in str(e.value)


# -- what the validator refuses --------------------------------------------


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (c:Chunk) DELETE c LIMIT 1",
        "MATCH (c:Chunk) DETACH DELETE c LIMIT 1",
        "CREATE (c:Chunk) RETURN c LIMIT 1",
        "MERGE (c:Chunk {id: 'x'}) RETURN c LIMIT 1",
        "MATCH (c:Chunk) SET c.text = '' RETURN c LIMIT 1",
        "MATCH (c:Chunk) REMOVE c.text RETURN c LIMIT 1",
        "LOAD CSV FROM '/etc/passwd' AS row RETURN row LIMIT 1",
        "CALL mg.load_all() YIELD * RETURN 1 LIMIT 1",
        "DROP INDEX ON :Chunk(id) LIMIT 1",
    ],
)
def test_a_template_that_writes_or_reaches_out_is_refused(cypher: str):
    with pytest.raises(TemplateError):
        q.validate_template(tpl(cypher))


def test_a_template_without_a_limit_is_refused():
    with pytest.raises(TemplateError, match="no LIMIT"):
        q.validate_template(tpl("MATCH (c:Chunk) RETURN c"))


@pytest.mark.parametrize("pattern", ["*", "*2..", "* ..", "*.."])
def test_an_unbounded_traversal_is_refused(pattern: str):
    cypher = f"MATCH (a:Section)-[:CONTAINS{pattern}]->(b:Section) RETURN b LIMIT 1"
    with pytest.raises(TemplateError, match="unbounded"):
        q.validate_template(tpl(cypher))


@pytest.mark.parametrize("pattern", ["*1..4", "*..4", "*3"])
def test_a_bounded_traversal_is_allowed(pattern: str):
    cypher = f"MATCH (a:Section)-[:CONTAINS{pattern}]->(b:Section) RETURN b LIMIT 1"
    q.validate_template(tpl(cypher))


def test_count_star_is_not_mistaken_for_an_unbounded_traversal():
    """A check with false positives is one reviewers learn to override."""
    q.validate_template(tpl("MATCH (c:Chunk) RETURN count(*) AS n LIMIT 1"))


def test_an_unknown_label_is_refused():
    with pytest.raises(TemplateError, match="unknown label"):
        q.validate_template(tpl("MATCH (x:Secrets) RETURN x LIMIT 1"))


def test_a_keyword_inside_a_string_literal_is_not_a_write():
    """Rejecting a title that contains "create" would be a false positive."""
    q.validate_template(
        tpl("MATCH (s:Section) WHERE s.title = 'How to create a graph' RETURN s LIMIT 1")
    )


def test_a_keyword_inside_a_comment_is_not_a_write():
    q.validate_template(tpl("// we never DELETE here\nMATCH (c:Chunk) RETURN c LIMIT 1"))


def test_a_semantic_traversal_must_declare_itself():
    """Otherwise the UI cannot tell the user the result rests on a model's guess."""
    cypher = "MATCH (c:Chunk)-[:MENTIONS]->(k:Concept) RETURN k LIMIT 1"
    with pytest.raises(TemplateError, match="uses_semantic_edges"):
        q.validate_template(tpl(cypher))
    q.validate_template(tpl(cypher, uses_semantic_edges=True))


def test_a_parameter_mismatch_in_either_direction_is_refused():
    with pytest.raises(TemplateError, match="undeclared"):
        q.validate_template(tpl("MATCH (c:Chunk {id: $x}) RETURN c LIMIT 1"))
    with pytest.raises(TemplateError, match="unused"):
        q.validate_template(
            tpl("MATCH (c:Chunk) RETURN c LIMIT 1", params=(Param("x", "id"),))
        )


# -- argument binding -------------------------------------------------------


def test_binding_rejects_an_id_that_is_not_one():
    t = q.get("document_outline")
    for bad in ["", "ver_x", "'; MATCH (n) DETACH DELETE n //", "ver_" + "z" * 24]:
        with pytest.raises(TemplateError):
            q.bind(t, {"tenant_id": LEGACY, "version_id": bad})


def test_binding_rejects_a_missing_required_argument():
    with pytest.raises(TemplateError, match="missing required"):
        q.bind(q.get("document_outline"), {"tenant_id": LEGACY, })


def test_binding_rejects_an_unexpected_argument():
    with pytest.raises(TemplateError, match="unexpected"):
        q.bind(
            q.get("document_outline"),
            {"tenant_id": LEGACY, "version_id": "ver_" + "a" * 24, "database": "other"},
        )


def test_an_optional_argument_falls_back_to_its_default():
    bound = q.bind(q.get("document_outline"), {"tenant_id": LEGACY, "version_id": "ver_" + "a" * 24})
    assert bound["limit"] == 25


def test_the_limit_is_clamped_no_matter_what_the_planner_asks_for():
    t = q.get("document_outline")
    vid = "ver_" + "a" * 24
    assert q.bind(t, {"tenant_id": LEGACY, "version_id": vid, "limit": 10_000})["limit"] == q.MAX_LIMIT
    assert q.bind(t, {"tenant_id": LEGACY, "version_id": vid, "limit": 0})["limit"] == 1
    assert q.bind(t, {"tenant_id": LEGACY, "version_id": vid, "limit": -5})["limit"] == 1


def test_a_bool_is_not_an_int():
    """`True == 1` in Python; a planner returning a bool is returning nonsense."""
    with pytest.raises(TemplateError, match="must be an int"):
        q.bind(
            q.get("document_outline"),
            {"tenant_id": LEGACY, "version_id": "ver_" + "a" * 24, "limit": True},
        )


def test_an_id_list_is_element_checked_and_truncated():
    t = q.get("citations_for_chunks")
    good = "chk_" + "a" * 24
    with pytest.raises(TemplateError, match="list of graph ids"):
        q.bind(t, {"tenant_id": LEGACY, "chunk_ids": [good, "not-an-id"]})
    with pytest.raises(TemplateError, match="list of graph ids"):
        q.bind(t, {"tenant_id": LEGACY, "chunk_ids": good})  # a bare string is not a list
    bound = q.bind(t, {"tenant_id": LEGACY, "chunk_ids": [good] * (q.MAX_LIMIT + 50)})
    assert len(bound["chunk_ids"]) == q.MAX_LIMIT


# -- the overview templates and their own ceilings --------------------------


def test_a_capped_parameter_clamps_at_its_own_ceiling_not_the_global_one():
    """A whole-library canvas needs more rows than a planner may ever have.
    The ceiling moves; it does not disappear."""
    t = q.get("library_mentions")
    cap = next(p.cap for p in t.params if p.name == "mention_limit")
    bound = q.bind(t, {"tenant_id": LEGACY, "library_id": "lib_x", "mention_limit": cap * 10})
    assert bound["mention_limit"] == cap > q.MAX_LIMIT


def test_an_uncapped_parameter_still_clamps_at_the_global_ceiling():
    """The escape hatch is per parameter. Adding one must not have loosened the
    rule for every template that never asked."""
    t = q.get("document_outline")
    bound = q.bind(t, {"tenant_id": LEGACY, "version_id": "ver_" + "a" * 24, "limit": 10_000})
    assert bound["limit"] == q.MAX_LIMIT


def test_every_template_that_raises_its_ceiling_is_hidden_from_the_planner():
    """The two are one decision. A template a model can name may not return
    more rows than `MAX_LIMIT` allows, whatever it declares."""
    for t in q.TEMPLATES:
        raised = any(p.cap is not None and p.cap > q.MAX_LIMIT for p in t.params)
        assert not (raised and t.planner_visible), t.id


def test_the_catalogue_omits_the_overview_templates():
    offered = {t["id"] for t in q.catalogue()}
    hidden = {t.id for t in q.TEMPLATES if not t.planner_visible}
    assert hidden == {
        "graph_node_counts",
        "graph_edge_counts",
        "library_documents",
        "library_mentions",
    }
    assert offered.isdisjoint(hidden)
    assert offered  # and it did not empty itself


def test_a_hidden_template_is_still_reachable_by_id():
    """Hidden from the planner, not withdrawn: the API names them directly."""
    assert q.get("library_mentions").id == "library_mentions"


def test_the_library_scope_cannot_be_omitted_from_the_overview():
    """Concepts merge by canonical name with no library in the id. A defaulted
    scope would mean a caller who forgot one silently got a different library."""
    with pytest.raises(TemplateError, match="missing required argument"):
        q.bind(q.get("library_mentions"), {"tenant_id": LEGACY, })
    with pytest.raises(TemplateError, match="missing required argument"):
        q.bind(q.get("library_documents"), {})


# -- the tenant rule --------------------------------------------------------
#
# Checked in the validator rather than left to a test, because a template that
# loads is a template that can be named by id. These assert the rule itself; the
# *isolation* it buys is proved against a real database in
# `test_tenant_isolation.py`.


def test_a_template_that_does_not_scope_itself_does_not_load():
    with pytest.raises(TemplateError, match="tenant_id"):
        q.validate_template(
            raw(
                "MATCH (c:Chunk {id: $chunk_id}) RETURN c.id AS id LIMIT 1",
                params=(Param("chunk_id", "id"),),
            )
        )


def test_the_rule_is_satisfied_by_using_the_parameter_not_by_declaring_it():
    """Declaring `tenant_id` and never filtering on it would be worse than not
    declaring it: it would look scoped in the parameter list and read
    everything. The check is on *used* parameters, and the validator's own
    unused-parameter rule closes the other direction."""
    with pytest.raises(TemplateError, match="unused parameter"):
        q.validate_template(
            raw(
                "MATCH (c:Chunk {id: $chunk_id}) RETURN c.id AS id LIMIT 1",
                params=(Param("chunk_id", "id"), Param("tenant_id", "string")),
            )
        )


def test_every_shipped_template_is_scoped():
    """The registry as it ships, not a constructed example."""
    for t in q.TEMPLATES:
        assert any(p.name == "tenant_id" for p in t.params), t.id
        assert "$tenant_id" in t.cypher, t.id


def test_the_tenant_is_required_and_has_no_default():
    """A default would be somebody's organisation, and the wrong somebody."""
    for t in q.TEMPLATES:
        tenant = next(p for p in t.params if p.name == "tenant_id")
        assert tenant.required, t.id
        assert tenant.default is None, t.id
