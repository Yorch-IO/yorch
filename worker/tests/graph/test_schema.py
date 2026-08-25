"""Identity rules. Every one of these is a property of the code, not of a corpus."""

from __future__ import annotations

import pytest

from brainworker.graph import schema


def test_version_identity_is_content_not_path():
    """Two paths holding identical bytes must resolve to one version.

    This is the fix for the inherited `doc_id_for()` defect: hashing the path
    made byte-identical duplicates index twice and then compete in ranking.
    """
    sha = "a" * 64
    assert schema.version_id(sha) == schema.version_id(sha)
    assert schema.document_id("lib", "libros/a.pdf") != schema.document_id(
        "lib", "libros/b.pdf"
    )


def test_version_id_rejects_anything_that_is_not_a_digest():
    for bad in ("", "not-a-hash", "A" * 64, "a" * 63, "a" * 65):
        with pytest.raises(ValueError):
            schema.version_id(bad)


def test_document_id_is_library_relative():
    """Moving a library must not orphan its documents, so the key excludes the root."""
    assert schema.document_id("lib1", "a.pdf") != schema.document_id("lib2", "a.pdf")
    assert schema.document_id("lib1", "a.pdf") == schema.document_id("lib1", "a.pdf")


def test_ids_do_not_collide_across_field_boundaries():
    """`("a:b", "c")` and `("a", "b:c")` must not hash the same."""
    assert schema.document_id("a:b", "c") != schema.document_id("a", "b:c")
    assert schema.source_folder_id("a", "b/c") != schema.source_folder_id("a/b", "c")


def test_section_id_keys_on_position_not_title():
    """Titles repeat — "Introducción" appears once per part in this corpus."""
    v = schema.version_id("b" * 64)
    assert schema.section_id(v, (1,)) != schema.section_id(v, (2,))
    assert schema.section_id(v, (1, 2)) != schema.section_id(v, (12,))


def test_chunk_id_is_stable_and_rejects_negative_indices():
    v = schema.version_id("c" * 64)
    assert schema.chunk_id(v, 7) == schema.chunk_id(v, 7)
    assert schema.chunk_id(v, 7) != schema.chunk_id(v, 8)
    with pytest.raises(ValueError):
        schema.chunk_id(v, -1)


@pytest.mark.parametrize(
    "a,b",
    [
        ("Justificación por la fe", "justificacion por la fe"),
        ("JUSTIFICACIÓN POR LA FE", "Justificación  por  la  fe"),
        ("La Gracia, común", "la gracia comun"),
    ],
)
def test_concept_names_converge_on_one_node(a: str, b: str):
    assert schema.concept_id(a) == schema.concept_id(b)


def test_distinct_concepts_stay_distinct():
    assert schema.concept_id("gracia") != schema.concept_id("gracia común")


def test_every_id_matches_the_shape_the_query_validator_accepts():
    """The validator's `id` type is a regex; these two definitions must agree."""
    from brainworker.graph.queries import _ID

    ids = [
        schema.library_id("l"),
        schema.source_folder_id("l", "/p"),
        schema.document_id("l", "a.pdf"),
        schema.version_id("d" * 64),
        schema.section_id("v", (1, 2)),
        schema.chunk_id("v", 0),
        schema.concept_id("gracia"),
        schema.claim_id("chk_x", "texto"),
        schema.citation_id("chk_x", "loc"),
    ]
    for value in ids:
        assert _ID.fullmatch(value), f"{value} is not accepted as a graph id"


def test_schema_statements_cover_every_label():
    for label in schema.LABELS:
        assert any(f":{label}(id)" in s for s in schema.SCHEMA_STATEMENTS)
        assert any(f"(n:{label})" in s for s in schema.SCHEMA_STATEMENTS)


def test_semantic_and_deterministic_edges_do_not_overlap():
    """A relationship type cannot be both — the UI labels provenance from this."""
    assert not (schema.DETERMINISTIC_EDGES & schema.SEMANTIC_EDGES)
