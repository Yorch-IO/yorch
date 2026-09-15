"""Two organisations, the same shape, against a real Memgraph.

**Nothing here goes through the `graph` fixture's convenience wrapper.** That
wrapper fills in the legacy tenant when a test omits one, which is right for the
thirty tests that are about something else and worthless for these: a double
that quietly supplies the thing under test proves nothing. Every query below
names its organisation, and half of them name the *wrong* one on purpose.

The property: an id is not authorization. Ids are salted with the tenant, which
stops two customers colliding — but a tenant id is a value its own members hold,
so a member of A who has the same file as B can *compute* B's `ver_`. What stops
them reading it is the predicate every template now carries, and the validator
refuses to load a template without one.
"""

from __future__ import annotations

import os
import secrets

import pytest

from brainworker.graph import Graph, GraphError
from brainworker.graph import projection as proj
from brainworker.graph.projection import ChunkNode, SectionNode, VersionNode
from brainworker.graph.schema import concept_id as make_concept_id

URL = os.environ.get("BRAIN_MEMGRAPH_URL", "bolt://127.0.0.1:7788")

MINE = "tnt_" + "a" * 24
THEIRS = "tnt_" + "b" * 24
#: The same idea, mentioned by both. Its id differs by tenant, which is half the
#: point; the other half is that neither can read the other's node anyway.
SHARED_NAME = "Providencia"


@pytest.fixture(scope="module")
def raw_graph():
    """The real client, not the scoped wrapper the other suites use."""
    try:
        g = Graph(URL, timeout=5.0)
        g.probe()
    except Exception as e:
        pytest.skip(f"no Memgraph at {URL}: {type(e).__name__}: {e}")
    g.ensure_schema()
    yield g
    g.close()


def _version(tenant: str, library: str) -> VersionNode:
    """A one-section, one-chunk document. Random sha so runs cannot collide."""
    return VersionNode(
        library=library,
        source_key=f"libros/{secrets.token_hex(6)}.pdf",
        content_sha256=secrets.token_hex(32),
        title=f"Libro de {library}",
        author=None,
        fmt="pdf",
        indexed_at="2026-08-26T00:00:00Z",
        tenant_id=tenant,
        sections=(SectionNode(path=(1,), title="Uno", level=1,
                              char_start=0, char_end=10),),
        chunks=(ChunkNode(ordinal=0, kind="cuerpo", text="El texto.",
                          char_start=0, char_end=9, section_path=(1,)),),
    )


@pytest.fixture(scope="module")
def two_tenants(raw_graph):
    lib_mine = f"lib_iso_mine_{secrets.token_hex(4)}"
    lib_theirs = f"lib_iso_theirs_{secrets.token_hex(4)}"
    mine = _version(MINE, lib_mine)
    theirs = _version(THEIRS, lib_theirs)

    built = {}
    for node, tenant in ((mine, MINE), (theirs, THEIRS)):
        proj.project_structure(raw_graph, node)
        proj.activate(raw_graph, node)
        cid = make_concept_id(SHARED_NAME, tenant)
        proj.project_concepts(
            raw_graph,
            [{"name": SHARED_NAME, "type": "doctrina", "descriptions": [f"lectura de {tenant}"]}],
            tenant=tenant,
        )
        built[tenant] = {"node": node, "concept": cid, "library": node.library}

    try:
        yield built
    finally:
        for node in (mine, theirs):
            try:
                raw_graph.write(
                    """
                    MATCH (d:Document {id: $document_id})
                    OPTIONAL MATCH (d)-[:HAS_VERSION]->(v:DocumentVersion)
                    OPTIONAL MATCH (v)-[:HAS_SECTION]->(s:Section)
                    OPTIONAL MATCH (v)-[:HAS_CHUNK]->(c:Chunk)
                    OPTIONAL MATCH (c)-[:CITES]->(cit:Citation)
                    DETACH DELETE d, v, s, c, cit
                    """,
                    {"document_id": node.document},
                )
            except GraphError:
                pass
        # The concepts too: unlike the shared corpus, these are this test's own
        # and salted to tenants nothing else uses.
        for tenant in (MINE, THEIRS):
            try:
                raw_graph.write(
                    "MATCH (k:Concept {id: $id}) DETACH DELETE k",
                    {"id": make_concept_id(SHARED_NAME, tenant)},
                )
            except GraphError:
                pass


def test_the_same_idea_is_two_nodes(two_tenants):
    """Salting is what stops one node carrying two customers' descriptions."""
    assert two_tenants[MINE]["concept"] != two_tenants[THEIRS]["concept"]


def test_an_outline_is_invisible_to_the_other_organisation(raw_graph, two_tenants):
    version = two_tenants[MINE]["node"].version
    mine = raw_graph.query("document_outline", {"version_id": version, "tenant_id": MINE})
    theirs = raw_graph.query("document_outline", {"version_id": version, "tenant_id": THEIRS})
    assert mine, "the owner reads their own outline"
    # The id is genuine and correctly formed. Only the predicate refuses it.
    assert theirs == []


def test_a_computed_id_does_not_help(raw_graph, two_tenants):
    """The attack the salt does *not* stop, and the predicate does.

    A tenant id is not a secret — its own members send it in a header — so a
    member of one organisation who holds the same file as another can derive the
    other's `ver_` exactly. Asking for it with their own tenant returns nothing.
    """
    theirs = two_tenants[THEIRS]["node"]
    from brainworker.graph.schema import version_id

    forged = version_id(theirs.content_sha256, THEIRS)
    assert forged == theirs.version, "the derivation really is reproducible"
    assert raw_graph.query("document_outline", {"version_id": forged, "tenant_id": MINE}) == []


def test_a_concept_lookup_by_name_stays_within_the_organisation(raw_graph, two_tenants):
    """`concept_by_name` matches on the canonical *name*, so the salt buys it
    nothing — this is the template where the predicate is doing all the work."""
    from brainworker.graph.schema import canonical_concept

    rows = raw_graph.query(
        "concept_by_name",
        {"canonical_names": [canonical_concept(SHARED_NAME)], "tenant_id": MINE, "limit": 10},
    )
    ids = {r.data["id"] for r in rows}
    assert two_tenants[MINE]["concept"] in ids
    assert two_tenants[THEIRS]["concept"] not in ids


def test_the_node_counts_are_per_organisation(raw_graph, two_tenants):
    """`/project-summary`'s legs. Unfiltered, this counted the whole database —
    every other customer's corpus included."""
    def counts(tenant: str) -> dict[str, int]:
        return {
            r.data["label"]: r.data["total"]
            for r in raw_graph.query("graph_node_counts", {"tenant_id": tenant})
        }

    mine, theirs = counts(MINE), counts(THEIRS)
    assert mine["DocumentVersion"] == 1 and theirs["DocumentVersion"] == 1
    assert mine["Chunk"] == 1 and theirs["Chunk"] == 1
    # And neither sees the real corpus sharing the same database.
    assert mine["Document"] == 1


def test_a_library_overview_is_its_organisations_own(raw_graph, two_tenants):
    rows = raw_graph.query(
        "library_documents",
        {"library_id": two_tenants[THEIRS]["library"], "tenant_id": MINE, "document_limit": 100},
    )
    assert rows == [], "naming another organisation's library returns nothing"
