"""What the Bolt client actually guarantees, measured against Memgraph 3.12.0."""

from __future__ import annotations

import pytest

from brainworker.graph import Graph, GraphError
from brainworker.graph.schema import LABELS


def test_read_session_does_not_enforce_read_only(graph: Graph):
    """The measurement the client's docstring rests on.

    Memgraph executes a write inside a `default_access_mode="READ"` session:
    Bolt's access mode is a Neo4j routing hint, and Memgraph's RBAC is an
    Enterprise feature this stack does not have. The security property therefore
    comes from `Graph.query` accepting only a template id — not from the
    session flag.

    If this test ever fails, that is *good news*: the database started enforcing
    it. Invert the assertion and promote the flag from belt to braces.
    """
    marker = "chk_" + "f" * 24
    try:
        graph._read(
            "CREATE (n:Chunk {id: $id, text: 'written from a READ session'}) "
            "RETURN n LIMIT 1",
            {"id": marker},
        )
        wrote = True
    except GraphError:
        wrote = False
    finally:
        graph.write("MATCH (n:Chunk {id: $id}) DETACH DELETE n", {"id": marker})

    assert wrote, (
        "Memgraph now refuses writes in a READ session. Update "
        "brainworker/graph/client.py — the read path has a second layer again."
    )


def test_query_only_accepts_a_registered_template(graph: Graph):
    from brainworker.graph.queries import TemplateError

    with pytest.raises(TemplateError):
        graph.query("MATCH (n) DETACH DELETE n")


def test_ensure_schema_is_idempotent(graph: Graph):
    """It runs on every worker start, not once in a migration."""
    graph.ensure_schema()
    graph.ensure_schema()

    rows = graph.write("SHOW INDEX INFO;")
    indexed = {r.data.get("label") for r in rows}
    assert LABELS <= indexed, f"missing indexes for {sorted(LABELS - indexed)}"


def test_probe_reports_a_node_count_and_a_latency(graph: Graph):
    detail = graph.probe()
    assert "node(s)" in detail and "ms" in detail


def test_an_unreachable_graph_raises_a_typed_error():
    """The UI branches on `kind`; a bare message would only be echoed."""
    with pytest.raises(GraphError) as e:
        with Graph("bolt://127.0.0.1:1", timeout=2.0) as g:
            g.probe()
    assert e.value.kind == "graph_unreachable"
