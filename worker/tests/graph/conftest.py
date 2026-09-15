"""Fixtures for the tests that need a real Memgraph.

These are integration tests and they skip — loudly, with the URL they tried —
when nothing answers on Bolt. That is the same bargain the engine's corpus
resolver makes: a skipped test that says why is useful, a silently green one is
not.

**Nothing here wipes the database.** Every test works inside a version id
derived from a random digest and tears down only what it created, so pointing
`BRAIN_MEMGRAPH_URL` at a graph holding real documents cannot destroy it. A
`MATCH (n) DETACH DELETE n` in a fixture is one misconfigured environment
variable away from deleting a user's library.
"""

from __future__ import annotations

import os
import secrets

import pytest

from brainworker.graph import Graph, GraphError
from brainworker.graph.projection import ChunkNode, SectionNode, VersionNode
from brainworker.graph.schema import LEGACY_TENANT_ID

URL = os.environ.get("BRAIN_MEMGRAPH_URL", "bolt://127.0.0.1:7788")


class _ScopedGraph:
    """A `Graph` that fills in the tenant when a test did not name one.

    Every template now requires `$tenant_id`, and most of the tests in this
    directory are not about tenancy — they are about whether the confidence
    floor filters, whether the degree counts books, whether a claim names a
    chunk a person can check. Threading a tenant through each of them would add
    a constant to thirty call sites and make none of them clearer.

    **This convenience is exactly why isolation has a test of its own.**
    `test_tenant_isolation.py` builds two organisations with the same shape and
    asserts each sees only its own; nothing there goes through this wrapper. A
    test double that quietly supplies the thing under test would be worthless,
    so the thing under test is tested somewhere this double cannot reach.
    """

    def __init__(self, inner: Graph):
        self._inner = inner

    def query(self, template_id, args=None):
        args = dict(args or {})
        args.setdefault("tenant_id", LEGACY_TENANT_ID)
        return self._inner.query(template_id, args)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture(scope="session")
def graph():
    try:
        g = Graph(URL, timeout=5.0)
        g.probe()
    except Exception as e:
        pytest.skip(f"no Memgraph at {URL}: {type(e).__name__}: {e}")
    g.ensure_schema()
    yield _ScopedGraph(g)
    g.close()


@pytest.fixture
def version(graph) -> VersionNode:
    """A small document whose ids cannot collide with another test's."""
    sha = secrets.token_hex(32)
    node = VersionNode(
        library="lib_test",
        tenant_id=LEGACY_TENANT_ID,
        source_key=f"libros/{sha[:8]}.pdf",
        content_sha256=sha,
        title="Institución de la religión cristiana",
        author="Juan Calvino",
        fmt="pdf",
        sections=(
            SectionNode(path=(1,), title="Libro I", level=1),
            SectionNode(path=(1, 1), title="Del conocimiento de Dios", level=2),
            SectionNode(path=(2,), title="Libro II", level=1),
        ),
        chunks=(
            ChunkNode(0, "cuerpo", "Casi toda la suma de nuestra sabiduría…",
                      0, 40, section_path=(1, 1), page=17),
            ChunkNode(1, "preguntas", "¿Qué es conocer a Dios?", 40, 63,
                      section_path=(1, 1), page=18),
            ChunkNode(2, "nota", "Cf. Gén. 2:15.", 63, 77,
                      section_path=(2,), page=19),
        ),
    )
    yield node
    _purge(graph, node)


def _purge(graph: Graph, node: VersionNode) -> None:
    """Remove exactly this version's subgraph, and nothing else.

    Concepts are deliberately left behind: they are shared across documents by
    design, so deleting one because a test that mentioned it finished would
    corrupt any other document that also mentions it.
    """
    try:
        graph.write(
            """
            MATCH (d:Document {id: $document_id})
            OPTIONAL MATCH (d)-[:HAS_VERSION]->(v:DocumentVersion)
            OPTIONAL MATCH (v)-[:HAS_SECTION]->(s:Section)
            OPTIONAL MATCH (v)-[:HAS_CHUNK]->(c:Chunk)
            OPTIONAL MATCH (c)-[:CITES]->(cit:Citation)
            OPTIONAL MATCH (cl:Claim)-[:DERIVED_FROM]->(c)
            DETACH DELETE d, v, s, c, cit, cl
            """,
            {"document_id": node.document},
        )
    except GraphError:
        pass  # a test that could not connect has nothing to clean up
