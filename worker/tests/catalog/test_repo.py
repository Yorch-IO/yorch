"""Catalog behaviour against a real Postgres, in a disposable schema."""

from __future__ import annotations

import pytest

from brainworker.graph.schema import LEGACY_TENANT_ID
from brainworker.catalog import Catalog, CatalogError, LibraryOwnedByAnother
from brainworker.catalog import migrations as m


@pytest.fixture
def catalog(database_url: str):
    m.apply_migrations(database_url)
    with Catalog(database_url, min_size=1, max_size=2) as c:
        c.ensure_library("lib_1", "Biblioteca", tenant_id=LEGACY_TENANT_ID)
        yield c


def _document(c: Catalog, key: str = "libros/calvino.pdf", doc: str = "doc_1") -> str:
    return c.upsert_document(
        tenant_id=LEGACY_TENANT_ID,
        document_id=doc,
        library_id="lib_1",
        source_key=key,
        title="Institución",
        fmt="pdf",
    )


def test_registering_new_content_creates_a_version(catalog: Catalog):
    _document(catalog)
    version, created = catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_1", document_id="doc_1", content_sha256="a" * 64, byte_size=10
    )
    assert created is True
    assert version.id == "ver_1" and version.state == "pending"


def test_a_second_path_with_identical_bytes_links_instead_of_re_indexing(
    catalog: Catalog,
):
    """The duplicate-file fix at the catalog layer.

    `libros/` and `libros/done/` hold byte-identical copies of nine documents in
    this corpus. Both must resolve to one version, or they compete in ranking.
    """
    _document(catalog, "libros/a.pdf", "doc_a")
    _document(catalog, "libros/done/a.pdf", "doc_b")

    first, created_first = catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_a", document_id="doc_a", content_sha256="b" * 64, byte_size=10
    )
    second, created_second = catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_b", document_id="doc_b", content_sha256="b" * 64, byte_size=10
    )

    assert created_first is True and created_second is False
    assert second.id == first.id == "ver_a", "the second id must not win"


def test_registering_the_same_pair_twice_is_idempotent(catalog: Catalog):
    """Temporal retries activities; a retry must not raise on the link insert."""
    _document(catalog)
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_1", document_id="doc_1", content_sha256="c" * 64, byte_size=1
    )
    _, created = catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_1", document_id="doc_1", content_sha256="c" * 64, byte_size=1
    )
    assert created is False


def test_a_version_is_not_active_until_activation(catalog: Catalog):
    _document(catalog)
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_1", document_id="doc_1", content_sha256="d" * 64, byte_size=1
    )
    assert catalog.active_version("doc_1") is None

    catalog.activate("doc_1", "ver_1")
    assert catalog.active_version("doc_1") == "ver_1"
    assert catalog.version_by_content("d" * 64).state == "indexed"


def test_activating_a_newer_version_replaces_the_old_one(catalog: Catalog):
    _document(catalog)
    for vid, sha in (("ver_1", "e" * 64), ("ver_2", "f" * 64)):
        catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
            version_id=vid, document_id="doc_1", content_sha256=sha, byte_size=1
        )
    catalog.activate("doc_1", "ver_1")
    catalog.activate("doc_1", "ver_2")
    assert catalog.active_version("doc_1") == "ver_2"
    # The old version stays readable, so a citation already shown still resolves.
    assert catalog.version_by_content("e" * 64) is not None


def test_an_unknown_state_is_refused_before_it_reaches_the_constraint(catalog: Catalog):
    _document(catalog)
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_1", document_id="doc_1", content_sha256="1" * 64, byte_size=1
    )
    with pytest.raises(CatalogError, match="unknown version state"):
        catalog.set_version_state("ver_1", "finished")


def test_a_missing_file_keeps_its_history(catalog: Catalog):
    _document(catalog)
    catalog.mark_absent("doc_1")
    assert catalog.documents("lib_1") == []
    absent = catalog.documents("lib_1", include_absent=True)
    assert len(absent) == 1 and absent[0].present is False
    assert absent[0].absent_since is not None


