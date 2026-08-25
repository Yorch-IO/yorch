"""A disposable Postgres schema per test session.

Migrations run against a throwaway `search_path` schema rather than a throwaway
database: it needs no CREATE DATABASE privilege, it is one statement to drop,
and — the reason that matters — a bug in this fixture can destroy a test schema
but cannot destroy the catalog the developer is running the app against.
"""

from __future__ import annotations

import os
import pathlib
import secrets

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402

def _dev_url() -> str:
    """Where the developer's Postgres actually is.

    `BRAIN_DATABASE_URL` wins. Failing that, `infra/.env` is read, because the
    app generates the Postgres password on first launch and writes it there —
    the literal `brain:brain` in the docs is only ever right on a stack that has
    never been started by the app.
    """
    if url := os.environ.get("BRAIN_DATABASE_URL", "").strip():
        return url

    env = pathlib.Path(__file__).resolve().parents[3] / "infra" / ".env"
    values: dict[str, str] = {}
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip()

    user = values.get("BRAIN_PG_USER", "brain")
    password = values.get("BRAIN_PG_PASSWORD", "brain")
    port = values.get("BRAIN_POSTGRES_PORT", "5532")
    return f"postgresql://{user}:{password}@127.0.0.1:{port}/brain"


URL = _dev_url()


@pytest.fixture
def database_url():
    name = f"brain_test_{secrets.token_hex(6)}"
    try:
        with psycopg.connect(URL, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f'CREATE SCHEMA "{name}"')
    except Exception as e:
        pytest.skip(f"no Postgres at {URL}: {type(e).__name__}: {e}")

    scoped = f"{URL}?options=-csearch_path%3D{name}"
    try:
        yield scoped
    finally:
        with psycopg.connect(URL, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')
