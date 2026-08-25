"""Permanent removal, across all three stores, in the one order that is safe.

**Projections first, catalog last.** Postgres is the operational source of
truth; Qdrant and Memgraph are projections of it, and either can be rebuilt from
catalog rows plus the original file while neither can be reconstructed from the
other. That asymmetry decides the order:

* Delete the catalog row first and crash, and the projections keep points and
  nodes that nothing references and nothing can find again to retry — an
  unrecoverable leak, and one that is invisible because the shelf looks right.
* Delete the projections first and crash, and the catalog still names the
  document, so pressing the button again finishes the job.

Ordering is therefore what makes removal idempotent, and idempotence is why this
is a plain function called from a request rather than a Temporal workflow: the
remedy for a failure is to ask again, which is exactly the reasoning `/ask`
already uses.

A second property, inherited rather than built here: `answering.retrieve._hydrate`
requires a chunk's owning `Document` before it will build Evidence from it, so
even a half-finished removal cannot produce an answer citing a document that is
on its way out. That is a backstop, not the plan.

**Nothing here touches the file on disk.** The plan asks for removal of versions
and indexes. Deleting the user's book is a different act, and not a recoverable
one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import config
from .catalog import Catalog
from .graph import Graph
from .graph import projection as proj

log = logging.getLogger(__name__)


class RemovalError(RuntimeError):
    """Something the caller can act on, carrying the kind the UI keys advice on."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Removal:
    """What was destroyed, and what deliberately was not.

    Both halves, because an irreversible act reported only as "done" leaves the
    user to guess whether their cost history went with it. It did not.
    """

    document_id: str | None = None
    versions_removed: list[str] = field(default_factory=list)
    versions_kept: list[str] = field(default_factory=list)
    qdrant_points: int = 0
    qdrant_repointed: int = 0
    graph: dict[str, int] = field(default_factory=dict)
    catalog: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "versions_removed": self.versions_removed,
            "versions_kept": self.versions_kept,
            "qdrant_points": self.qdrant_points,
            "qdrant_repointed": self.qdrant_repointed,
            "graph": self.graph,
            "catalog": self.catalog,
            # Stated rather than implied. `run.document_id` and `run.version_id`
            # are ON DELETE SET NULL by design: what a document cost is a fact
            # about money that outlives the document.
            "kept": {
                "runs": "conservados, con document_id en NULL",
                "costs": "conservados",
                "source_file": "intacto",
            },
        }


def _qdrant(settings: config.Settings):
    from docagent.qdrant import Qdrant

    return Qdrant(settings.qdrant_url, settings.qdrant_collection)


def remove_document(
    settings: config.Settings, *, library_id: str, document_id: str
) -> Removal:
    """Remove a document, and every version it was the last to hold."""
    with Catalog(settings.database_url) as catalog:
        document = catalog.document(document_id, library_id=library_id)
        if document is None:
            raise RemovalError(
                f"no existe {document_id!r} en la biblioteca {library_id!r}",
                kind="document_not_found",
            )
        versions = catalog.versions_of(document_id)
        # Who else holds each of these bytes. A version at a second path is one
        # version and two document slots, so removing one slot must leave it.
        holders = {v.id: catalog.documents_holding(v.id) for v in versions}

    sole = [v.id for v in versions if not _others(holders[v.id], document_id)]
    shared = [v.id for v in versions if _others(holders[v.id], document_id)]

    result = Removal(document_id=document_id, versions_removed=sole,
                     versions_kept=shared)

    # 1. Qdrant.
    with _qdrant(settings) as q:
        if q.exists():
            for version_id in sole:
                result.qdrant_points += q.delete_by_filter(
                    {"library_id": library_id, "version_id": version_id}
                )
            # A point's payload names the document that *indexed* it. When two
            # documents share a version the second took the `link_duplicate`
            # path and never re-indexed, so removing the first leaves every
            # point naming a document that no longer exists. Attribution comes
            # from the graph, so no answer is wrong — but a stale value on an
            # indexed field is a trap for whatever filters on it next.
            for version_id in shared:
                survivor = _others(holders[version_id], document_id)[0]
                result.qdrant_repointed += q.set_payload(
                    {"library_id": library_id, "version_id": version_id,
                     "document_id": document_id},
                    {"document_id": survivor},
                )

    # 2. Memgraph. It makes the same shared-version decision independently,
    #    from `HAS_VERSION`, because that edge is the graph's own record of it.
    with Graph(settings.memgraph_url) as graph:
        result.graph = proj.remove_document(graph, document_id).as_dict()

    # 3. Catalog, last and only now.
    with Catalog(settings.database_url) as catalog:
        result.catalog = catalog.remove_document(document_id, library_id=library_id)

    log.info(
        "removed document %s from %s: %d points, %s",
        document_id, library_id, result.qdrant_points, result.graph,
    )
    return result


def remove_version(
    settings: config.Settings, *, library_id: str, version_id: str
) -> Removal:
    """Remove one version, leaving its document and any other versions standing.

    Deleting the active version leaves the document with no active version,
    which is the honest state: `document_active_version` is cleared rather than
    silently repointed at an older version the user did not choose.
    """
    with Catalog(settings.database_url) as catalog:
        holders = catalog.documents_holding(version_id)
        in_library = [
            h for h in holders
            if catalog.document(h, library_id=library_id) is not None
        ]
        if not in_library:
            raise RemovalError(
                f"no existe la versión {version_id!r} en la biblioteca {library_id!r}",
                kind="version_not_found",
            )

    result = Removal(versions_removed=[version_id])

    with _qdrant(settings) as q:
        if q.exists():
            result.qdrant_points = q.delete_by_filter(
                {"library_id": library_id, "version_id": version_id}
            )

    with Graph(settings.memgraph_url) as graph:
        result.graph = proj.remove_version(graph, version_id).as_dict()

    with Catalog(settings.database_url) as catalog:
        result.catalog = catalog.remove_version(version_id)

    return result


def _others(holders: list[str], document_id: str) -> list[str]:
    return [h for h in holders if h != document_id]