def test_re_importing_a_file_that_had_gone_missing_brings_it_back(catalog: Catalog):
    """A folder unmounted for a minute must not need manual repair."""
    _document(catalog)
    catalog.mark_absent("doc_1")
    _document(catalog)
    assert [d.id for d in catalog.documents("lib_1")] == ["doc_1"]


def test_costs_accumulate_rather_than_overwrite(catalog: Catalog):
    """A retried activity spent its tokens whether or not the attempt succeeded."""
    catalog.start_run(tenant_id=LEGACY_TENANT_ID, run_id="run_1", workflow_id="wf-1", kind="index")
    catalog.record_cost(
        "run_1", stage="correction", provider="vertex", model="gemini-2.5-flash",
        input_tokens=1000, output_tokens=500, usd=0.0334,
    )
    catalog.record_cost(
        "run_1", stage="correction", provider="vertex", model="gemini-2.5-flash",
        input_tokens=1000, output_tokens=500, usd=0.0334,
    )
    assert len(catalog.costs("run_1")) == 2
    total = catalog.total_cost("run_1")
    assert total["input_tokens"] == 2000
    assert total["usd"] == pytest.approx(0.0668)


def test_an_unpriced_charge_is_counted_but_not_folded_into_the_total(catalog: Catalog):
    """Otherwise the UI shows a confident total that silently excludes it."""
    catalog.start_run(tenant_id=LEGACY_TENANT_ID, run_id="run_1", workflow_id="wf-1", kind="index")
    catalog.record_cost(
        "run_1", stage="embedding", provider="vertex", model="gemini-embedding-001",
        input_tokens=100, usd=0.0033,
    )
    catalog.record_cost(
        "run_1", stage="extraction", provider="vertex", model="unreleased-model",
        input_tokens=50, usd=None,
    )
    total = catalog.total_cost("run_1")
    assert total["usd"] == pytest.approx(0.0033)
    assert total["unpriced_entries"] == 1
    assert total["input_tokens"] == 150


def test_artifacts_are_recorded_by_reference_and_overwritten_on_retry(catalog: Catalog):
    catalog.start_run(tenant_id=LEGACY_TENANT_ID, run_id="run_1", workflow_id="wf-1", kind="index")
    catalog.record_artifact(
        "run_1", name="chunks", rel_path="runs/run_1/chunks.jsonl",
        sha256="a" * 64, size_bytes=10,
    )
    catalog.record_artifact(
        "run_1", name="chunks", rel_path="runs/run_1/chunks.jsonl",
        sha256="b" * 64, size_bytes=20,
    )
    rows = catalog.artifacts("run_1")
    assert len(rows) == 1 and rows[0]["sha256"] == "b" * 64


def test_a_profile_collision_is_recorded_for_a_person_to_judge(catalog: Catalog):
    """Retrieval metrics cannot see this defect, so the gate has to show it."""
    _document(catalog)
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_1", document_id="doc_1", content_sha256="2" * 64, byte_size=1
    )
    catalog.record_profile_warning(
        version_id="ver_1", profile_id="prof_teologia", collides_with="prof_actas",
        similarity=0.94, detail="same heading fingerprint, unrelated subject matter",
    )
    open_warnings = catalog.open_profile_warnings("ver_1")
    assert len(open_warnings) == 1 and open_warnings[0]["similarity"] == 0.94

    catalog.acknowledge_profile_warning(open_warnings[0]["id"])
    assert catalog.open_profile_warnings("ver_1") == []


def test_a_run_records_where_it_failed(catalog: Catalog):
    catalog.start_run(tenant_id=LEGACY_TENANT_ID, run_id="run_1", workflow_id="wf-1", kind="index")
    catalog.set_run_stage("run_1", "correction")
    catalog.finish_run(
        "run_1", "failed", error_kind="provider_quota", error_detail="429 exhausted",
    )
    with catalog._conn() as conn:
        row = conn.execute(
            "SELECT state, stage, error_kind, finished_at FROM run WHERE id = 'run_1'"
        ).fetchone()
    assert row[0] == "failed" and row[1] == "correction" and row[2] == "provider_quota"
    assert row[3] is not None


