"""Bolt access to Memgraph, split into a read path and a write path.

The split is the point of this module. Question answering runs through
:meth:`Graph.query`, which accepts only a registered template id and typed
parameters; projection runs through :meth:`Graph.write`, which takes raw Cypher
and is reachable only from activity code. Nothing that handles a planner's
output can reach the second one.

**The database does not enforce the read side, and this was measured rather than
assumed.** Sessions here are opened with ``default_access_mode="READ"``, and
against Memgraph 3.12.0 a ``CREATE`` inside such a session *succeeds*: the Bolt
access mode is a routing hint for Neo4j clusters, not a permission, and
Memgraph's role-based access control is an Enterprise feature this stack does
not have. ``test_read_session_does_not_enforce_read_only`` pins that behaviour,
so the day it changes is a test failure rather than a silent assumption.

What holds the line is therefore structural, not a runtime check: a planner
never emits Cypher. It picks a template id from a fixed registry and supplies
parameters that travel as Bolt parameters, so no model output can become query
syntax. ``queries.validate_template`` guards the other direction — a human
writing a template that deletes, or forgetting a ``LIMIT`` — and it runs at
import, so such a template stops the process from starting. The access mode is
kept because it costs nothing and becomes real if the backend ever gains RBAC,
but it is belt, not braces.

The Neo4j driver is used rather than ``pymgclient`` because the latter links the
``mgclient`` C library, which would have to be built into the worker image for
every platform. The wire protocol is Bolt either way, and the worker image is
already the largest packaging risk in the project at 873 MB.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from .queries import Template, bind, get
from .schema import SCHEMA_STATEMENTS

log = logging.getLogger(__name__)

#: Long enough to survive a snapshot pause, short enough that the UI's spinner
#: is not the thing that tells the user something is wrong.
DEFAULT_TIMEOUT = 30.0


class GraphError(RuntimeError):
    """Memgraph refused or could not be reached. Carries no query text.

    Deliberately opaque to the caller: this reaches the UI, and a Cypher
    fragment in an error toast is both meaningless to the user and a way for
    template internals to leak into a screenshot.
    """

    def __init__(self, message: str, *, kind: str = "graph_error") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Row:
    """One result row, already converted out of driver types."""

    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


class Graph:
    def __init__(self, url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        # Memgraph runs without authentication here for the same reason Qdrant
        # does: the port is loopback-only and the stack is single-user. The
        # driver still requires a tuple, so an empty one is passed explicitly
        # rather than left to a default that might change.
        self._driver: Driver = GraphDatabase.driver(
            url, auth=("", ""), connection_timeout=timeout
        )
        self._timeout = timeout

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> "Graph":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- read path ---------------------------------------------------------

    def query(self, template_id: str, args: dict[str, Any] | None = None) -> list[Row]:
        """Run one registered template with typed arguments.

        There is no overload taking Cypher, and there must not be: since
        Memgraph does not enforce the read access mode (see the module
        docstring), this signature is the whole reason a question cannot write.
        """
        template: Template = get(template_id)
        params = bind(template, args or {})
        return self._read(template.cypher, params)

    def _read(self, cypher: str, params: dict[str, Any]) -> list[Row]:
        try:
            with self._driver.session(default_access_mode="READ") as session:
                result = session.run(cypher, params, timeout=self._timeout)
                return [Row(dict(record)) for record in result]
        except ServiceUnavailable as e:
            raise GraphError(str(e), kind="graph_unreachable") from e
        except Neo4jError as e:
            raise GraphError(str(e), kind="graph_refused") from e

    # -- write path --------------------------------------------------------

    def write(self, cypher: str, params: dict[str, Any] | None = None) -> list[Row]:
        """Projection only. Never reachable from a planner-chosen path."""
        try:
            with self._driver.session(default_access_mode="WRITE") as session:
                result = session.run(cypher, params or {}, timeout=self._timeout)
                return [Row(dict(record)) for record in result]
        except ServiceUnavailable as e:
            raise GraphError(str(e), kind="graph_unreachable") from e
        except Neo4jError as e:
            raise GraphError(str(e), kind="graph_refused") from e

    def write_many(self, statements: Iterable[tuple[str, dict[str, Any]]]) -> None:
        """Run several statements in one transaction.

        Projection has to be all-or-nothing per unit: a version whose sections
        landed but whose chunks did not would satisfy every structural query
        while returning nothing to cite.
        """
        try:
            with self._driver.session(default_access_mode="WRITE") as session:
                with session.begin_transaction(timeout=self._timeout) as tx:
                    for cypher, params in statements:
                        tx.run(cypher, params)
                    tx.commit()
        except ServiceUnavailable as e:
            raise GraphError(str(e), kind="graph_unreachable") from e
        except Neo4jError as e:
            raise GraphError(str(e), kind="graph_refused") from e

    # -- lifecycle ---------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create indexes and uniqueness constraints, ignoring "already exists".

        Each statement runs in its own transaction: Memgraph refuses index and
        constraint DDL inside a multi-command transaction, so the batched
        helper above cannot be used here.
        """
        for statement in SCHEMA_STATEMENTS:
            try:
                self.write(statement)
            except GraphError as e:
                if "already exists" in str(e).lower():
                    continue
                raise

    def probe(self) -> str:
        """One round trip, for the health endpoint. Returns a human detail line."""
        started = time.perf_counter()
        rows = self._read(
            "MATCH (n) RETURN count(n) AS nodes LIMIT 1", {}
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        nodes = rows[0]["nodes"] if rows else 0
        return f"{nodes} node(s), {elapsed_ms:.0f} ms"
