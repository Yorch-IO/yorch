"""Migration behaviour. These need Postgres and skip, with the URL, without it."""

from __future__ import annotations

import pathlib

import psycopg
import pytest

from brainworker.catalog import migrations as m


def _tables(database_url: str) -> set[str]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    return {r[0] for r in rows}


def test_migrating_an_empty_catalog_creates_every_table(database_url: str):
    applied = m.migrate(database_url)
    # Spelled out rather than derived from the directory: this list is the
    # tripwire for a migration arriving that nobody meant to add, and a derived
    # one would welcome it. Updating it is meant to be a deliberate act.
    assert applied == [
        "001_initial.sql",
        "002_profile_warning.sql",
        "003_document_source_path.sql",
        "004_run_kind_rebuild.sql",
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
    } <= _tables(database_url)


def test_migrating_twice_applies_nothing_the_second_time(database_url: str):
    """It runs on every API start, so "already applied" is the common path."""
    m.migrate(database_url)
    assert m.migrate(database_url) == []


def test_current_version_tracks_the_highest_applied(database_url: str):
    assert m.current_version(database_url) is None
    m.migrate(database_url)
    # Derived, because what this test is about is that `current_version` returns
    # the *highest* applied version rather than the first or the last inserted.
    # The inventory itself is pinned by the test above, so a literal here would
    # only churn on every migration without checking anything more.
    highest = max(f.name.split("_", 1)[0] for f in m.SCHEMA_DIR.glob("*.sql"))
    assert m.current_version(database_url) == highest


def test_003_adds_the_column_reindex_reads(database_url: str):
    """`source_key` is library-relative on purpose and cannot name a file, so
    re-index has nowhere to read from without this. Nullable, because documents
    imported before it have no recorded path and must say so rather than guess."""
    m.migrate(database_url)
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'document' AND column_name = 'source_path' "
            "AND table_schema = current_schema()"
        ).fetchone()
    assert row is not None, "003 did not add document.source_path"
    assert row[0] == "YES"


def test_editing_an_applied_migration_is_refused(
    database_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    """Two machines silently disagreeing about the schema is the failure this
    prevents; both would report themselves fully migrated."""
    m.migrate(database_url)

    edited = tmp_path / "schema"
    edited.mkdir()
    for src in sorted(m.SCHEMA_DIR.glob("*.sql")):
        (edited / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    target = edited / "001_initial.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")

    monkeypatch.setattr(m, "SCHEMA_DIR", edited)
    with pytest.raises(m.MigrationError, match="changed after it was applied"):
        m.migrate(database_url)


def test_a_version_is_not_answerable_until_it_is_activated(database_url: str):
    """`state` and `activated_at` are the gate; the default must be closed."""
    m.migrate(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO document_version (id, content_sha256, byte_size) "
            "VALUES ('ver_x', 'a', 1)"
        )
        row = conn.execute(
            "SELECT state, activated_at FROM document_version WHERE id = 'ver_x'"
        ).fetchone()
    assert row == ("pending", None)


def test_one_document_cannot_have_two_active_versions(database_url: str):
    """Enforced by the database, because a citation must resolve after a crash."""
    m.migrate(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO library (id, name) VALUES ('lib_1', 'L')")
        conn.execute(
            "INSERT INTO document (id, library_id, source_key, title, format) "
            "VALUES ('doc_1', 'lib_1', 'a.pdf', 'A', 'pdf')"
        )
        for vid, sha in (("ver_1", "a"), ("ver_2", "b")):
            conn.execute(
                "INSERT INTO document_version (id, content_sha256, byte_size) "
                "VALUES (%s, %s, 1)",
                (vid, sha),
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


def test_identical_bytes_cannot_be_stored_as_two_versions(database_url: str):
    """The uniqueness that makes the duplicate-file fix real rather than a habit."""
    m.migrate(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO document_version (id, content_sha256, byte_size) "
            "VALUES ('ver_a', 'same', 1)"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO document_version (id, content_sha256, byte_size) "
                "VALUES ('ver_b', 'same', 1)"
            )


def test_a_cost_with_no_known_price_stays_null_rather_than_zero(database_url: str):
    """Zero would be a lie the UI would render as "free"."""
    m.migrate(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO run (id, workflow_id, kind) VALUES ('run_1', 'wf-1', 'index')"
        )
        conn.execute(
            "INSERT INTO cost_entry (run_id, stage, provider, model, input_tokens) "
            "VALUES ('run_1', 'correction', 'vertex', 'gemini-2.5-flash', 1000)"
        )
        row = conn.execute("SELECT usd, input_tokens FROM cost_entry").fetchone()
    assert row[0] is None and row[1] == 1000


def test_004_lets_a_rebuild_run_be_told_apart_from_a_reindex(database_url: str):
    """A rebuild replays artifacts and can only spend on embedding; a reindex
    re-reads the file and pays for correction too. `cost_entry` is the one place
    a user can see where the money went, so the two must not share a kind."""
    m.migrate(database_url)
    with psycopg.connect(database_url) as conn:
        conn.execute("INSERT INTO library (id, name) VALUES ('lib_1', 'L')")
        for kind in ("index", "reindex", "rebuild"):
            conn.execute(
                "INSERT INTO run (id, workflow_id, kind) VALUES (%s, %s, %s)",
                (f"r_{kind}", f"wf_{kind}", kind),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO run (id, workflow_id, kind) VALUES ('r_x', 'wf_x', 'nonsense')"
            )
