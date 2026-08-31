"""Typed access to the catalog. The only place SQL for these tables is written.

One owner of the schema and one set of models is the reason catalog access sits
behind the control API rather than behind a Rust database driver. That argument
only holds if the Python side does not itself grow three places that each know
what a document row looks like, which is what this module prevents.

Every write that spans more than one table is one transaction. The recurring
failure it guards against is a version marked active whose links were never
written: questions would then resolve to a version with no chunks, and the
symptom — an empty answer — points nowhere near the cause.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import class_row, dict_row

from ..graph.schema import LEGACY_TENANT_ID
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)


class CatalogError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "catalog_error") -> None:
        super().__init__(message)
        self.kind = kind


class DuplicateContent(CatalogError):
    """These bytes are already indexed under another path.

    Not an error the pipeline should fail on — it is the *desired* outcome of
    importing a duplicate — so it carries the existing version id and the caller
    links to it instead of re-indexing.
    """

    def __init__(self, version_id: str) -> None:
        super().__init__(
            f"content already indexed as {version_id}", kind="duplicate_content"
        )
        self.version_id = version_id


class LibraryOwnedByAnother(CatalogError):
    """A library id already exists under a different organisation.

    Distinct from "not found": the id is real, and the caller may not have it.
    It carries `kind="library_not_yours"` rather than the generic one so that
    whatever surfaces it can say *why* — but nothing surfaces it yet. Today it
    fails the ingest activity, and through it the workflow, which is the right
    outcome and is why this is a raise rather than a quiet return. A 404 would
    be the wrong answer if it ever does reach HTTP: the user picked this library
    id, and "it does not exist" sends them to create it again under the same
    colliding id.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="library_not_yours")


# ---------------------------------------------------------------------------
# Row models
# ---------------------------------------------------------------------------


@dataclass
class Document:
    id: str
    library_id: str
    folder_id: str | None
    source_key: str
    title: str
    author: str | None
    format: str
    present: bool
    absent_since: datetime | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    #: Absolute path as the *worker* sees it, or None for a document imported
    #: before migration 003. `source_key` answers identity and is
    #: library-relative on purpose, so it cannot answer "where do I read this
    #: again". Defaulted so a SELECT that does not ask for it still builds a row.
    source_path: str | None = None
    #: Whose it is. Defaulted for the same reason: a SELECT written before this
    #: column existed still builds a row, and the value it would have carried is
    #: the one every pre-tenancy row already has.
    tenant_id: str = LEGACY_TENANT_ID


@dataclass
class Version:
    id: str
    content_sha256: str
    byte_size: int
    page_count: int | None
    state: str
    activated_at: datetime | None
    failed_reason: str | None
    created_at: datetime


@dataclass
class Run:
    id: str
    workflow_id: str
    document_id: str | None
    version_id: str | None
    kind: str
    state: str
    stage: str | None
    started_at: datetime
    finished_at: datetime | None
    error_kind: str | None
    error_detail: str | None


@dataclass
class ProjectTotals:
    """What the whole installation holds, counted once each.

    Every figure comes from its own scalar subquery rather than from one
    grouped join: `document_version_link` is many-to-many by design, so a
    version two documents hold would be summed twice by anything that joined
    through it. Counting versions directly is also what makes "a shared version
    counts once" true by construction rather than by a `DISTINCT` somebody has
    to remember.
    """

    libraries: int
    documents: int
    absent_documents: int
    active_versions: int
    indexed_versions: int
    indexed_bytes: int
    #: How many versions record a page count, and their sum. Nothing writes
    #: `page_count` today — `register_version` runs before the document has been
    #: extracted, so the number is not knowable there — and a caller must render
    #: "not recorded" rather than a zero that reads as "no pages". Kept as a
    #: measurement rather than a hardcoded absence so the figure starts working
    #: by itself the day something fills the column in.
    versions_with_pages: int
    pages: int


