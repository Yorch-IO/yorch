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
from psycopg.types.json import Json

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
class RunEvent:
    """One transition in a run's life, in the order it happened.

    Not a start and an end but a single moment: a stage's duration is the next
    event's `at` minus this one's, and the terminal event — the only one
    carrying an `outcome` — closes the last stage. A stage that started and
    never ended is therefore unrepresentable, which matters because that is
    exactly the shape a crashed run would leave behind and exactly what would be
    indistinguishable from a stage still working.
    """

    seq: int
    at: datetime
    stage: str
    #: One of `succeeded`, `failed`, `cancelled`, `blocked`, and only on the row
    #: that closes the run. NULL everywhere else.
    outcome: str | None
    detail: str | None


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
    #: Why it ended that way, truncated to 2000 characters by the workflow.
    #:
    #: Joined to the summary rather than left to `GET /runs/{id}` because a
    #: queue has to be able to say *why* a row is red without a second request
    #: per failed run.
    error_detail: str | None
    title: str | None
    #: `COALESCE(d.library_id, r.library_id)`, not the join alone.
    #:
    #: A run opens its row before its first activity, so a run that failed in
    #: `probe_video` or `stage_source` has no document to join to — and the
    #: import queue is per-library. Reading only the join is what made a failed
    #: video run invisible on 2026-09-05: the row existed and the screen that
    #: asked for it filtered it out.
    library_id: str | None
    #: What the run knew about itself before it had a document: the URL for a
    #: video, the picked file's basename for an import.
    #:
    #: Beside `title` rather than folded into it, because `title` *is* the
    #: document's title and a run with no document honestly has none. The UI
    #: reads `title ?? label ?? workflow_id`.
    label: str | None
    #: Both nullable and both `ON DELETE SET NULL`: cost and run history
    #: deliberately outlive the document they were spent on. A queue row whose
    #: document is gone still has a bill and an audit trail worth reading, and
    #: these are what let a caller ask for one.
    document_id: str | None
    version_id: str | None
    #: What this run has been billed so far, summed from `cost_entry`.
    #:
    #: `None`, never zero, when no stage has recorded a price — the same rule
    #: `Cost.usd` follows, and for the same reason: a run whose model has no
    #: price and a run that has not spent yet are different facts, and zero
    #: claims the second. A run still in its free stages legitimately has none.
    usd_so_far: float | None


#: How many times `open_turn` will retry a lost race for the next sequence
#: number. Two concurrent turns in one conversation is already unusual — the Ask
#: screen allows one question at a time for the same reason — so this is a
#: backstop against a duplicate signal, not a queue.
_TURN_SEQ_ATTEMPTS = 5


@dataclass
class Conversation:
    """One multi-turn conversation, as a list renders it."""

    id: str
    library_id: str
    title: str
    title_generated: bool
    created_at: datetime
    last_message_at: datetime
    turns: int = 0