def test_a_run_can_start_before_its_version_exists(catalog: Catalog):
    """`run.version_id` is a foreign key, so an ingest that named the version up
    front failed with a ForeignKeyViolation that pointed nowhere near ordering.
    The run is created bare and the version attached once it is written."""
    _document(catalog)
    catalog.start_run(
        tenant_id=LEGACY_TENANT_ID,
        run_id="run_1", workflow_id="wf-1", kind="index", document_id="doc_1"
    )
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_1", document_id="doc_1", content_sha256="3" * 64, byte_size=1
    )
    catalog.attach_version("run_1", "doc_1", "ver_1")
    with catalog._conn() as conn:
        row = conn.execute("SELECT version_id FROM run WHERE id = 'run_1'").fetchone()
    assert row[0] == "ver_1"


def test_a_duplicate_path_becomes_answerable_too(catalog: Catalog):
    """A second path to already-indexed content must be active, not pending.

    The ingest short-circuit returns before the activation step, so a duplicate
    was linked to a fully indexed version and still reported "not yet active"
    forever — a second copy in the Library that never became answerable.
    """
    _document(catalog, "libros/a.pdf", "doc_a")
    _document(catalog, "copias/a.pdf", "doc_b")
    sha = "9" * 64

    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_x", document_id="doc_a", content_sha256=sha, byte_size=1
    )
    catalog.activate("doc_a", "ver_x")

    version, created = catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_ignored", document_id="doc_b", content_sha256=sha, byte_size=1
    )
    assert created is False and version.id == "ver_x"

    catalog.activate("doc_b", version.id)
    assert catalog.active_version("doc_b") == "ver_x"
    assert catalog.active_version("doc_a") == "ver_x", "the first is untouched"


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------
#
# Permanent removal is the explicit user act the plan asks for, and the cases
# below are the ones where the obvious implementation is wrong: a version two
# documents hold, an `ON DELETE RESTRICT` that blocks the delete, and cost
# history that must survive the thing it was spent on.


def test_a_version_two_documents_hold_survives_removing_either(catalog: Catalog):
    """The many-to-many link table exists precisely for this. Byte-identical
    files at two paths are two document slots and one version; deleting one slot
    must not destroy the bytes the other still points at."""
    _document(catalog, "libros/a.pdf", "doc_a")
    _document(catalog, "libros/done/a.pdf", "doc_b")
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_a", document_id="doc_a", content_sha256="c" * 64, byte_size=10
    )
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_a", document_id="doc_b", content_sha256="c" * 64, byte_size=10
    )
    assert catalog.documents_holding("ver_a") == ["doc_a", "doc_b"]

    removed = catalog.remove_document("doc_a", library_id="lib_1")

    assert removed == {"documents": 1, "versions": 0}
    assert catalog.documents_holding("ver_a") == ["doc_b"]
    assert catalog.versions_of("doc_b")[0].id == "ver_a"

    # And the last holder takes it with them.
    assert catalog.remove_document("doc_b", library_id="lib_1") == {
        "documents": 1,
        "versions": 1,
    }
    assert catalog.documents_holding("ver_a") == []


def test_removing_a_document_clears_the_active_version_pointer(catalog: Catalog):
    """`document_active_version.version_id` is ON DELETE RESTRICT, so a naive
    delete raises instead of removing. The constraint is right — "which version
    does this citation refer to" must have one answer even after a crash — so
    removal has to clear the pointer itself."""
    _document(catalog)
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_1", document_id="doc_1", content_sha256="d" * 64, byte_size=10
    )
    catalog.activate("doc_1", "ver_1")
    assert catalog.active_version("doc_1") == "ver_1"

    assert catalog.remove_document("doc_1", library_id="lib_1")["versions"] == 1
    assert catalog.document("doc_1") is None


