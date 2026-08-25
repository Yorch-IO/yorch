"""Removing a version and a document from the graph, against a real Memgraph.

**Concept names are randomised, and that is not decoration.** `test_library_scope`
learned it the expensive way: its first draft used the literal name "Felicidad"
and its own precondition failed, because `concept_id` is
`concept_id(canonical_name)` with no library and no test marker in it — the name
already existed in this machine's real library from an earlier extraction run. A
test that hard-codes a concept name is sharing a node with production data, and
here that would mean asserting about the deletion of something a user owns.

Nothing in this module deletes anything it did not create.
"""

from __future__ import annotations

import secrets

import pytest

from brainworker.graph.projection import (
    ChunkNode,
    SectionNode,
    SemanticEdge,
    VersionNode,
    project_claims,
    project_concepts,
    project_semantic_edges,
    project_structure,
    remove_document,
    remove_version,
)
from brainworker.graph.schema import chunk_id, claim_id, concept_id


def _version(title: str, *, library: str = "lib_removal_test") -> VersionNode:
    sha = secrets.token_hex(32)
    return VersionNode(
        library=library,
        source_key=f"libros/{sha[:8]}.pdf",
        content_sha256=sha,
        title=title,
        author="Anónimo",
        fmt="pdf",
        sections=(
            SectionNode(path=(1,), title="Parte primera", level=1),
            SectionNode(path=(1, 1), title="Capítulo I", level=2),
        ),
        chunks=(
            ChunkNode(0, "cuerpo", "El primer párrafo del documento.", 0, 32,
                      section_path=(1, 1), page=1),
            ChunkNode(1, "nota", "Cf. Gén. 2:15.", 32, 46,
                      section_path=(1, 1), page=1),
        ),
    )


def _mention(graph, version: VersionNode, concept_name: str) -> str:
    """Project one concept, one claim and their edges onto this version's chunk 0."""
    chunk = chunk_id(version.version, 0)
    project_concepts(graph, [{"name": concept_name, "type": "tema"}])
    project_claims(
        graph,
        [{"text": f"Una afirmación sobre {concept_name}.",
          "confidence": 0.9, "source_chunk_id": chunk}],
    )
    project_semantic_edges(
        graph,
        [
            SemanticEdge(
                type="MENTIONS", source_id=chunk,
                target_id=concept_id(concept_name), confidence=0.9,
                extractor_model="test", source_chunk_id=chunk,
            ),
            SemanticEdge(
                type="ABOUT",
                source_id=claim_id(chunk, f"Una afirmación sobre {concept_name}."),
                target_id=concept_id(concept_name), confidence=0.9,
                extractor_model="test", source_chunk_id=chunk,
            ),
        ],
    )
    _CREATED.append(concept_id(concept_name))
    return concept_id(concept_name)


#: Concepts this module invented, drained after every test.
#:
#: `conftest._purge` deliberately leaves concepts behind, and for a real one
#: that is right: they are shared across documents by design, so deleting one
#: because the test that mentioned it finished would corrupt another document.
#: These are different in kind — every name carries a random hex suffix, so
#: nothing else can ever reach them — which makes leaving them pure residue.
#:
#: Drained unconditionally rather than at the end of each test body, because the
#: leak happens precisely when a test *fails* between creating a concept and
#: removing it. Fifty-five of them were found accumulated in a developer's live
#: graph, all from failing iterations of this file.
_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _drain_concepts(graph):
    yield
    while _CREATED:
        graph.write(
            "MATCH (k:Concept {id: $id}) DETACH DELETE k", {"id": _CREATED.pop()}
        )


def _count(graph, label: str, node_id: str) -> int:
    rows = graph.write(
        f"MATCH (n:{label} {{id: $id}}) RETURN count(n) AS n", {"id": node_id}
    )
    return int(rows[0]["n"]) if rows else 0


def _chunks_of(graph, version_id: str) -> int:
    rows = graph.write(
        "MATCH (:DocumentVersion {id: $v})-[:HAS_CHUNK]->(c:Chunk) RETURN count(c) AS n",
        {"v": version_id},
    )
    return int(rows[0]["n"]) if rows else 0


