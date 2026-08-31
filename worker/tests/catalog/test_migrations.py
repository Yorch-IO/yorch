"""Schema behaviour. These need Postgres and skip, with the URL, without it.

Prisma owns this schema now, so what is tested here changed shape: there is no
longer a Python runner whose *applying* is the subject. What is tested is the
schema those migrations produce, the assertion that replaced the runner, and the
one property the tenancy migration changed — content uniqueness is now per
tenant.
"""

from __future__ import annotations

import psycopg
import pytest

from brainworker.catalog import migrations as m

LEGACY = "tnt_000000000000000000000001"


def _tables(database_url: str) -> set[str]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    return {r[0] for r in rows}


def test_applying_every_migration_creates_every_table(database_url: str):
    applied = m.apply_migrations(database_url)
    # Spelled out rather than derived from the directory: this list is the
    # tripwire for a migration arriving that nobody meant to add, and a derived
    # one would welcome it. Updating it is meant to be a deliberate act.
    assert applied == [
        "0_init",
        "20260826120000_tenancy",
        "20260826180000_tenant_required",
        "20260831140000_run_blocked",
        "20260831160000_run_kind_ask",
    ]
    assert {
        "library",
        "source_folder",
        "document",
        "document_version",
        "document_version_link",
        "document_active_version",
        "run",
        "run_artifact",
        "cost_entry",
        "profile_warning",
        "schema_migration",
        "tenant",
        "app_user",
        "tenant_membership",
    } <= _tables(database_url)


def test_applying_twice_applies_nothing_the_second_time(database_url: str):
    m.apply_migrations(database_url)
    assert m.apply_migrations(database_url) == []


def test_current_version_is_the_newest_applied(database_url: str):
    assert m.current_version(database_url) is None
    m.apply_migrations(database_url)
    assert m.current_version(database_url) == m.REQUIRED_MIGRATION


def test_require_schema_refuses_a_catalog_that_was_never_migrated(database_url: str):
    """The message has to name the fix. An operator reading /health sees this
    string and nothing else."""
    with pytest.raises(m.MigrationError, match="never been migrated"):
        m.require_schema(database_url)


def test_require_schema_refuses_a_catalog_behind_the_code(database_url: str):
    m.apply_migrations(database_url)
    with pytest.raises(m.MigrationError, match="behind this code"):
        m.require_schema(database_url, minimum="99999999999999_not_yet_written")


def test_require_schema_accepts_a_catalog_ahead_of_the_code(database_url: str):
    """A database migrated ahead of the binary reading it is the ordinary state
    during a rollout — the migrate step runs before either plane starts. Refusing
    it would make every deployment a flap."""
    m.apply_migrations(database_url)
    assert m.require_schema(database_url, minimum="0_init") == [
        "0_init",
        "20260826120000_tenancy",
        "20260826180000_tenant_required",
        "20260831140000_run_blocked",
        m.REQUIRED_MIGRATION,
    ]


def test_a_half_applied_migration_does_not_count_as_applied(database_url: str):
    """Prisma leaves `finished_at` null when a migration dies partway. Counting
    that row is how "the column is there" gets believed about a database where
    it is not."""
    m.apply_migrations(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE _prisma_migrations SET finished_at = NULL WHERE migration_name = %s",
            (m.REQUIRED_MIGRATION,),
        )
    with pytest.raises(m.MigrationError, match="behind this code"):
        m.require_schema(database_url)


def test_the_column_reindex_reads_is_nullable(database_url: str):
    """`source_key` is library-relative on purpose and cannot name a file, so
    re-index has nowhere to read from without `source_path`. Nullable, because
    documents imported before it exists have no recorded path and must say so
    rather than guess."""
    m.apply_migrations(database_url)
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'document' AND column_name = 'source_path' "
            "AND table_schema = current_schema()"
        ).fetchone()
    assert row is not None, "document.source_path is missing"
    assert row[0] == "YES"


def test_a_write_that_names_no_tenant_is_refused(database_url: str):
    """The acceptance criterion for phase 2, and the inverse of what stood here.

    Through phase 1 this asserted the opposite: a column default put an
    unqualified write into the legacy organisation, which is what let the free
    plane and every activity keep writing with no Python change. That test said
    of itself that it would be the thing to fail when the default came off, and
    that the removal had to arrive *with* the activities passing a tenant rather
    than instead of it. Both happened, so this is now the assertion.

    A silent wrong answer is worse than an error, and the field this guards is
    the one that decides whose data a row is.
    """
    m.apply_migrations(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.NotNullViolation):
            # Deliberately naming no tenant: this is the statement the column
            # default used to accept, and it is the whole point of the phase.
            conn.execute("INSERT INTO library (id, name) VALUES ('lib_1', 'L')")


def test_a_write_that_names_one_still_works(database_url: str):
    m.apply_migrations(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO library (id, name, tenant_id) VALUES ('lib_1', 'L', %s)",
            (LEGACY,),
        )
        row = conn.execute("SELECT tenant_id FROM library WHERE id = 'lib_1'").fetchone()
    assert row == (LEGACY,)