def test_removing_a_version_directly_clears_its_pointer_too(catalog: Catalog):
    _document(catalog)
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_1", document_id="doc_1", content_sha256="e" * 64, byte_size=10
    )
    catalog.activate("doc_1", "ver_1")

    removed = catalog.remove_version("ver_1")

    assert removed["versions"] == 1
    assert removed["active_pointers_cleared"] == 1
    assert catalog.active_version("doc_1") is None
    assert catalog.versions_of("doc_1") == []
    # The document itself is untouched: removing a version is not removing a book.
    assert catalog.document("doc_1") is not None


def test_run_history_and_cost_survive_the_document_they_were_spent_on(
    catalog: Catalog,
):
    """Both foreign keys are ON DELETE SET NULL deliberately. What a document
    cost is a fact about money, and it outlives the document."""
    _document(catalog)
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_1", document_id="doc_1", content_sha256="f" * 64, byte_size=10
    )
    catalog.start_run(tenant_id=LEGACY_TENANT_ID, run_id="run_1", workflow_id="wf_1", kind="index",
                      document_id="doc_1")
    catalog.attach_version("run_1", "doc_1", "ver_1")
    catalog.record_cost(
        run_id="run_1", stage="correction", provider="vertex",
        model="gemini-3.6-flash", input_tokens=100, output_tokens=200, usd=0.01,
    )

    catalog.remove_document("doc_1", library_id="lib_1")

    costs = catalog.costs("run_1")
    # `float(...)` because `usd` is a numeric column and comes back as Decimal,
    # while `Cost.usd` is annotated `float | None`. Harmless everywhere it is
    # serialised; worth knowing before comparing one.
    assert len(costs) == 1 and float(costs[0].usd) == pytest.approx(0.01)


def test_removal_refuses_a_document_in_another_library(catalog: Catalog):
    """Document ids derive from the library, but an id arriving over HTTP is a
    string. The check is what makes a mistyped one a 404 rather than a deletion
    in somebody else's library."""
    _document(catalog)
    catalog.ensure_library("lib_2", "Otra", tenant_id=LEGACY_TENANT_ID)

    assert catalog.remove_document("doc_1", library_id="lib_2") == {
        "documents": 0,
        "versions": 0,
    }
    assert catalog.document("doc_1") is not None
    assert catalog.document("doc_1", library_id="lib_2") is None


def test_removing_an_absent_document_is_not_an_error(catalog: Catalog):
    """Removal is retried after a partial failure, so a second pass finding
    nothing is the normal case."""
    assert catalog.remove_document("doc_missing") == {"documents": 0, "versions": 0}
    assert catalog.remove_version("ver_missing")["versions"] == 0


def test_the_source_path_is_recorded_and_not_erased_by_a_later_import(
    catalog: Catalog,
):
    """Re-index reads this. `COALESCE` on update, for the same reason `author`
    has it: a caller that does not know the path must not blank one."""
    catalog.upsert_document(
        tenant_id=LEGACY_TENANT_ID,
        document_id="doc_1", library_id="lib_1", source_key="libros/calvino.pdf",
        title="Institución", fmt="pdf",
        source_path="/workspace/inbox/libros/calvino.pdf",
    )
    assert catalog.document("doc_1").source_path == "/workspace/inbox/libros/calvino.pdf"

    _document(catalog)  # a second import that says nothing about the path
    assert catalog.document("doc_1").source_path == "/workspace/inbox/libros/calvino.pdf"


def test_a_document_imported_before_the_migration_has_no_path(catalog: Catalog):
    """Null is meaningful: re-index refuses by name rather than guessing."""
    _document(catalog)
    assert catalog.document("doc_1").source_path is None
    assert catalog.documents("lib_1")[0].source_path is None


# -- project-wide totals ---------------------------------------------------


