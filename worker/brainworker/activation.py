"""Promoting a version by hand, when the pipeline deliberately did not.

An ingest that finds a **structural collision** indexes everything and withholds
only the last step: the fingerprint that selects a family profile is structural,
and structure is not subject matter, so two unrelated families sharing a layout
collide exactly and the second document is chunked with the first's heading
rules. Retrieval metrics provably cannot see that — the eval questions are
generated from the very chunks the wrong rules produced — so the escape hatch has
to be a person's.

There are two of them, and they are different acts. Re-importing with
`ignore_profile` says *the inherited rules were wrong*: it re-chunks with the
engine's measured defaults and costs another pipeline run. This says *the rules
were right and the check was over-cautious*, and costs nothing, because the index
it promotes is the one already paid for.

**Catalog first, then graph** — the opposite of removal's order, and for the same
underlying reason. A crash between the two leaves a document answerable from a
graph whose `active` flag is stale, which re-projection repairs; the reverse
leaves a version answerable in the graph that the catalog does not consider
active, which nothing repairs on its own. `activities/ingest.activate_version`
makes the same choice, and this exists so the two cannot drift.

A plain function called from a request rather than a workflow, like removal: the
remedy for a failure is to ask again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import config
from .audit import audited
from .catalog import Catalog
from .graph import Graph
from .graph import projection as proj

log = logging.getLogger(__name__)


class ActivationError(RuntimeError):
    """Something the caller can act on, carrying the kind the UI keys advice on."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Activation:
    version_id: str
    #: Every document holding these bytes. A version can be shared — byte-identical
    #: files at two paths are two documents and one version — and promoting it for
    #: one while leaving the other pointing at an older version would make the
    #: same content answer differently depending on which copy was asked about.
    documents: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"version_id": self.version_id, "documents": list(self.documents)}


def activate_version(
    library_id: str, version_id: str, *, tenant_id: str, run_id: str | None = None
) -> Activation:
    """Promote `version_id`, for the organisation that owns it.

    **The tenant is required, and it used to default to the legacy one.** That
    default is the same mistake `VersionNode.tenant_id` already made and it fails
    the same two ways, because this function does both things: the catalog lookup
    below is the authorization predicate — a salted id is not authorization, so
    the default silently refused every organisation that is not the legacy one —
    and `proj.activate` builds a `VersionNode` from it, so a caller that meant a
    different organisation would have written its activation into the legacy
    graph under an id nothing there computes.

    Measured 2026-08-31: `ver_0b71d21eeb3228f54437d9cf` sat `pending` in
    `tnt_f489b4a62220158ef6790c07` with its index paid for, its 600 points in
    Qdrant and its 600 chunks projected — and the one route that could have
    published it for $0 answered 404 for the organisation that owned it. Naming
    the tenant at each call site is what makes the free plane's answer a decision
    rather than an accident; it is that organisation, and it says so.
    """
    settings = config.load()

    with Catalog(settings.database_url) as catalog:
        documents = [
            d
            for d in catalog.documents_holding(version_id)
            # Scoped, because a version id is a value its own members hold and a
            # salted id is not authorization. The predicate is the boundary.
            if (doc := catalog.document(d, library_id=library_id, tenant_id=tenant_id))
            is not None
        ]
        if not documents:
            raise ActivationError(
                f"no existe {version_id!r} en {library_id!r}",
                kind="version_not_found",
            )
        rows = {
            d: catalog.document(d, library_id=library_id, tenant_id=tenant_id)
            for d in documents
        }
        version = next(
            (v for v in catalog.versions_of(documents[0]) if v.id == version_id), None
        )
        if version is None:  # pragma: no cover - documents_holding said otherwise
            raise ActivationError(
                f"no existe {version_id!r}", kind="version_not_found"
            )
        for document_id in documents:
            catalog.activate(document_id, version_id)

    # Recorded only now, because everything above is the authorization check and
    # a run row for a version this caller may not touch would be a leak dressed
    # as an audit trail.
    with audited(
        settings,
        kind="activate",
        tenant_id=tenant_id,
        run_id=run_id,
        document_id=documents[0],
        version_id=version_id,
    ) as step:
        step("activating")
        with Graph(settings.memgraph_url) as graph:
            for document_id, row in rows.items():
                proj.activate(
                    graph,
                    proj.VersionNode(
                        library=library_id,
                        source_key=row.source_key,
                        content_sha256=version.content_sha256,
                        title=row.title,
                        tenant_id=tenant_id,
                    ),
                )

    log.info("activated %s for %s", version_id, ", ".join(documents))
    return Activation(version_id=version_id, documents=documents)