@dataclass
class ConversationTurn:
    """One question and its answer.

    `searched` is the standalone question the rewrite produced and is what was
    actually embedded — shown rather than kept for debugging, because an answer
    to a question the person did not type is indistinguishable from a bad answer
    unless the substitution is visible.
    """

    seq: int
    question: str
    searched: str | None
    answer: str
    state: str
    effort: str
    style_effort: str | None
    citations: list[dict[str, Any]]
    cited_evidence: list[dict[str, Any]]
    error: dict[str, str] | None
    asked_at: datetime
    answered_at: datetime | None


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

    def answer_styles(self, *, tenant_id: str) -> dict[str, str]:
        """One organisation's overridden answer styles, keyed by effort level.

        Only levels somebody edited are rows here, so an absent key means "use
        the built-in default" rather than "empty style". That is what makes
        restoring a default a delete instead of a lookup of what the default
        used to be, lets a level added later need no backfill, and lets an
        improved default in `effort.py` reach every organisation that never
        overrode it.

        `tenant_id` is required for the reason every other listing here takes
        one: a style is a house style, and falling back to the legacy
        organisation's when a caller forgets would put one organisation's
        wording on another's answers with nothing saying so.
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    "SELECT effort, body FROM answer_style WHERE tenant_id = %s",
                    (tenant_id,),
                ).fetchall()
        return {r["effort"]: r["body"] for r in rows}

    def set_answer_style(self, effort: str, body: str, *, tenant_id: str) -> None:
        """Override one level's style, or clear the override.

        An empty or blank `body` deletes the row rather than storing whitespace,
        so "restore the default" and "save an empty box" are the same gesture
        and cannot drift into two different states — a stored empty string would
        otherwise mean "no style at all", which is a third behaviour nobody
        asked for and which `compose_system` would silently render as the bare
        rules.
        """
        if not body.strip():
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM answer_style "
                        "WHERE tenant_id = %s AND effort = %s",
                        (tenant_id, effort),
                    )
            return
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO answer_style (tenant_id, effort, body, updated_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (tenant_id, effort)
                    DO UPDATE SET body = EXCLUDED.body,
                                  updated_at = CURRENT_TIMESTAMP
                    """,
                    (tenant_id, effort, body.strip()),
                )

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
                   r.started_at, r.finished_at, r.error_kind, r.error_detail,
                   d.title, COALESCE(d.library_id, r.library_id) AS library_id,
                   r.label, r.document_id, r.version_id,
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

    #: The columns every `RunSummary` query selects, so a field added to the
    #: dataclass cannot be selected by one of them and forgotten by the other.
    _RUN_SUMMARY_COLUMNS = """
        r.id, r.workflow_id, r.kind, r.state, r.stage,
        r.started_at, r.finished_at, r.error_kind, r.error_detail,
        d.title, COALESCE(d.library_id, r.library_id) AS library_id, r.label,
        r.document_id, r.version_id,
        (SELECT sum(ce.usd) FROM cost_entry ce
          WHERE ce.run_id = r.id)::float8 AS usd_so_far
    """

    def runs(
        self,
        *,
        tenant_id: str,
        limit: int = 25,
        before: tuple[datetime, str] | None = None,
        kinds: Sequence[str] | None = None,
        states: Sequence[str] | None = None,
        library_id: str | None = None,
        document_id: str | None = None,
        version_id: str | None = None,
    ) -> list[RunSummary]:
        """The queue: every run this organisation has, newest first.

        Separate from :meth:`recent_runs`, which is the Home screen's and is
        deliberately limit-only — a landing page shows the last ten and needs no
        cursor. This one is what a person scrolls, so it filters and it pages.

        **Keyset paging on `(started_at, id)`, not an OFFSET.** Runs are started
        continuously and an OFFSET shifts under a list being appended to at the
        top, which shows a row twice or skips one. The id breaks ties:
        `started_at` defaults to `now()` and several runs enqueued from one
        multi-file drop can land in the same microsecond.

        `library_id` filters on `COALESCE(d.library_id, r.library_id)`. The join
        alone was not enough in **either** direction: a run whose document has
        been removed keeps its own column and stays reachable, and a run that
        failed before it registered a document — which is now every run that
        dies in its first activity, since the row is opened before it — has no
        document to join to at all. Reading only the join is what made a video
        run refused by YouTube invisible in the queue that had just started it.
        """
        where = ["r.tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if kinds:
            where.append("r.kind = ANY(%s)")
            params.append(list(kinds))
        if states:
            where.append("r.state = ANY(%s)")
            params.append(list(states))
        if library_id:
            # `COALESCE`, never `d.library_id` alone. A run opens its row before
            # its first activity now, so one that failed in `probe_video` or
            # `stage_source` has no document to join to — and filtering it out
            # here is precisely what kept a failed video run off the screen it
            # was opened for.
            where.append("COALESCE(d.library_id, r.library_id) = %s")
            params.append(library_id)
        if document_id:
            where.append("r.document_id = %s")
            params.append(document_id)
        if version_id:
            where.append("r.version_id = %s")
            params.append(version_id)
        if before is not None:
            where.append("(r.started_at, r.id) < (%s, %s)")
            params.extend(before)
        params.append(max(1, limit))

        sql = f"""
            SELECT {self._RUN_SUMMARY_COLUMNS}
              FROM run r
              LEFT JOIN document d ON d.id = r.document_id
             WHERE {" AND ".join(where)}
             ORDER BY r.started_at DESC, r.id DESC
             LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(RunSummary)) as cur:
                return cur.execute(sql, tuple(params)).fetchall()

    def run(self, run_id: str, *, tenant_id: str) -> RunSummary | None:
        """One run, by id, from the catalog alone.

        This is what makes a finished run readable at all. `GET /runs/{id}`
        answers from Temporal — `describe()` for the state, a query for the
        stage — and Temporal forgets a run when its retention expires, at which
        point the route reports `state: null` and `stage: null` for a run whose
        every column is still sitting in Postgres.

        Takes a required `tenant_id` even though the id alone would find the row.
        An id is not authorization: `_salt()` folds the tenant into a derived id
        to stop collisions, and a member of one organisation who holds the same
        file as another can recompute it. Only a predicate refuses a read.
        """
        sql = f"""
            SELECT {self._RUN_SUMMARY_COLUMNS}
              FROM run r
              LEFT JOIN document d ON d.id = r.document_id
             WHERE r.id = %s AND r.tenant_id = %s
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(RunSummary)) as cur:
                return cur.execute(sql, (run_id, tenant_id)).fetchone()

    def run_events(self, run_id: str, *, tenant_id: str) -> list[RunEvent]:
        """What the run did, in order. Ordered by `seq`, never by `at`.

        `at` is `workflow.now()`, which does not advance inside a workflow task:
        two transitions decided in the same task carry the same timestamp, and
        ordering by it would put them in whatever order the planner chose. `seq`
        is the workflow's own counter and is the only total order there is.
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(RunEvent)) as cur:
                return cur.execute(
                    """
                    SELECT e.seq, e.at, e.stage, e.outcome, e.detail
                      FROM run_event e
                     WHERE e.run_id = %s AND e.tenant_id = %s
                     ORDER BY e.seq
                    """,
                    (run_id, tenant_id),
                ).fetchall()

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
        library_id: str | None = None,
        label: str | None = None,
    ) -> str:
        """Open the row, idempotently. Called twice on the ingest paths, on
        purpose.

        `ON CONFLICT (id) DO NOTHING` is what lets a workflow open its row before
        its first activity and `register_document` keep its own call unchanged:
        the second one is a no-op, and `attach_version` fills in the document and
        version immediately after. The alternative — a `DO UPDATE` — would let a
        late caller overwrite `library_id` or `label` with the nulls it does not
        know about.

        `library_id` and `label` are what a run knows about itself before it has
        a document. They are the reason a run that fails in `probe_video` or
        `stage_source` is visible at all: the import queue is per-library and it
        filters on `COALESCE(d.library_id, r.library_id)`.
        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO run
                       (id, workflow_id, document_id, version_id, kind,
                        tenant_id, library_id, label)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (run_id, workflow_id, document_id, version_id, kind, tenant_id,
                 library_id or None, label or None),
            )
        return run_id

    def set_run_stage(
        self,
        run_id: str,
        stage: str,
        *,
        state: str | None = None,
        seq: int | None = None,
        at: datetime | None = None,
        detail: str | None = None,
    ) -> None:
        """Move the run's cursor, and — when told where it sits — record it.

        Both writes go through one connection on purpose. `run.stage` says where
        a run is *now* and is overwritten on every transition; `run_event` says
        what it did and is append-only. Splitting them across two activities
        would let a retry land one without the other and leave the trail
        disagreeing with the cursor about the same moment.

        `seq` and `at` come from the workflow, never from here: see
        :meth:`_insert_event` for why that is the whole idempotency story.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE run SET stage = %s, state = COALESCE(%s, state) WHERE id = %s",
                (stage, state, run_id),
            )
            if seq is not None and at is not None:
                self._insert_event(conn, run_id, seq=seq, at=at, stage=stage,
                                   detail=detail)

    @staticmethod
    def _insert_event(
        conn: Any,
        run_id: str,
        *,
        seq: int,
        at: datetime,
        stage: str,
        outcome: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Append one transition, idempotently.

        Two properties make a Temporal retry a no-op, and neither of them lives
        here — the database only enforces what the workflow already guarantees:

        - `seq` is a counter on the workflow object, so a retried transition
          carries the number it carried the first time and `ON CONFLICT DO
          NOTHING` drops it.
        - `at` is `workflow.now()`, fixed when the activity was first scheduled,
          so a retry records when the stage was *entered* rather than when the
          retry landed. `now()` here would move the timestamp under a retry and
          silently stretch the previous stage's measured duration.

        `tenant_id` is derived from the run in the INSERT, exactly as
        :meth:`record_cost` and :meth:`record_artifact` do, so an event and its
        run can never disagree about who owns them. **The consequence is that an
        event for a run that does not exist yet is silently dropped**, which is
        why `IngestWorkflow` buffers the two transitions that precede
        `register_document` instead of relying on this.
        """
        conn.execute(
            """
            INSERT INTO run_event (run_id, seq, at, stage, outcome, detail, tenant_id)
            SELECT %s, %s, %s, %s, %s, %s, r.tenant_id
              FROM run r WHERE r.id = %s
            ON CONFLICT (run_id, seq) DO NOTHING
            """,
            (run_id, seq, at, stage, outcome, detail, run_id),
        )

    def record_run_events(self, run_id: str, events: list[RunEvent]) -> None:
        """Flush transitions that happened before the run row existed.

        `staging` and `registering` run before `register_document`, so their
        inserts would find no run to derive a tenant from. The workflow holds
        them and calls this once the row is there. Same idempotency: each
        carries the `seq` it was given, so a retried flush inserts nothing.
        """
        if not events:
            return
        with self._conn() as conn:
            for event in events:
                self._insert_event(
                    conn,
                    run_id,
                    seq=event.seq,
                    at=event.at,
                    stage=event.stage,
                    outcome=event.outcome,
                    detail=event.detail,
                )

    def finish_run(
        self,
        run_id: str,
        state: str,
        *,
        error_kind: str | None = None,
        error_detail: str | None = None,
        seq: int | None = None,
        at: datetime | None = None,
        stage: str | None = None,
    ) -> bool:
        """Close the run, and close its last stage with it. False if there was
        no row.

        The terminal event names the stage the run was *in* when it ended, not a
        stage of its own: "at 14:07, in `semantics`, this ended `failed`" is the
        sentence somebody reading a red row needs, and inventing a synthetic
        `finished` stage would push the real answer one row further away.

        **The return value exists because a bare UPDATE cannot fail.** Zero rows
        affected raises nothing, so `record_run_outcome` reported Completed
        against a run row that did not exist — visible in the production history
        of `video-1788624193136-4fa22984` as event 13, an activity that
        succeeded at writing nothing. The row is opened before the first activity
        now, so this should not happen again; if it does, the caller says so
        rather than ticking.
        """
        with self._conn() as conn:
            updated = conn.execute(
                """
                UPDATE run SET state = %s, finished_at = now(),
                               error_kind = %s, error_detail = %s
                 WHERE id = %s
                """,
                (state, error_kind, error_detail, run_id),
            ).rowcount
            if seq is not None and at is not None and stage is not None:
                self._insert_event(
                    conn,
                    run_id,
                    seq=seq,
                    at=at,
                    stage=stage,
                    outcome=state,
                    detail=error_kind,
                )
        return updated > 0

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

    def costs(self, run_id: str, *, tenant_id: str) -> list[Cost]:
        """Every charge this run made, in the order it made them.

        The tenant predicate is on the query as well as on whatever guard the
        route ran, and that is not belt-and-braces: this statement reads another
        organisation's model names and bill straight out of the catalog, which is
        the same pair of guards `retrieve.search` keeps for `tenant_id` in a
        Qdrant filter.

        `ORDER BY id` rather than by `created_at`, because two charges inside one
        stage can share a timestamp and insertion order is the real sequence.
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(Cost)) as cur:
                return cur.execute(
                    # Cast in SQL, not in Python, for the reason `recent_runs`
                    # already gives: `cost_entry.usd` is `numeric(12, 6)`, so
                    # psycopg hands back a `Decimal` and the `float | None`
                    # annotation is a lie. Harmless while a row is only
                    # serialised one at a time — which is why it sat in the
                    # defect list rather than being fixed — and *not* harmless
                    # here, because the ledger sums these: the total came out of
                    # `/runs/{id}/audit` as the JSON **string** `"0.000000"`,
                    # which the Rust client's `Option<f64>` refuses outright.
                    # Measured against a real run, 2026-09-01.
                    "SELECT stage, provider, model, input_tokens, output_tokens, "
                    "usd::float8 AS usd "
                    "FROM cost_entry WHERE run_id = %s AND tenant_id = %s ORDER BY id",
                    (run_id, tenant_id),
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

    # -- conversations -----------------------------------------------------
    #
    # `conversation` takes a required `tenant_id` on every read, like every other
    # listing here. `conversation_turn` and `conversation_delta` never take one:
    # they derive it in the INSERT from the conversation they hang off, exactly
    # as `cost_entry`, `run_artifact` and `run_event` derive theirs from a run.
    # A turn and its conversation therefore cannot disagree about who owns them,
    # and a write naming a conversation that does not exist inserts nothing —
    # which is why `start_conversation` must run before any turn is signalled.

    def start_conversation(
        self,
        conversation_id: str,
        *,
        tenant_id: str,
        library_id: str,
        title: str,
    ) -> None:
        """Create the row a conversation's turns will hang off."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO conversation
                       (id, tenant_id, library_id, title)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (conversation_id, tenant_id, library_id, title),
            )

    def conversations(
        self, *, tenant_id: str, limit: int = 50
    ) -> list[Conversation]:
        """This organisation's conversations, most recently used first."""
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(Conversation)) as cur:
                return cur.execute(
                    """
                    SELECT c.id, c.library_id, c.title, c.title_generated,
                           c.created_at, c.last_message_at,
                           (SELECT count(*) FROM conversation_turn t
                             WHERE t.conversation_id = c.id)::int AS turns
                      FROM conversation c
                     WHERE c.tenant_id = %s
                     ORDER BY c.last_message_at DESC
                     LIMIT %s
                    """,
                    (tenant_id, limit),
                ).fetchall()

    def conversation(
        self, conversation_id: str, *, tenant_id: str
    ) -> Conversation | None:
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(Conversation)) as cur:
                return cur.execute(
                    """
                    SELECT c.id, c.library_id, c.title, c.title_generated,
                           c.created_at, c.last_message_at,
                           (SELECT count(*) FROM conversation_turn t
                             WHERE t.conversation_id = c.id)::int AS turns
                      FROM conversation c
                     WHERE c.id = %s AND c.tenant_id = %s
                    """,
                    (conversation_id, tenant_id),
                ).fetchone()

    def set_conversation_title(
        self,
        conversation_id: str,
        title: str,
        *,
        tenant_id: str,
        generated: bool = True,
    ) -> None:
        """Name a conversation.

        `generated=True` is the model naming it, and it happens once:
        `title_generated` is recorded rather than inferred from the turn count,
        because a title call that failed must not be retried on every subsequent
        turn of a long conversation.

        `generated=False` is the provisional name — the first question,
        truncated — and it **refuses to overwrite a generated one**. Without that
        guard the two writers race on a fast first turn: the model names the
        conversation, and a provisional write that was already in flight puts the
        truncated question back over it.
        """
        guard = "" if generated else " AND NOT title_generated"
        with self._conn() as conn:
            conn.execute(
                f"""
                UPDATE conversation
                   SET title = %s, title_generated = %s
                 WHERE id = %s AND tenant_id = %s{guard}
                """,
                (title, generated, conversation_id, tenant_id),
            )

    def delete_conversation(self, conversation_id: str, *, tenant_id: str) -> bool:
        """Remove a conversation and its turns. False if it was not theirs.

        Unlike removing a document, this touches no other store: a conversation
        has no Qdrant points and no graph nodes, so the ordering `removal.py`
        exists to enforce has nothing to order. The turns and their deltas go
        with it by cascade.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM conversation WHERE id = %s AND tenant_id = %s",
                    (conversation_id, tenant_id),
                )
                return cur.rowcount > 0

    def turns(
        self, conversation_id: str, *, tenant_id: str, limit: int | None = None
    ) -> list[ConversationTurn]:
        """The transcript, oldest first.

        `limit` keeps the *last* n, which is what reseeding a dormant session's
        window needs — the end of a conversation is what a pronoun reaches back
        into. Ordered ascending on the way out either way, because both the
        reader and the rewrite read it forwards.
        """
        tail = "" if limit is None else " ORDER BY t.seq DESC LIMIT %s"
        args: tuple[Any, ...] = (conversation_id, tenant_id)
        if limit is not None:
            args = args + (limit,)
        with self._conn() as conn:
            with conn.cursor(row_factory=class_row(ConversationTurn)) as cur:
                rows = cur.execute(
                    f"""
                    SELECT t.seq, t.question, t.searched, t.answer, t.state,
                           t.effort, t.style_effort, t.citations, t.cited_evidence,
                           t.error, t.asked_at, t.answered_at
                      FROM conversation_turn t
                      JOIN conversation c ON c.id = t.conversation_id
                     WHERE t.conversation_id = %s AND c.tenant_id = %s
                    {tail or " ORDER BY t.seq"}
                    """,
                    args,
                ).fetchall()
        return sorted(rows, key=lambda r: r.seq)

    def open_turn(
        self,
        conversation_id: str,
        *,
        tenant_id: str,
        question: str,
        effort: str,
    ) -> int | None:
        """Claim the next turn number and record the question. `None` if refused.

        One statement doing three things that must not be separable:

        * **the ownership predicate** — `c.tenant_id = %s` in the `SELECT`'s
          `WHERE`, so a conversation belonging to another organisation produces
          no row and this returns `None`. An id is not authorization, and the
          free plane's `/reindex` is the recorded example of what happens when a
          lookup by id is trusted on its own.
        * **the tenant derivation** — `c.tenant_id` is *selected*, never passed,
          so a turn cannot disagree with its conversation.
        * **the sequence** — computed inside the same insert. Two concurrent
          turns can still both read the same maximum under READ COMMITTED, and
          the primary key is what settles it: the loser gets a unique violation
          and retries, rather than silently overwriting the winner's question.

        The row is written before the turn is answered, deliberately. Same
        reasoning as `start_question_run` opening a run row before asking: a turn
        in flight has to be visible while it is in flight, or a reader who
        reloads mid-answer sees a conversation that has forgotten what they just
        said.
        """
        for _ in range(_TURN_SEQ_ATTEMPTS):
            try:
                with self._conn() as conn:
                    with conn.cursor() as cur:
                        row = cur.execute(
                            """
                            INSERT INTO conversation_turn
                                   (conversation_id, seq, tenant_id, question, effort)
                            SELECT c.id,
                                   coalesce((SELECT max(t.seq)
                                               FROM conversation_turn t
                                              WHERE t.conversation_id = c.id), 0) + 1,
                                   c.tenant_id, %s, %s
                              FROM conversation c
                             WHERE c.id = %s AND c.tenant_id = %s
                            RETURNING seq
                            """,
                            (question, effort, conversation_id, tenant_id),
                        ).fetchone()
                return None if row is None else int(row[0])
            except psycopg.errors.UniqueViolation:
                continue
        raise CatalogError(
            f"could not claim a turn number for {conversation_id}: "
            "too many concurrent turns"
        )

    def settle_turn(
        self,
        conversation_id: str,
        seq: int,
        *,
        state: str,
        searched: str = "",
        answer: str = "",
        style_effort: str | None = None,
        citations: Sequence[dict[str, Any]] = (),
        cited_evidence: Sequence[dict[str, Any]] = (),
        error: dict[str, str] | None = None,
    ) -> None:
        """Write the turn's outcome, and touch the conversation's clock.

        Both in one connection: a transcript whose last turn is answered while
        the list still sorts the conversation by an older timestamp is a
        conversation the reader cannot find again.
        """
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE conversation_turn
                   SET state = %s, searched = %s, answer = %s,
                       style_effort = %s, citations = %s, cited_evidence = %s,
                       error = %s, answered_at = CURRENT_TIMESTAMP
                 WHERE conversation_id = %s AND seq = %s
                """,
                (
                    state, searched or None, answer, style_effort,
                    Json(list(citations)), Json(list(cited_evidence)),
                    Json(error) if error else None,
                    conversation_id, seq,
                ),
            )
            conn.execute(
                "UPDATE conversation SET last_message_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (conversation_id,),
            )

    # -- the stream relay --------------------------------------------------

    def publish(
        self,
        conversation_id: str,
        turn_seq: int,
        rows: Sequence[tuple[int, str, str, dict[str, Any] | None]],
    ) -> None:
        """Publish relay rows for a turn in flight: `(chunk_seq, kind, text, detail)`.

        One ordered stream carrying two things. `kind` is `token` for prose and a
        stage name otherwise, and because both share `chunk_seq` they interleave
        without the reader having to merge two sources — which a timestamp could
        not do anyway, since two flushes inside a millisecond are exactly what
        the sequence exists to disambiguate.

        Prose is batched by the caller: a row per token would be tens of
        thousands of inserts for one answer. Stages are not batched, because
        there are five of them and each one's whole value is arriving *when it
        happens*.

        Best-effort at every call site: a relay that is down costs the reader a
        live view, never the answer, which lands on the turn row regardless.
        """
        if not rows:
            return
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO conversation_delta
                           (conversation_id, turn_seq, chunk_seq, tenant_id,
                            kind, text, detail)
                    SELECT %s, %s, %s, c.tenant_id, %s, %s, %s
                      FROM conversation c WHERE c.id = %s
                    ON CONFLICT (conversation_id, turn_seq, chunk_seq) DO NOTHING
                    """,
                    [
                        (
                            conversation_id, turn_seq, n, kind, text,
                            Json(detail) if detail is not None else None,
                            conversation_id,
                        )
                        for n, kind, text, detail in rows
                    ],
                )

    def relay(
        self, conversation_id: str, turn_seq: int, *, tenant_id: str, since: int = 0
    ) -> list[dict[str, Any]]:
        """Everything published for one turn after `since`, in order.

        `since` is the resume point that makes a dropped SSE connection cost
        nothing, which is the whole reason this is a table rather than a
        notification. Stage rows come back through it too, so a reader who
        reconnects mid-turn learns which stage it is in rather than only what
        prose it has missed.
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return cur.execute(
                    """
                    SELECT d.chunk_seq, d.kind, d.text, d.detail
                      FROM conversation_delta d
                      JOIN conversation c ON c.id = d.conversation_id
                     WHERE d.conversation_id = %s AND d.turn_seq = %s
                       AND d.chunk_seq > %s AND c.tenant_id = %s
                     ORDER BY d.chunk_seq
                    """,
                    (conversation_id, turn_seq, since, tenant_id),
                ).fetchall()

    def clear_deltas(self, conversation_id: str, turn_seq: int) -> None:
        """Drop a settled turn's relay rows.

        The finished text is on the turn row, so anything left here is a second
        copy of something already saved. Called after `settle_turn`, never
        before: a reader still following the stream has to be able to reach the
        end of it.
        """
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM conversation_delta "
                "WHERE conversation_id = %s AND turn_seq = %s",
                (conversation_id, turn_seq),
            )