def test_project_totals_count_a_shared_version_once(catalog):
    """Two paths, identical bytes: one version, two documents. A total that
    joined through `document_version_link` would report two."""
    catalog.ensure_library("lib_totals", "Totales", tenant_id=LEGACY_TENANT_ID, language="es")
    first = catalog.upsert_document(
        tenant_id=LEGACY_TENANT_ID,
        document_id="doc_t1", library_id="lib_totals",
        source_key="a.pdf", title="A", fmt="pdf",
    )
    second = catalog.upsert_document(
        tenant_id=LEGACY_TENANT_ID,
        document_id="doc_t2", library_id="lib_totals",
        source_key="b.pdf", title="B", fmt="pdf",
    )
    sha = "f" * 64
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_totals", document_id=first,
        content_sha256=sha, byte_size=1000,
    )
    # A different id for the same bytes: the second call links rather than
    # creating, which is the case a naive join would double-count.
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_totals_b", document_id=second,
        content_sha256=sha, byte_size=1000,
    )
    totals = catalog.project_totals(tenant_id=LEGACY_TENANT_ID)
    assert totals.documents == 2
    # One version, not two — the whole point.
    assert totals.indexed_versions == 0  # nothing activated yet
    assert totals.active_versions == 0
    catalog.activate("doc_t1", "ver_totals")
    catalog.activate("doc_t2", "ver_totals")
    after = catalog.project_totals(tenant_id=LEGACY_TENANT_ID)
    assert after.indexed_versions == 1
    assert after.indexed_bytes == 1000
    assert after.active_versions == 2  # two shelf entries, one set of bytes


def test_project_totals_count_only_what_was_activated(catalog):
    """`indexed` is what can be retrieved from. A registered-but-unapproved
    version is not content the project holds."""
    catalog.ensure_library("lib_totals2", "Totales", tenant_id=LEGACY_TENANT_ID, language="es")
    doc = catalog.upsert_document(
        tenant_id=LEGACY_TENANT_ID,
        document_id="doc_t3", library_id="lib_totals2",
        source_key="c.pdf", title="C", fmt="pdf",
    )
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_totals2", document_id=doc,
        content_sha256="e" * 64, byte_size=2048,
    )
    assert catalog.project_totals(tenant_id=LEGACY_TENANT_ID).indexed_versions == 0
    catalog.activate("doc_t3", "ver_totals2")
    totals = catalog.project_totals(tenant_id=LEGACY_TENANT_ID)
    assert totals.indexed_versions == 1
    assert totals.active_versions == 1
    assert totals.indexed_bytes == 2048


def test_pages_are_measured_as_absent_rather_than_assumed(catalog):
    """Nothing writes `page_count` — `register_version` runs before extraction.
    The summary reports how many rows record one so the figure starts working
    by itself if that ever changes, instead of staying a hardcoded absence."""
    catalog.ensure_library("lib_pages", "Páginas", tenant_id=LEGACY_TENANT_ID, language="es")
    doc = catalog.upsert_document(
        tenant_id=LEGACY_TENANT_ID,
        document_id="doc_p1", library_id="lib_pages",
        source_key="p.pdf", title="P", fmt="pdf",
    )
    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_pages", document_id=doc,
        content_sha256="d" * 64, byte_size=10,
    )
    assert catalog.project_totals(tenant_id=LEGACY_TENANT_ID).versions_with_pages == 0

    catalog.register_version(
        tenant_id=LEGACY_TENANT_ID,
        version_id="ver_pages2", document_id=doc,
        content_sha256="c" * 64, byte_size=10, page_count=175,
    )
    totals = catalog.project_totals(tenant_id=LEGACY_TENANT_ID)
    assert totals.versions_with_pages == 1
    assert totals.pages == 175


def test_recent_runs_are_newest_first_and_bounded(catalog):
    catalog.ensure_library("lib_runs", "Runs", tenant_id=LEGACY_TENANT_ID, language="es")
    doc = catalog.upsert_document(
        tenant_id=LEGACY_TENANT_ID,
        document_id="doc_r1", library_id="lib_runs",
        source_key="r.pdf", title="Recientes", fmt="pdf",
    )
    for i in range(5):
        catalog.start_run(
        tenant_id=LEGACY_TENANT_ID,
            run_id=f"run_r{i}", workflow_id=f"wf_r{i}",
            kind="index", document_id=doc,
        )
    rows = catalog.recent_runs(3, tenant_id=LEGACY_TENANT_ID)
    assert len(rows) == 3
    assert [r.workflow_id for r in rows] == ["wf_r4", "wf_r3", "wf_r2"]
    assert rows[0].title == "Recientes"
    assert rows[0].library_id == "lib_runs"