@pytest.fixture
def one(graph):
    """A projected version, removed by the test itself or by the teardown."""
    node = _version("Catecismo de prueba")
    project_structure(graph, node)
    yield node
    remove_document(graph, node.document)


def test_removing_a_version_takes_its_whole_subgraph(graph, one):
    concept = _mention(graph, one, f"Concepto {secrets.token_hex(6)}")

    assert _chunks_of(graph, one.version) == 2

    removed = remove_version(graph, one.version)

    assert removed.versions == 1
    assert removed.chunks == 2
    assert removed.sections == 2
    assert removed.claims == 1
    assert _count(graph, "DocumentVersion", one.version) == 0
    assert _chunks_of(graph, one.version) == 0
    # The last version mentioning it is gone, so the concept is collected.
    assert removed.concepts_collected == 1
    assert _count(graph, "Concept", concept) == 0


def test_a_shared_concept_survives_while_another_version_mentions_it(graph, one):
    """Concepts are merged by canonical name on purpose — that sharing is what
    makes `related_documents` work — so removing one of two mentioning versions
    must leave the node standing."""
    name = f"Concepto {secrets.token_hex(6)}"
    concept = _mention(graph, one, name)

    other = _version("Otro tratado")
    project_structure(graph, other)
    _mention(graph, other, name)
    try:
        removed = remove_version(graph, one.version)

        assert removed.concepts_collected == 0
        assert _count(graph, "Concept", concept) == 1
        # And the other version is untouched.
        assert _chunks_of(graph, other.version) == 2

        # Now the last mention goes, and only now is it collected.
        removed_last = remove_version(graph, other.version)
        assert removed_last.concepts_collected == 1
        assert _count(graph, "Concept", concept) == 0
    finally:
        remove_document(graph, other.document)


def test_a_version_two_documents_hold_survives_removing_either(graph):
    """Byte-identical files at two paths share one DocumentVersion — the fix for
    `doc_id_for()` hashing the path. Deleting one document must leave the version
    standing with only its edge to that document gone."""
    first = _version("Mismo contenido, ruta A")
    project_structure(graph, first)
    # Same sha256, different source_key: one version, two documents.
    second = VersionNode(
        library=first.library,
        source_key="libros/copia.pdf",
        content_sha256=first.content_sha256,
        title=first.title,
        author=first.author,
        fmt=first.fmt,
        sections=first.sections,
        chunks=first.chunks,
    )
    project_structure(graph, second)
    assert first.version == second.version

    try:
        removed = remove_document(graph, first.document)

        assert removed.documents == 1
        assert removed.versions == 0, "the version is still held by the other document"
        assert _count(graph, "Document", first.document) == 0
        assert _count(graph, "DocumentVersion", first.version) == 1
        assert _chunks_of(graph, first.version) == 2
    finally:
        remove_document(graph, second.document)

    assert _count(graph, "DocumentVersion", first.version) == 0


def test_removing_an_unknown_version_is_not_an_error(graph):
    """Removal is retried after a partial failure, so the second pass finds
    nothing and must say so rather than raising."""
    removed = remove_version(graph, "ver_" + secrets.token_hex(12))
    assert removed.versions == 0
    assert removed.chunks == 0


# ---------------------------------------------------------------------------
# Re-projection converges
# ---------------------------------------------------------------------------


def test_reprojecting_under_a_changed_title_does_not_duplicate_citations(graph, one):
    """Found by rebuilding, not by reading.

    `citation_id` is keyed on `(chunk, locator)` and the locator embeds the
    document title, so a re-projection under a different title used to mint a
    *second* Citation per chunk — and the stale one still answered
    `citations_for_chunks`, which is the query an answer's evidence comes from.
    A duplicate citation is not cosmetic: it is a second, unreachable pointer
    into the same passage.
    """

    def citations() -> int:
        rows = graph.write(
            "MATCH (:DocumentVersion {id: $v})-[:HAS_CHUNK]->(:Chunk)-[:CITES]->(c:Citation) "
            "RETURN count(DISTINCT c) AS n",
            {"v": one.version},
        )
        return int(rows[0]["n"])

    first = citations()
    assert first == len(one.chunks)

    renamed = VersionNode(
        library=one.library,
        source_key=one.source_key,
        content_sha256=one.content_sha256,
        title="Otro título entirely",
        author=one.author,
        fmt=one.fmt,
        sections=one.sections,
        chunks=one.chunks,
    )
    project_structure(graph, renamed)

    assert citations() == first, "re-projection duplicated the citations"

    # And the surviving citation is the current one, not the stale one.
    rows = graph.write(
        "MATCH (:DocumentVersion {id: $v})-[:HAS_CHUNK]->(:Chunk)-[:CITES]->(c:Citation) "
        "RETURN c.locator AS locator",
        {"v": one.version},
    )
    assert all("Otro título" in str(r["locator"]) for r in rows), [
        r["locator"] for r in rows
    ]


