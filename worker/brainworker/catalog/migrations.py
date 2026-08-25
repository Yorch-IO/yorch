"""Schema migration, run by the control API on startup.

Startup rather than a separate step, because the schema version then cannot
disagree with the code that reads it: there is no window in which a new API
binary is talking to an old catalog. The cost is that migrations must be fast
and additive; anything that rewrites a large table belongs in a maintenance
workflow instead, and the app should refuse to start rather than block on it.

Concurrency is handled with a Postgres advisory lock rather than by assuming a
single API process. In host dev mode a developer routinely has the containerised
API and a host one pointed at the same database, and two processes racing
`CREATE TABLE` produce an error that reads like a corrupted catalog.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib

import psycopg

log = logging.getLogger(__name__)

SCHEMA_DIR = pathlib.Path(__file__).parent / "schema"

#: Arbitrary but fixed. Postgres advisory locks are a single global namespace;
#: this value identifies "the Company Brain catalog migration" within it.
LOCK_KEY = 0x0B3A17_10


class MigrationError(RuntimeError):
    pass


def _files() -> list[pathlib.Path]:
    files = sorted(SCHEMA_DIR.glob("*.sql"))
    if not files:
        raise MigrationError(f"no migrations found in {SCHEMA_DIR}")
    seen: set[str] = set()
    for f in files:
        version = f.name.split("_", 1)[0]
        if not version.isdigit():
            raise MigrationError(f"migration {f.name} does not start with a number")
        if version in seen:
            raise MigrationError(f"duplicate migration number {version}")
        seen.add(version)
    return files


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            version    text PRIMARY KEY,
            name       text NOT NULL,
            -- Stored so an edit to an already-applied file is caught. Editing
            -- one is the mistake that makes two machines disagree about what
            -- the schema is while both report themselves up to date.
            sha256     text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def migrate(database_url: str) -> list[str]:
    """Apply pending migrations. Returns the names applied, newest last."""
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        _ensure_table(conn)
        # Blocks rather than failing: the other process is doing the same work,
        # and the right behaviour is to wait for it and then find nothing to do.
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            rows = conn.execute("SELECT version, name, sha256 FROM schema_migration").fetchall()
            known = {version: (name, sha) for version, name, sha in rows}

            for path in _files():
                version, _, _ = path.name.partition("_")
                body = path.read_text(encoding="utf-8")
                digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

                if version in known:
                    _, previous = known[version]
                    if previous != digest:
                        raise MigrationError(
                            f"{path.name} changed after it was applied "
                            f"({previous[:12]} → {digest[:12]}). Add a new "
                            "migration instead of editing this one."
                        )
                    continue

                log.info("applying migration %s", path.name)
                with conn.transaction():
                    conn.execute(body)
                    conn.execute(
                        "INSERT INTO schema_migration (version, name, sha256) "
                        "VALUES (%s, %s, %s)",
                        (version, path.name, digest),
                    )
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return applied


def current_version(database_url: str) -> str | None:
    """The highest applied migration, or None if the catalog is untouched.

    An absent table is a normal answer, not an error: the health endpoint may
    be asked before migrations have ever run, and reporting "no schema" is more
    useful there than raising.
    """
    with psycopg.connect(database_url) as conn:
        try:
            row = conn.execute(
                "SELECT version FROM schema_migration ORDER BY version DESC LIMIT 1"
            ).fetchone()
        except psycopg.errors.UndefinedTable:
            return None
    return row[0] if row else None