@dataclass
class RunSummary:
    """A run and the title of the document it was spent on, if that still exists.

    Separate from :class:`Run`, which maps the table exactly: `title` and
    `library_id` are joined, and folding them in would make every `Run` claim to
    carry columns most queries do not select. `title` is None for a run whose
    document has been removed — `run.document_id` is `ON DELETE SET NULL`
    because cost and run history deliberately outlive the document they were
    spent on.
    """

    id: str
    workflow_id: str
    kind: str
    state: str
    stage: str | None
    started_at: datetime
    finished_at: datetime | None
    error_kind: str | None
    title: str | None
    library_id: str | None
    #: What this run has been billed so far, summed from `cost_entry`.
    #:
    #: `None`, never zero, when no stage has recorded a price — the same rule
    #: `Cost.usd` follows, and for the same reason: a run whose model has no
    #: price and a run that has not spent yet are different facts, and zero
    #: claims the second. A run still in its free stages legitimately has none.
    usd_so_far: float | None


@dataclass
class Cost:
    """Token counts with a price applied, kept apart from each other.

    `usd` is NULL when no price is known for the model, which the UI renders as
    "not priced" rather than as free. Zero would be a lie.
    """

    stage: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    usd: float | None


#: Version states, in the order a successful run moves through them. Kept here
#: as well as in the CHECK constraint because the workflow branches on order,
#: and a string comparison against a literal would not catch a typo.
STATES: tuple[str, ...] = (
    "pending",
    "previewed",
    "approved",
    "indexing",
    "indexed",
    "failed",
)