def test_a_run_survives_the_document_it_was_spent_on(catalog):
    """`run.document_id` is ON DELETE SET NULL because cost and run history
    deliberately outlive removal. An inner join here would drop exactly that."""
    catalog.ensure_library("lib_orphan", "Huérfano", tenant_id=LEGACY_TENANT_ID, language="es")
    doc = catalog.upsert_document(
        tenant_id=LEGACY_TENANT_ID,
        document_id="doc_o1", library_id="lib_orphan",
        source_key="o.pdf", title="Se irá", fmt="pdf",
    )
    catalog.start_run(
        tenant_id=LEGACY_TENANT_ID,
        run_id="run_o1", workflow_id="wf_o1", kind="index", document_id=doc
    )
    catalog.remove_document("doc_o1")
    rows = [r for r in catalog.recent_runs(50, tenant_id=LEGACY_TENANT_ID) if r.workflow_id == "wf_o1"]
    assert len(rows) == 1
    assert rows[0].title is None
    assert rows[0].library_id is None


def test_the_three_listings_show_only_their_own_organisation(catalog):
    """A listing has no id to resolve ownership through, so the predicate is
    the whole boundary. Seeding a second organisation is what makes this a test
    rather than a restatement: with one tenant in the table, an unscoped query
    and a scoped one return the same rows."""
    other = "tnt_" + "a" * 24
    with catalog._conn() as conn:  # noqa: SLF001 - fixture-level setup
        conn.execute(
            "INSERT INTO tenant (id, slug, name) VALUES (%s, 'other', 'Otra')",
            (other,),
        )
    catalog.ensure_library("lib_mine", "Mía", tenant_id=LEGACY_TENANT_ID)
    catalog.ensure_library("lib_theirs", "Suya", tenant_id=other)
    catalog.start_run(
        tenant_id=other, run_id="run_theirs", workflow_id="wf-theirs", kind="index"
    )
    catalog.start_run(
        tenant_id=LEGACY_TENANT_ID, run_id="run_mine", workflow_id="wf-mine",
        kind="index",
    )

    mine = {r["id"] for r in catalog.libraries(tenant_id=LEGACY_TENANT_ID)}
    assert "lib_mine" in mine and "lib_theirs" not in mine
    assert [r["id"] for r in catalog.libraries(tenant_id=other)] == ["lib_theirs"]
    assert [r.id for r in catalog.recent_runs(50, tenant_id=other)] == ["run_theirs"]
    assert "run_theirs" not in {
        r.id for r in catalog.recent_runs(50, tenant_id=LEGACY_TENANT_ID)
    }
    assert catalog.project_totals(tenant_id=other).libraries == 1


def test_project_totals_counts_only_its_own_organisation(catalog):
    """Every figure carries its own predicate — there is no join to hang one
    outer filter on — so each is asserted rather than the row as a whole. A
    subquery added here without its predicate is the mistake the shape invites.
    """
    other = "tnt_" + "b" * 24
    with catalog._conn() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO tenant (id, slug, name) VALUES (%s, 'b', 'B')", (other,)
        )
    catalog.ensure_library("lib_b", "B", tenant_id=other)
    catalog.upsert_document(
        tenant_id=other, document_id="doc_b", library_id="lib_b",
        source_key="b.pdf", title="B", fmt="pdf",
    )
    catalog.register_version(
        tenant_id=other, version_id="ver_b", content_sha256="b" * 64,
        byte_size=1000, document_id="doc_b",
    )
    catalog.set_version_state("ver_b", "indexed")
    catalog.activate("doc_b", "ver_b")

    # The fixture seeds one legacy library, so the legacy figures are its own
    # and the assertion that matters is that none of B's crossed over.
    mine = catalog.project_totals(tenant_id=LEGACY_TENANT_ID)
    assert (mine.documents, mine.active_versions) == (0, 0)
    assert (mine.indexed_versions, mine.indexed_bytes) == (0, 0)
    assert (mine.versions_with_pages, mine.pages, mine.absent_documents) == (0, 0, 0)

    theirs = catalog.project_totals(tenant_id=other)
    assert (theirs.libraries, theirs.documents) == (1, 1)
    assert (theirs.active_versions, theirs.indexed_versions) == (1, 1)
    assert theirs.indexed_bytes == 1000