def test_identical_bytes_are_one_version_per_tenant_and_not_one_overall(
    database_url: str,
):
    """The rule this catalog was designed around — one DocumentVersion per
    distinct sha256, the fix for docagent's path-hashing `doc_id_for()` — now
    holds *per tenant*. Two customers importing the same PDF must get two
    versions, two sets of chunks and two bills; sharing them would leak one
    corpus into the other."""
    m.apply_migrations(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant (id, slug, name) "
            "VALUES ('tnt_00000000000000000000000b', 'otro', 'Otro')"
        )
        conn.execute(
            "INSERT INTO document_version (id, content_sha256, byte_size, tenant_id) "
            "VALUES ('ver_a', 'same', 1, %s)",
            (LEGACY,),
        )
        # The same bytes under a different tenant: allowed, and the point.
        conn.execute(
            "INSERT INTO document_version (id, content_sha256, byte_size, tenant_id) "
            "VALUES ('ver_b', 'same', 1, 'tnt_00000000000000000000000b')"
        )
        # The same bytes twice under one tenant: still refused.
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO document_version (id, content_sha256, byte_size, tenant_id) "
                "VALUES ('ver_c', 'same', 1, %s)",
                (LEGACY,),
            )


def test_a_tenant_holding_data_cannot_be_deleted(database_url: str):
    """RESTRICT rather than CASCADE. Cascading would take the catalog rows and
    leave the Qdrant points and Memgraph nodes they point at behind, unreachable
    and unnoticed — removal has an order (`removal.py`: projections first,
    catalog last) and a foreign key cannot honour it."""
    m.apply_migrations(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO library (id, name, tenant_id) VALUES ('lib_1', 'L', %s)",
            (LEGACY,),)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM tenant WHERE id = %s", (LEGACY,))


def test_a_tenant_id_must_look_like_a_graph_id(database_url: str):
    """Phase 2 puts this string on a graph node, where the query binder checks
    it against `(lib|fld|doc|…|tnt)_[0-9a-f]{24}`. A free-form id would pass
    every test here and fail there, months later."""
    m.apply_migrations(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO tenant (id, slug, name) VALUES ('acme', 'acme', 'A')")


def test_an_email_must_be_stored_lowercase(database_url: str):
    """The login lookup is case-insensitive, so two rows differing only in case
    would make it ambiguous which one claims the Cognito sub — and the wrong
    answer there hands one person's memberships to another."""
    m.apply_migrations(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO app_user (id, email) VALUES ('usr_1', 'Bob@Example.com')"
            )


def test_a_version_is_not_answerable_until_it_is_activated(database_url: str):
    """`state` and `activated_at` are the gate; the default must be closed."""
    m.apply_migrations(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO document_version (id, content_sha256, byte_size, tenant_id) "
            "VALUES ('ver_x', 'a', 1, %s)",
            (LEGACY,),
        )
        row = conn.execute(
            "SELECT state, activated_at FROM document_version WHERE id = 'ver_x'"
        ).fetchone()
    assert row == ("pending", None)


def test_one_document_cannot_have_two_active_versions(database_url: str):
    """Enforced by the database, because a citation must resolve after a crash."""
    m.apply_migrations(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO library (id, name, tenant_id) VALUES ('lib_1', 'L', %s)",
            (LEGACY,),)
        conn.execute(
            "INSERT INTO document (id, library_id, source_key, title, format, tenant_id) "
            "VALUES ('doc_1', 'lib_1', 'a.pdf', 'A', 'pdf', %s)",
            (LEGACY,),
        )
        for vid, sha in (("ver_1", "a"), ("ver_2", "b")):
            conn.execute(
                "INSERT INTO document_version (id, content_sha256, byte_size, tenant_id) "
                "VALUES (%s, %s, 1, %s)",
                (vid, sha, LEGACY),
            )
        conn.execute(
            "INSERT INTO document_active_version (document_id, version_id) "
            "VALUES ('doc_1', 'ver_1')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO document_active_version (document_id, version_id) "
                "VALUES ('doc_1', 'ver_2')"
            )


def test_a_cost_with_no_known_price_stays_null_rather_than_zero(database_url: str):
    """Zero would be a lie the UI would render as "free"."""
    m.apply_migrations(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO run (id, workflow_id, kind, tenant_id) "
            "VALUES ('run_1', 'wf-1', 'index', %s)",
            (LEGACY,),
        )
        conn.execute(
            "INSERT INTO cost_entry "
            "(run_id, stage, provider, model, input_tokens, tenant_id) "
            "VALUES ('run_1', 'correction', 'vertex', 'gemini-2.5-flash', 1000, %s)",
            (LEGACY,),
        )
        row = conn.execute("SELECT usd, input_tokens FROM cost_entry").fetchone()
    assert row[0] is None and row[1] == 1000


def test_a_rebuild_run_can_be_told_apart_from_a_reindex(database_url: str):
    """A rebuild replays artifacts and can only spend on embedding; a reindex
    re-reads the file and pays for correction too. `cost_entry` is the one place
    a user can see where the money went, so the two must not share a kind."""
    m.apply_migrations(database_url)
    with psycopg.connect(database_url) as conn:
        conn.execute("INSERT INTO library (id, name, tenant_id) VALUES ('lib_1', 'L', %s)",
            (LEGACY,),)
        for kind in ("index", "reindex", "rebuild"):
            conn.execute(
                "INSERT INTO run (id, workflow_id, kind, tenant_id) "
                "VALUES (%s, %s, %s, %s)",
                (f"r_{kind}", f"wf_{kind}", kind, LEGACY),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO run (id, workflow_id, kind, tenant_id) "
                "VALUES ('r_x', 'wf_x', 'nonsense', %s)",
                (LEGACY,),
            )
