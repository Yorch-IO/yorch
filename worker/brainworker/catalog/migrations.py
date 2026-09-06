"""Schema *verification*, not schema application.

Ownership of this catalog's schema moved to Prisma when the NestJS control
plane arrived (`yorch-tauri-backend/prisma/migrations/`, a checkout beside
this one). This module used to apply
numbered SQL files on API startup; it no longer applies anything in production.
The four files it used to run are still on disk at `catalog/schema/` as the
historical record, and nothing reads them.

Why the assertion survived the runner it used to be:

The reason migrations ran at startup was that the schema then could not disagree
with the code reading it — there was no window in which a new API binary talked
to an old catalog. Moving migrations to a separate step reopens that window, so
something has to close it, and refusing to serve is the wrong answer for a
control plane whose `/health` is how an operator finds out what is wrong. So the
API asks, records the answer, and reports it on `/health` — the same shape the
old `MigrationError` path had.

`apply_migrations` exists for tests and only for tests. It reads the same files
`prisma migrate deploy` reads, so a test schema and a real one cannot drift, and
it needs no Node toolchain in the Python environment.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import uuid

import psycopg

log = logging.getLogger(__name__)

#: The oldest migration this code is willing to run against. Bump it in the same
#: commit as the code that needs the newer column, and never in a commit that
#: only adds one — an assertion ahead of the code it protects turns a working
#: deployment into a warning nobody can act on.
REQUIRED_MIGRATION = "20260906120000_delta_kind"

def _default_migrations_dir() -> pathlib.Path:
    """Where `prisma migrate deploy` reads from.

    The NestJS plane is its own checkout *beside* this one rather than a
    directory inside it, so this is a guess about someone else's disk —
    `BRAIN_MIGRATIONS_DIR` is how a machine that arranges things differently
    says so. It is absent inside the worker image, which ships neither
    checkout, and that is fine: nothing in production reads it and
    :func:`require_schema` never looks.
    """
    if override := os.environ.get("BRAIN_MIGRATIONS_DIR", "").strip():
        return pathlib.Path(override)
    repo = pathlib.Path(__file__).resolve().parents[3]
    return repo.parent / "yorch-tauri-backend" / "prisma" / "migrations"


MIGRATIONS_DIR = _default_migrations_dir()

#: The retired runner's own ledger, kept so an older worker image rolled back
#: onto this database still finds its four rows and applies nothing.
LEGACY_LEDGER = "schema_migration"


class MigrationError(RuntimeError):
    pass


def _applied(conn: psycopg.Connection) -> list[str]:
    """Migration names Prisma considers applied, oldest first.

    A row with `finished_at` unset is a migration that failed halfway, and one
    with `rolled_back_at` set was marked as reverted. Neither counts, and
    counting them is how "the column is there" gets believed about a database
    where it is not.
    """
    rows = conn.execute(
        "SELECT migration_name FROM _prisma_migrations "
        "WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL "
        "ORDER BY started_at"
    ).fetchall()
    return [name for (name,) in rows]


def current_version(database_url: str) -> str | None:
    """The newest applied migration, or None if the catalog is untouched.

    An absent table is a normal answer rather than an error: `/health` may be
    asked before migrations have ever run, and "no schema" is more useful there
    than a traceback.
    """
    with psycopg.connect(database_url) as conn:
        try:
            names = _applied(conn)
        except psycopg.errors.UndefinedTable:
            return None
    return names[-1] if names else None


def require_schema(
    database_url: str, minimum: str = REQUIRED_MIGRATION
) -> list[str]:
    """Raise unless `minimum` has been applied. Returns what has been.

    Deliberately a membership test rather than a comparison against the newest
    name: a database migrated *ahead* of this code is the ordinary state during
    a rollout and must not be refused, while a database missing the migration
    this code was written against must be.
    """
    try:
        with psycopg.connect(database_url) as conn:
            names = _applied(conn)
    except psycopg.errors.UndefinedTable:
        raise MigrationError(
            "the catalog has never been migrated (_prisma_migrations is absent). "
            "Run `npx prisma migrate deploy` from the yorch-tauri-backend "
            "checkout, or start the `migrate` compose service."
        ) from None

    if minimum not in names:
        newest = names[-1] if names else "nothing"
        raise MigrationError(
            f"catalog schema is behind this code: {minimum!r} has not been "
            f"applied (newest applied: {newest}). Run `npx prisma migrate "
            "deploy` from the yorch-tauri-backend checkout."
        )
    return names


def _files() -> list[pathlib.Path]:
    if not MIGRATIONS_DIR.is_dir():
        raise MigrationError(
            f"no migrations directory at {MIGRATIONS_DIR} — this function is "
            "test support and needs the repository checkout, not the image."
        )
    files = sorted(MIGRATIONS_DIR.glob("*/migration.sql"), key=lambda p: p.parent.name)
    if not files:
        raise MigrationError(f"no migrations found in {MIGRATIONS_DIR}")
    return files


def apply_migrations(database_url: str) -> list[str]:
    """Apply every migration, for a throwaway test schema. **Not production.**

    Production applies migrations with `prisma migrate deploy`. This exists so
    the Python suite can build a schema without a Node toolchain, and it reads
    the same files that command reads — which is the point. A helper that
    maintained its own copy of the DDL would let the tests pass against a schema
    the product does not have, which is the failure this whole move was meant to
    end.

    The `_prisma_migrations` rows it writes carry the same sha256 Prisma
    computes, so a schema built here and one built by the CLI are
    indistinguishable to :func:`require_schema`.
    """
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _prisma_migrations (
                id                  varchar(36) PRIMARY KEY,
                checksum            varchar(64) NOT NULL,
                finished_at         timestamptz,
                migration_name      varchar(255) NOT NULL,
                logs                text,
                rolled_back_at      timestamptz,
                started_at          timestamptz NOT NULL DEFAULT now(),
                applied_steps_count integer NOT NULL DEFAULT 0
            )
            """
        )
        known = set(_applied(conn))
        for path in _files():
            name = path.parent.name
            if name in known:
                continue
            body = path.read_text(encoding="utf-8")
            log.info("applying migration %s", name)
            with conn.transaction():
                conn.execute(body)
                conn.execute(
                    "INSERT INTO _prisma_migrations "
                    "(id, checksum, migration_name, finished_at, applied_steps_count) "
                    "VALUES (%s, %s, %s, now(), 1)",
                    (
                        str(uuid.uuid4()),
                        hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        name,
                    ),
                )
            applied.append(name)
    return applied