class Catalog:
    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        timeout: float = 10.0,
        pooled: bool = True,
    ) -> None:
        """`timeout` bounds how long a caller waits for a connection.

        `pooled=False` opens one plain connection per call instead. That is the
        right shape for a single best-effort write from an activity, and the
        difference is not stylistic: a pool *retries a refused connection in the
        background*, so a caller waits the full timeout even when the database
        is actively refusing — turning "connection refused" from an immediate
        error into a three-second stall per statement. Long-lived callers like
        the control API still want the pool.
        """
        self._url = database_url
        self._timeout = timeout
        self._pool: ConnectionPool | None = None
        if pooled:
            # `open=False` then `open(wait=False)` so construction cannot block
            # on a database that is still starting — the control API has to
            # answer /health first, and that answer is the thing that says why.
            self._pool = ConnectionPool(
                database_url,
                min_size=min_size,
                max_size=max_size,
                open=False,
                timeout=timeout,
                kwargs={"connect_timeout": int(timeout)},
            )
            self._pool.open(wait=False)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextlib.contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        if self._pool is None:
            with psycopg.connect(
                self._url, connect_timeout=int(self._timeout), autocommit=True
            ) as conn:
                yield conn
            return
        with self._pool.connection(timeout=self._timeout) as conn:
            yield conn

    # -- libraries and folders --------------------------------------------

    def ensure_library(
        self, library_id: str, name: str = "", *, tenant_id: str, language: str = "es"
    ) -> str:
        """Create or update a library, refusing one that belongs to somebody else.

        An empty `name` means "do not rename it", not "call it nothing". The
        ingest is the only caller and it does not always know the name: the id
        arrives on every `IngestRequest` and the name does not, so a request
        that carries none must leave a library the user already named alone. A
        row created without one falls back to its id, which is what every
        library in this installation is currently called.

        **The library id is chosen by the client**, not derived: it arrives on
        `IngestRequest` as a plain string, and `lib_teologia` is the sort of
        value two organisations pick independently. Without the predicate on
        the conflict clause, the second one's ingest would silently rename the
        first one's library and then attach its documents to a row it does not
        own — invisible to both, since every listing filters on `tenant_id` and
        the two would disagree about which organisation the library is in.

        A refused write raises rather than returning quietly, because the caller
        is `register_version`, whose next statement writes a document pointing at
        this id. Failing the ingest is the outcome; landing in another
        organisation's library is not.
        """
        with self._conn() as conn:
            row = conn.execute(
                """
                INSERT INTO library (id, name, language, tenant_id)
                VALUES (%s, COALESCE(NULLIF(%s, ''), %s), %s, %s)
                ON CONFLICT (id) DO UPDATE
                    -- Tested against the parameter rather than against
                    -- `EXCLUDED.name`: the insert already folded an empty name
                    -- into the id, so by here the two are indistinguishable and
                    -- a second import would rename the library after itself.
                    SET name = CASE WHEN %s = '' THEN library.name
                                    ELSE EXCLUDED.name END,
                        language = EXCLUDED.language
                    WHERE library.tenant_id = EXCLUDED.tenant_id
                RETURNING id
                """,
                (library_id, name, library_id, language, tenant_id, name),
            ).fetchone()
        if row is None:
            raise LibraryOwnedByAnother(
                f"la biblioteca {library_id!r} pertenece a otra organización"
            )
        return library_id

    def libraries(self, *, tenant_id: str) -> list[dict[str, Any]]:
        """One organisation's libraries, with how much is answerable in each.

        `tenant_id` is required rather than defaulted for the reason phase 2
        dropped the column defaults: a listing that falls back to the legacy
        organisation when a caller forgets is a listing that shows the wrong
        corpus without saying so. The FastAPI plane is single-tenant and passes
        `LEGACY_TENANT_ID` explicitly, at the call site, where a reader can see
        which organisation the free plane serves.

        The counts are what make this a picker rather than a list of strings.
        A library with documents but nothing `indexed` answers every question
        with `off_corpus`, and a UI that cannot tell the two apart sends the
        user to look for a broken retriever instead of an unfinished import.

        `ensure_library` is called on the ingest path, so a library exists as
        soon as one document has been *registered* — before anything is indexed.
        Ordered by content first so the useful one is the obvious default.
        """
        sql = """
            SELECT l.id,
                   l.name,
                   l.language,
                   count(DISTINCT d.id) AS documents,
                   count(DISTINCT dv.id) FILTER (WHERE dv.state = 'indexed')
                       AS indexed_versions
              FROM library l
              LEFT JOIN document d ON d.library_id = l.id
              LEFT JOIN document_version_link k ON k.document_id = d.id
              LEFT JOIN document_version dv ON dv.id = k.version_id
             WHERE l.tenant_id = %s
             GROUP BY l.id, l.name, l.language
             ORDER BY count(DISTINCT dv.id) FILTER (WHERE dv.state = 'indexed')
                          DESC,
                      l.id
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return cur.execute(sql, (tenant_id,)).fetchall()

    def project_totals(self, *, tenant_id: str) -> ProjectTotals:
        """Everything the catalog knows about one organisation, in one round trip.

        Scalar subqueries rather than joins, so no figure can be inflated by the
        many-to-many between documents and versions. See :class:`ProjectTotals`.

        Every subquery carries the predicate itself. Wrapping the lot in one
        outer filter is not available here — there is no join to hang it on —
        and adding a table to this list without its predicate is the mistake the
        shape invites, so `test_project_totals_counts_only_its_own_organisation`
        seeds two organisations and checks each figure rather than the row.
        """
        sql = """
            SELECT (SELECT count(*) FROM library WHERE tenant_id = %(t)s),
                   (SELECT count(*) FROM document
                     WHERE present AND tenant_id = %(t)s),
                   (SELECT count(*) FROM document
                     WHERE NOT present AND tenant_id = %(t)s),
                   (SELECT count(*) FROM document_active_version a
                     JOIN document d ON d.id = a.document_id
                    WHERE d.tenant_id = %(t)s),
                   (SELECT count(*) FROM document_version
                     WHERE state = 'indexed' AND tenant_id = %(t)s),
                   (SELECT COALESCE(SUM(byte_size), 0) FROM document_version
                     WHERE state = 'indexed' AND tenant_id = %(t)s),
                   (SELECT count(*) FROM document_version
                     WHERE page_count IS NOT NULL AND tenant_id = %(t)s),
                   (SELECT COALESCE(SUM(page_count), 0) FROM document_version
                     WHERE page_count IS NOT NULL AND tenant_id = %(t)s)
        """
        with self._conn() as conn:
            row = conn.execute(sql, {"t": tenant_id}).fetchone()
        assert row is not None
        # SUM() over bigint comes back as Decimal, which is neither
        # JSON-serialisable nor what the dataclass declares.
        return ProjectTotals(
            libraries=int(row[0]),
            documents=int(row[1]),
            absent_documents=int(row[2]),
            active_versions=int(row[3]),
            indexed_versions=int(row[4]),
            indexed_bytes=int(row[5]),
            versions_with_pages=int(row[6]),
            pages=int(row[7]),
        )

    def recent_runs(self, limit: int = 10, *, tenant_id: str) -> list[RunSummary]:
        """The most recently started runs, newest first.

        `LEFT JOIN`, not an inner one: `run.document_id` is `ON DELETE SET NULL`
        and removal deliberately keeps the run and its costs, so an inner join
        would quietly drop exactly the history that was kept on purpose.

        `usd_so_far` is a correlated subquery rather than a join with a GROUP BY,
        because the row is already one per run and grouping would make every
        other column an aggregate for no gain. It reads only the catalog, which
        is what keeps `/project-summary` free of a dependency on Temporal — the
        landing screen has to degrade rather than fail, so what a run is *doing*
        belongs to `/runs/{id}`, and what it has *cost* belongs here.
        """
        sql = """
            SELECT r.id, r.workflow_id, r.kind, r.state, r.stage,
                   r.started_at, r.finished_at, r.error_kind,
                   d.title, d.library_id,
                   -- Cast in SQL, not in Python: `cost_entry.usd` is
                   -- `numeric(12, 6)`, so psycopg hands back a `Decimal` and the
                   -- annotation would be a lie the way `Cost.usd`'s already is —
                   -- harmless while it is only serialised, a `TypeError` the
                   -- first time anything sums or compares it against a float.
                   (SELECT sum(ce.usd) FROM cost_entry ce
                     WHERE ce.run_id = r.id)::float8 AS usd_so_far
              FROM run r
              LEFT JOIN document d ON d.id = r.document_id
             WHERE r.tenant_id = %s
             ORDER BY r.started_at DESC
             LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(RunSummary)) as cur:
                return cur.execute(sql, (tenant_id, max(1, limit))).fetchall()

    def ensure_folder(
        self,
        folder_id: str,
        library_id: str,
        path: str,
        *,
        tenant_id: str,
        watch: bool = True,
    ) -> str:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO source_folder (id, library_id, path, watch, tenant_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, library_id, path) DO UPDATE SET watch = EXCLUDED.watch
                """,
                (folder_id, library_id, path, watch),
            )
        return folder_id

    def mark_folder_scanned(self, folder_id: str, error: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE source_folder
                   SET last_scan_at = now(),
                       state = %s,
                       last_error = %s
                 WHERE id = %s
                """,
                ("error" if error else "idle", error, folder_id),
            )

    # -- documents ---------------------------------------------------------

    def upsert_document(
        self,
        *,
        document_id: str,
        library_id: str,
        source_key: str,
        title: str,
        fmt: str,
        author: str | None = None,
        folder_id: str | None = None,
        source_path: str | None = None,
        tenant_id: str,
    ) -> str:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO document
                       (id, library_id, folder_id, source_key, title, author,
                        format, source_path, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                   SET title = EXCLUDED.title,
                       author = COALESCE(EXCLUDED.author, document.author),
                       format = EXCLUDED.format,
                       folder_id = COALESCE(EXCLUDED.folder_id, document.folder_id),
                       -- COALESCE, so a caller that does not know the path
                       -- cannot erase one an earlier import recorded. The same
                       -- reasoning as `author` directly above.
                       source_path = COALESCE(EXCLUDED.source_path,
                                              document.source_path),
                       -- A re-import of a file that had gone missing brings it
                       -- back rather than leaving a present file marked absent.
                       present = true,
                       absent_since = NULL,
                       updated_at = now()
                """,
                (document_id, library_id, folder_id, source_key, title, author,
                 fmt, source_path, tenant_id),
            )
        return document_id

    def mark_absent(self, document_id: str) -> None:
        """A deleted file keeps its history; removal is a separate, explicit act.

        A folder unmounted for a minute must not destroy an indexed corpus.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE document SET present = false, absent_since = now(), "
                "updated_at = now() WHERE id = %s AND present",
                (document_id,),
            )

    def documents(self, library_id: str, *, include_absent: bool = False) -> list[Document]:
        sql = """
            SELECT id, library_id, folder_id, source_key, title, author, format,
                   present, absent_since, tags, created_at, updated_at,
                   source_path, tenant_id
              FROM document
             WHERE library_id = %s
        """
        if not include_absent:
            sql += " AND present"
        sql += " ORDER BY title"
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(Document)) as cur:
                return cur.execute(sql, (library_id,)).fetchall()

    # -- versions ----------------------------------------------------------

    def register_version(
        self,
        *,
        version_id: str,
        document_id: str,
        content_sha256: str,
        byte_size: int,
        tenant_id: str,
        page_count: int | None = None,
    ) -> tuple[Version, bool]:
        """Record these bytes and link them to this document.

        Returns the version and whether it is *new to this installation*. A
        duplicate is not an error: the second path links to the version the
        first one created, which is what stops byte-identical files being
        embedded twice and then competing in ranking.
        """
        with self._conn() as conn, conn.transaction():
            with conn.cursor(row_factory=class_row(Version)) as cur:
                row = cur.execute(
                    """
                    INSERT INTO document_version
                           (id, content_sha256, byte_size, page_count, tenant_id)
                    VALUES (%s, %s, %s, %s, %s)
                    -- Widened with the constraint: uniqueness of content is
                    -- per tenant now, so two customers importing the same
                    -- file get two versions rather than sharing one.
                    ON CONFLICT (tenant_id, content_sha256) DO NOTHING
                    RETURNING id, content_sha256, byte_size, page_count, state,
                              activated_at, failed_reason, created_at
                    """,
                    (version_id, content_sha256, byte_size, page_count, tenant_id),
                ).fetchone()

                created = row is not None
                if row is None:
                    row = cur.execute(
                        """
                        SELECT id, content_sha256, byte_size, page_count, state,
                               activated_at, failed_reason, created_at
                          FROM document_version WHERE content_sha256 = %s
                        """,
                        (content_sha256,),
                    ).fetchone()
                    assert row is not None  # the conflict proves it exists

            conn.execute(
                "INSERT INTO document_version_link (document_id, version_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (document_id, row.id),
            )
        return row, created

    def set_version_state(
        self, version_id: str, state: str, *, failed_reason: str | None = None
    ) -> None:
        if state not in STATES:
            raise CatalogError(f"unknown version state {state!r}; known: {STATES}")
        with self._conn() as conn:
            conn.execute(
                "UPDATE document_version SET state = %s, failed_reason = %s "
                "WHERE id = %s",
                (state, failed_reason, version_id),
            )

    def activate(self, document_id: str, version_id: str) -> None:
        """The last step of a successful run, and the only one that changes what
        a question can see.

        Done in one transaction with the state flip so a crash between them
        cannot leave a document pointing at a version still marked `indexing`.
        """
        with self._conn() as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO document_active_version (document_id, version_id)
                VALUES (%s, %s)
                ON CONFLICT (document_id) DO UPDATE
                   SET version_id = EXCLUDED.version_id, activated_at = now()
                """,
                (document_id, version_id),
            )
            conn.execute(
                "UPDATE document_version SET state = 'indexed', activated_at = now() "
                "WHERE id = %s",
                (version_id,),
            )

    def active_version(self, document_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT version_id FROM document_active_version WHERE document_id = %s",
                (document_id,),
            ).fetchone()
        return row[0] if row else None

    def version_by_content(self, content_sha256: str) -> Version | None:
        """Look a version up by its bytes.

        **Not tenant-scoped, and that is a phase-1 limitation rather than a
        decision.** Content uniqueness moved to `(tenant_id, content_sha256)`,
        so two tenants can now hold the same bytes and this would return
        whichever row Postgres reaches first. Nothing in the product calls it
        today — only tests do — which is the only reason it is harmless. The fix
        is a `tenant_id` argument, and it belongs with the change that gives the
        activities a tenant to pass; adding the parameter before there is
        anything to fill it would only move the guess.
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(Version)) as cur:
                return cur.execute(
                    """
                    SELECT id, content_sha256, byte_size, page_count, state,
                           activated_at, failed_reason, created_at
                      FROM document_version WHERE content_sha256 = %s
                    """,
                    (content_sha256,),
                ).fetchone()

    # -- removal -----------------------------------------------------------
    #
    # Permanent removal is an explicit user act, which is why none of this is
    # reachable from an import path. `mark_absent` above is the *other* half:
    # a file that disappeared keeps its history, because a folder unmounted for
    # a minute must not destroy an indexed corpus.
    #
    # Nothing here touches the file on disk.

    def document(
        self,
        document_id: str,
        *,
        library_id: str | None = None,
        tenant_id: str | None = None,
    ) -> Document | None:
        """One document, optionally constrained to a library.

        The library check is not redundant with the id even though
        `document_id(library, source_key)` derives from both: an id arriving over
        HTTP is a string, and checking makes a mistyped one a 404 instead of a
        removal in somebody else's library. That is the lesson of scoping the
        graph retrieval leg — the guard has to be where the query is, not where
        the id was minted.
        """
        sql = """
            SELECT id, library_id, folder_id, source_key, title, author, format,
                   present, absent_since, tags, created_at, updated_at,
                   source_path, tenant_id
              FROM document WHERE id = %s
        """
        params: tuple[Any, ...] = (document_id,)
        if tenant_id is not None:
            # Ownership, not narrowing. Without it two ids are enough to reach
            # another organisation's document — and `remove_document` takes
            # exactly those two.
            sql += " AND tenant_id = %s"
            params += (tenant_id,)
        if library_id is not None:
            sql += " AND library_id = %s"
            params += (library_id,)
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(Document)) as cur:
                return cur.execute(sql, params).fetchone()

    def versions_of(self, document_id: str) -> list[Version]:
        """Every version linked to this document, newest first."""
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(Version)) as cur:
                return cur.execute(
                    """
                    SELECT v.id, v.content_sha256, v.byte_size, v.page_count,
                           v.state, v.activated_at, v.failed_reason, v.created_at
                      FROM document_version v
                      JOIN document_version_link l ON l.version_id = v.id
                     WHERE l.document_id = %s
                     ORDER BY v.created_at DESC
                    """,
                    (document_id,),
                ).fetchall()

    def documents_holding(self, version_id: str) -> list[str]:
        """Every document linked to these bytes.

        The link table is many-to-many on purpose: the same bytes at two paths
        are two document slots and one version, which is what stops a duplicate
        file being embedded twice and then competing with itself in ranking. It
        is also why removal has to ask this question before deleting anything.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT document_id FROM document_version_link WHERE version_id = %s "
                "ORDER BY first_seen_at",
                (version_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def remove_version(self, version_id: str) -> dict[str, int]:
        """Delete one version and everything the schema cascades from it.

        `document_active_version.version_id` is `ON DELETE RESTRICT`, so the
        pointer has to be cleared first — deliberately, because "which version
        does this citation refer to" must have exactly one answer even after a
        crash, and a cascade there would silently leave a document with none.

        `document_version_link` and `profile_warning` cascade on their own.
        `run.version_id` is `ON DELETE SET NULL`: what a version cost is a fact
        about money that outlives it, and the run row keeps it.
        """
        with self._conn() as conn, conn.transaction():
            active = conn.execute(
                "DELETE FROM document_active_version WHERE version_id = %s",
                (version_id,),
            ).rowcount
            links = conn.execute(
                "SELECT count(*) FROM document_version_link WHERE version_id = %s",
                (version_id,),
            ).fetchone()[0]
            deleted = conn.execute(
                "DELETE FROM document_version WHERE id = %s", (version_id,)
            ).rowcount
        return {
            "versions": deleted,
            "links": links if deleted else 0,
            "active_pointers_cleared": active,
        }

    def remove_document(self, document_id: str, *, library_id: str | None = None) -> dict[str, int]:
        """Delete a document and any version it was the last to hold.

        Deleting the `document` row cascades its links and its active-version
        pointer; `document_version` is independent by design, so a version left
        with no surviving link has to be collected explicitly. A version another
        document still holds is left exactly where it is.
        """
        with self._conn() as conn, conn.transaction():
            held = [
                r[0]
                for r in conn.execute(
                    "SELECT version_id FROM document_version_link WHERE document_id = %s",
                    (document_id,),
                ).fetchall()
            ]
            sql = "DELETE FROM document WHERE id = %s"
            params: tuple[Any, ...] = (document_id,)
            if library_id is not None:
                sql += " AND library_id = %s"
                params += (library_id,)
            documents = conn.execute(sql, params).rowcount
            if not documents:
                return {"documents": 0, "versions": 0}

            versions = 0
            for version_id in held:
                # `NOT EXISTS` rather than a count read back into Python: the
                # link rows for this document are already gone inside this
                # transaction, so the question is only about other documents.
                orphan = conn.execute(
                    "SELECT NOT EXISTS (SELECT 1 FROM document_version_link "
                    "WHERE version_id = %s)",
                    (version_id,),
                ).fetchone()[0]
                if orphan:
                    conn.execute(
                        "DELETE FROM document_active_version WHERE version_id = %s",
                        (version_id,),
                    )
                    versions += conn.execute(
                        "DELETE FROM document_version WHERE id = %s", (version_id,)
                    ).rowcount
        return {"documents": documents, "versions": versions}

    def latest_run_with_artifact(self, version_id: str, kind: str) -> str | None:
        """The newest run for this version that produced the named artifact.

        Which run's artifacts a rebuild replays. Newest rather than the
        activating one: a later re-index that succeeded produced the chunks the
        active index was actually built from, and an older run's `chunks.jsonl`
        would rebuild a different document.
        """
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT r.id
                  FROM run r
                  JOIN run_artifact a ON a.run_id = r.id
                 WHERE r.version_id = %s AND a.name = %s
                       AND r.state = 'succeeded'
                 ORDER BY r.started_at DESC
                 LIMIT 1
                """,
                (version_id, kind),
            ).fetchone()
        return row[0] if row else None

    # -- runs --------------------------------------------------------------

    def start_run(
        self,
        *,
        run_id: str,
        workflow_id: str,
        kind: str,
        tenant_id: str,
        document_id: str | None = None,
        version_id: str | None = None,
    ) -> str:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO run
                       (id, workflow_id, document_id, version_id, kind, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (run_id, workflow_id, document_id, version_id, kind, tenant_id),
            )
        return run_id

    def set_run_stage(self, run_id: str, stage: str, *, state: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE run SET stage = %s, state = COALESCE(%s, state) WHERE id = %s",
                (stage, state, run_id),
            )

    def finish_run(
        self,
        run_id: str,
        state: str,
        *,
        error_kind: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE run SET state = %s, finished_at = now(),
                               error_kind = %s, error_detail = %s
                 WHERE id = %s
                """,
                (state, error_kind, error_detail, run_id),
            )

    def attach_version(self, run_id: str, document_id: str, version_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE run SET document_id = %s, version_id = %s WHERE id = %s",
                (document_id, version_id, run_id),
            )

    def record_artifact(
        self,
        run_id: str,
        *,
        name: str,
        rel_path: str,
        sha256: str,
        size_bytes: int,
    ) -> None:
        """Record one artifact this run produced.

        **The tenant is read from the run row rather than passed in**, and that
        is the stronger arrangement: an artifact belongs to whoever the run
        belongs to, so deriving it in the same statement makes the two unable to
        disagree. Passing it would have threaded a value through nineteen call
        sites for the privilege of being able to get it wrong at one of them.
        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO run_artifact
                       (run_id, name, rel_path, sha256, size_bytes, tenant_id)
                SELECT %s, %s, %s, %s, %s, r.tenant_id FROM run r WHERE r.id = %s
                ON CONFLICT (run_id, name) DO UPDATE
                   SET rel_path = EXCLUDED.rel_path,
                       sha256 = EXCLUDED.sha256,
                       size_bytes = EXCLUDED.size_bytes
                """,
                (run_id, name, rel_path, sha256, size_bytes, run_id),
            )

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return cur.execute(
                    "SELECT name, rel_path, sha256, size_bytes FROM run_artifact "
                    "WHERE run_id = %s ORDER BY name",
                    (run_id,),
                ).fetchall()

    # -- cost --------------------------------------------------------------

    def record_cost(
        self,
        run_id: str,
        *,
        stage: str,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        usd: float | None = None,
    ) -> None:
        """Append one measured charge.

        Append-only rather than an updated total: a retried activity that
        overwrote its row would under-report the spend that actually happened,
        and the tokens were spent whether or not the attempt succeeded.
        """
        with self._conn() as conn:
            conn.execute(
                """
                -- Same as `record_artifact`: the charge belongs to whoever the
                -- run belongs to, and deriving it here is what stops the two
                -- ever disagreeing.
                INSERT INTO cost_entry
                       (run_id, stage, provider, model, input_tokens,
                        output_tokens, usd, tenant_id)
                SELECT %s, %s, %s, %s, %s, %s, %s, r.tenant_id
                  FROM run r WHERE r.id = %s
                """,
                (run_id, stage, provider, model, input_tokens, output_tokens,
                 usd, run_id),
            )

    def costs(self, run_id: str) -> list[Cost]:
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(Cost)) as cur:
                return cur.execute(
                    "SELECT stage, provider, model, input_tokens, output_tokens, usd "
                    "FROM cost_entry WHERE run_id = %s ORDER BY id",
                    (run_id,),
                ).fetchall()

    def total_cost(self, run_id: str) -> dict[str, Any]:
        """Totals, with the unpriced part reported rather than folded in.

        A run that used a model with no known price would otherwise show a
        confident total that silently excludes it.
        """
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       SUM(usd),
                       COUNT(*) FILTER (WHERE usd IS NULL)
                  FROM cost_entry WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone()
        assert row is not None
        # SUM() over bigint returns numeric, which psycopg maps to Decimal — not
        # JSON-serialisable, and not what the dataclasses downstream declare.
        return {
            "input_tokens": int(row[0]),
            "output_tokens": int(row[1]),
            "usd": float(row[2]) if row[2] is not None else None,
            "unpriced_entries": int(row[3]),
        }

    # -- profile warnings --------------------------------------------------

    def record_profile_warning(
        self,
        *,
        version_id: str,
        profile_id: str,
        collides_with: str,
        similarity: float,
        detail: str,
    ) -> None:
        """A structural fingerprint collision, for the approval gate to show.

        There is no automatic fix and inventing one would be worse than the
        defect: retrieval metrics cannot see this, because the synthetic eval
        questions are generated from the very chunks the wrong rules produced.
        A person decides.
        """
        with self._conn() as conn:
            conn.execute(
                """
                -- Derived from the version, for the reason above.
                INSERT INTO profile_warning
                       (version_id, profile_id, collides_with, similarity,
                        detail, tenant_id)
                SELECT %s, %s, %s, %s, %s, v.tenant_id
                  FROM document_version v WHERE v.id = %s
                """,
                (version_id, profile_id, collides_with, similarity, detail,
                 version_id),
            )

    def open_profile_warnings(self, version_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return cur.execute(
                    """
                    SELECT id, profile_id, collides_with, similarity, detail
                      FROM profile_warning
                     WHERE version_id = %s AND NOT acknowledged
                     ORDER BY similarity DESC
                    """,
                    (version_id,),
                ).fetchall()

    def acknowledge_profile_warning(self, warning_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE profile_warning SET acknowledged = true WHERE id = %s",
                (warning_id,),
            )