def test_a_library_id_another_organisation_holds_is_refused(catalog):
    """The library id arrives from the client as a plain string, so two
    organisations picking `lib_teologia` is ordinary rather than adversarial.
    Without the predicate the second ingest renames the first's library and
    attaches its documents to a row neither can list."""
    other = "tnt_" + "c" * 24
    with catalog._conn() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO tenant (id, slug, name) VALUES (%s, 'c', 'C')", (other,)
        )
    catalog.ensure_library("lib_shared", "Mía", tenant_id=LEGACY_TENANT_ID)

    with pytest.raises(LibraryOwnedByAnother) as caught:
        catalog.ensure_library("lib_shared", "Suya", tenant_id=other)
    assert caught.value.kind == "library_not_yours"

    # And the refusal left the original untouched, which is the half a rollback
    # would give for free and an `ON CONFLICT DO NOTHING` would not.
    rows = catalog.libraries(tenant_id=LEGACY_TENANT_ID)
    assert {r["id"]: r["name"] for r in rows}["lib_shared"] == "Mía"
    assert catalog.libraries(tenant_id=other) == []


def test_the_same_organisation_still_updates_its_own_library(catalog):
    catalog.ensure_library("lib_mine2", "Antes", tenant_id=LEGACY_TENANT_ID)
    catalog.ensure_library("lib_mine2", "Después", tenant_id=LEGACY_TENANT_ID)
    rows = {r["id"]: r["name"] for r in catalog.libraries(tenant_id=LEGACY_TENANT_ID)}
    assert rows["lib_mine2"] == "Después"


def test_a_library_keeps_the_name_it_was_given(catalog: Catalog):
    """The id is chosen by the client and is not a name.

    `activities/ingest.py` passed the id in both positions, so a library seeded
    as «Teología» read as `lib_teologia` in every picker from its first import
    onwards — visible in this installation's own catalog, where both rows are
    named after themselves.
    """
    catalog.ensure_library("lib_nueva", "Teología", tenant_id=LEGACY_TENANT_ID)
    rows = {b["id"]: b for b in catalog.libraries(tenant_id=LEGACY_TENANT_ID)}
    assert rows["lib_nueva"]["name"] == "Teología"


def test_an_import_that_carries_no_name_does_not_erase_one(catalog: Catalog):
    """Empty means "leave it alone", not "call it nothing".

    The name does not travel on every `IngestRequest`, so the common case is a
    second import into a library somebody already named. Overwriting it with a
    fallback would put the defect back one import later, which is the same
    reason `source_path` and `author` are `COALESCE`d on update.
    """
    catalog.ensure_library("lib_nueva", "Teología", tenant_id=LEGACY_TENANT_ID)
    catalog.ensure_library("lib_nueva", "", tenant_id=LEGACY_TENANT_ID)

    rows = {b["id"]: b for b in catalog.libraries(tenant_id=LEGACY_TENANT_ID)}
    assert rows["lib_nueva"]["name"] == "Teología"


def test_a_library_created_without_a_name_falls_back_to_its_id(catalog: Catalog):
    """Today's behaviour, kept deliberately: that is what makes the field
    additive. A caller that sends nothing gets exactly what it got before."""
    catalog.ensure_library("lib_sin_nombre", tenant_id=LEGACY_TENANT_ID)

    rows = {b["id"]: b for b in catalog.libraries(tenant_id=LEGACY_TENANT_ID)}
    assert rows["lib_sin_nombre"]["name"] == "lib_sin_nombre"
