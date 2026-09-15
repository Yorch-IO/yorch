"""Identity rules. Every one of these is a property of the code, not of a corpus."""

from __future__ import annotations

import re
import pytest

from brainworker.graph import schema


def test_version_identity_is_content_not_path():
    """Two paths holding identical bytes must resolve to one version.

    This is the fix for the inherited `doc_id_for()` defect: hashing the path
    made byte-identical duplicates index twice and then compete in ranking.
    """
    sha = "a" * 64
    assert schema.version_id(sha, schema.LEGACY_TENANT_ID) == schema.version_id(sha, schema.LEGACY_TENANT_ID)
    assert schema.document_id("lib", "libros/a.pdf") != schema.document_id(
        "lib", "libros/b.pdf"
    )


def test_version_id_rejects_anything_that_is_not_a_digest():
    for bad in ("", "not-a-hash", "A" * 64, "a" * 63, "a" * 65):
        with pytest.raises(ValueError):
            schema.version_id(bad, schema.LEGACY_TENANT_ID)


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
    v = schema.version_id("b" * 64, schema.LEGACY_TENANT_ID)
    assert schema.section_id(v, (1,)) != schema.section_id(v, (2,))
    assert schema.section_id(v, (1, 2)) != schema.section_id(v, (12,))


def test_chunk_id_is_stable_and_rejects_negative_indices():
    v = schema.version_id("c" * 64, schema.LEGACY_TENANT_ID)
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
    assert schema.concept_id(a, schema.LEGACY_TENANT_ID) == schema.concept_id(b, schema.LEGACY_TENANT_ID)


def test_distinct_concepts_stay_distinct():
    assert schema.concept_id("gracia", schema.LEGACY_TENANT_ID) != schema.concept_id("gracia común", schema.LEGACY_TENANT_ID)


def test_every_id_matches_the_shape_the_query_validator_accepts():
    """The validator's `id` type is a regex; these two definitions must agree."""
    from brainworker.graph.queries import _ID

    ids = [
        schema.library_id("l"),
        schema.source_folder_id("l", "/p"),
        schema.document_id("l", "a.pdf"),
        schema.version_id("d" * 64, schema.LEGACY_TENANT_ID),
        schema.section_id("v", (1, 2)),
        schema.chunk_id("v", 0),
        schema.concept_id("gracia", schema.LEGACY_TENANT_ID),
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


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


def test_the_legacy_tenants_ids_are_exactly_what_they_were():
    """The whole reason the salt is conditional.

    Salting unconditionally would have changed every `ver_`, `sec_`, `chk_`,
    `clm_` and `cit_` in the graph and every point id in Qdrant at once — and
    the projections can only be rebuilt from artifacts. Measured on this
    installation 2026-08-26: 69 indexed versions, all 69 with a `chunks`
    artifact *recorded*, and only **31** with the file still on disk. This test
    is what keeps somebody from "tidying up" the branch and stranding 38 books.
    """
    sha = "a" * 64
    assert schema.version_id(sha, schema.LEGACY_TENANT_ID) == f"ver_{sha[:24]}"
    # And the concept id is the plain digest of the canonical name, as before.
    assert schema.concept_id("Dios", schema.LEGACY_TENANT_ID) == (
        f"con_{schema._digest(schema.canonical_concept('Dios'))}"
    )


def test_two_tenants_holding_the_same_bytes_get_different_versions():
    """`document_version.id` is a primary key.

    Before the salt, two customers importing the same PDF computed the same id
    and the second insert collided on the primary key — *before* the widened
    `UNIQUE (tenant_id, content_sha256)` could ever be reached, which made that
    constraint unreachable in practice.
    """
    sha = "b" * 64
    a = schema.version_id(sha, "tnt_" + "a" * 24)
    b = schema.version_id(sha, "tnt_" + "b" * 24)
    assert a != b
    assert a != schema.version_id(sha, schema.LEGACY_TENANT_ID)


def test_two_tenants_talking_about_the_same_idea_get_different_concepts():
    """Sharing a concept across documents is the feature; across tenants it is a
    leak. `Concept.description_raw` accumulates a description per chunk that
    mentions it, so one node would carry two customers' text — and filtering the
    traversal cannot fix that, because the content is already on the node."""
    a = schema.concept_id("Justificación por la fe", "tnt_" + "a" * 24)
    b = schema.concept_id("Justificación por la fe", "tnt_" + "b" * 24)
    assert a != b
    # Within one tenant the convergence still holds: that is what makes
    # traversal worth having.
    assert a == schema.concept_id("justificacion por la fe", "tnt_" + "a" * 24)


def test_a_salted_id_still_has_the_shape_the_query_binder_accepts():
    """`bind` refuses anything that is not `<prefix>_[0-9a-f]{24}`, so a salted
    id that came out longer would be rejected at the door of every template."""
    import re

    pattern = re.compile(r"(lib|fld|doc|ver|sec|chk|con|clm|cit)_[0-9a-f]{24}")
    other = "tnt_" + "c" * 24
    for value in (
        schema.version_id("d" * 64, other),
        schema.concept_id("Gracia", other),
        schema.chunk_id(schema.version_id("d" * 64, other), 3),
        schema.section_id(schema.version_id("d" * 64, other), (1, 2)),
    ):
        assert pattern.fullmatch(value), value


def test_a_tenant_id_is_derived_from_its_slug_and_is_stable():
    """Seeding the same organisation twice must not mint two of them."""
    once = schema.tenant_id("acme")
    assert once == schema.tenant_id("acme")
    assert once != schema.tenant_id("acme-inc")
    assert re.fullmatch(r"tnt_[0-9a-f]{24}", once)


def test_no_slug_derives_the_legacy_tenant_id():
    """The legacy id is a literal from before this function, and `_salt`
    recognises it *by value* to keep 69 versions' ids unchanged. A slug that
    happened to derive it would silently unsalt that tenant."""
    assert schema.LEGACY_TENANT_ID == "tnt_000000000000000000000001"
    # Its digest half is not hexadecimal-random but a hand-written counter, and
    # blake2s cannot be asked to produce it; the assertion that matters is that
    # the derivation's shape and this literal are distinguishable at all.
    for slug in ("legacy", "default", "", "0", "acme"):
        assert schema.tenant_id(slug) != schema.LEGACY_TENANT_ID