def test_a_concept_reachable_only_through_a_claim_is_collected_too(graph, one):
    """`extract_semantics` names a concept two ways, and only one of them is a
    `MENTIONS` edge from a chunk.

    A claim's subject — `concepts.setdefault(about, …)` — is attached by `ABOUT`
    from the Claim and may have no `MENTIONS` edge at all. Collecting on
    `MENTIONS` alone left every such concept orphaned after its document was
    removed, which is how four of them were found sitting in a live graph.
    """
    name = f"Reclamado {secrets.token_hex(6)}"
    chunk = chunk_id(one.version, 0)
    text = f"Una afirmación sobre {name}."
    project_concepts(graph, [{"name": name, "type": "tema"}])
    _CREATED.append(concept_id(name))
    project_claims(
        graph, [{"text": text, "confidence": 0.9, "source_chunk_id": chunk}]
    )
    # ABOUT only — deliberately no MENTIONS, which is exactly what the extractor
    # produces for a concept that appears solely as a claim's subject.
    project_semantic_edges(
        graph,
        [
            SemanticEdge(
                type="ABOUT", source_id=claim_id(chunk, text),
                target_id=concept_id(name), confidence=0.9,
                extractor_model="test", source_chunk_id=chunk,
            )
        ],
    )
    assert _count(graph, "Concept", concept_id(name)) == 1

    removed = remove_version(graph, one.version)

    assert removed.concepts_collected == 1
    assert _count(graph, "Concept", concept_id(name)) == 0


def test_a_concept_reachable_only_through_a_relation_is_collected_too(graph, one):
    """The same hole one level further out, closed before it could be dug.

    A claim's *second* concept — the one it `INVOLVES` — reaches the graph the
    same way its subject does: `concepts.setdefault(related, …)` with no
    `MENTIONS` edge behind it. Collecting on `MENTIONS` and `ABOUT` alone would
    have left it orphaned after its document was removed, which is exactly what
    happened to claim-only concepts before `ABOUT` was added to the candidates.
    """
    name = f"Relacionado {secrets.token_hex(6)}"
    chunk = chunk_id(one.version, 0)
    text = f"Una afirmación que relaciona algo con {name}."
    project_concepts(graph, [{"name": name}])
    _CREATED.append(concept_id(name))
    project_claims(
        graph, [{"text": text, "confidence": 0.9, "source_chunk_id": chunk}]
    )
    # INVOLVES only: no MENTIONS, and not even the ABOUT that the previous test
    # relies on. This is the sole edge tying the concept to the document.
    project_semantic_edges(
        graph,
        [
            SemanticEdge(
                type="INVOLVES", source_id=claim_id(chunk, text),
                target_id=concept_id(name), confidence=0.9,
                extractor_model="test", source_chunk_id=chunk,
            )
        ],
    )
    assert _count(graph, "Concept", concept_id(name)) == 1

    removed = remove_version(graph, one.version)

    assert removed.concepts_collected == 1
    assert _count(graph, "Concept", concept_id(name)) == 0


def test_a_concept_both_mentioned_and_claimed_is_counted_once(graph, one):
    """The candidate list concatenates two `collect(DISTINCT …)` results, so a
    concept reached both ways appears twice. Uncorrected it doubled the number
    reported to the user — and a removal report that overstates what it deleted
    is worse than one that says nothing."""
    concept = _mention(graph, one, f"Ambos {secrets.token_hex(6)}")

    removed = remove_version(graph, one.version)

    assert removed.concepts_collected == 1
    assert _count(graph, "Concept", concept) == 0
