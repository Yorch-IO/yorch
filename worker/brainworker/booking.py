"""Building a book for a version that was indexed before books existed.

The on-demand half. The pipeline's own `epub` stage covers everything imported
from now on; this covers the seventy-odd versions already in the catalog, and
the case a person wants a book for a document imported without the switch.

**It costs nothing but the metadata call, and usually not even that.** The
chunks were paid for when the document was indexed and nothing is re-embedded,
re-corrected or re-extracted — the same argument `activation.py` makes for
itself, arriving from the other direction: the expensive work is already done
and what is missing is a last, free step.

A module beside the pipeline rather than logic inside a route, for the reason
`removal.py` and `activation.py` both give: the rule belongs in one place so no
caller can get it wrong, and a second implementation in TypeScript would be a
second place for the two to drift. Unlike those two, **both planes reach this
through a workflow** — it writes an artifact and it can spend, and every
artifact writer and every charge in this codebase lives in an activity where
`_record` and `_charge` can derive the tenant from the run row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import bookexport, bookmeta, config
from .artifacts import ArtifactRef
from .audit import audited, mint_run_id
from .catalog import Catalog
from .pipeline import Spend

log = logging.getLogger(__name__)


class ExportError(RuntimeError):
    """Something the caller can act on, carrying the kind the UI keys advice on."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Export:
    version_id: str
    run_id: str
    artifact: str
    bytes: int
    title: str
    author: str | None
    #: What the metadata call cost, or `None` when it was not made — which is
    #: the ordinary case, because it runs once per document ever.
    spend: Spend | None = None

    def as_dict(self) -> dict:
        return {
            "version_id": self.version_id,
            "run_id": self.run_id,
            "artifact": self.artifact,
            "bytes": self.bytes,
            "title": self.title,
            "author": self.author,
            "usd": self.spend.usd if self.spend else None,
        }


def needs_metadata(author: str | None, title: str, filename_title: str) -> bool:
    """Whether a model could tell this document anything it does not know.

    Two placeholders, and the test for each is exact rather than heuristic. The
    author is a placeholder when it is NULL, which it is for every document on
    this installation — the column has never been written. The title is one
    while it is still *precisely* the stem the import derived it from, computed
    by the caller with the same `PurePosixPath(source_key).stem` `stage_source`
    used. Anything else is a value somebody chose, and paying a model to
    second-guess it is the failure this predicate exists to prevent.
    """
    return author is None or title == filename_title


def filename_title(source_key: str) -> str:
    """The title an import would have derived from this document's own key.

    The same derivation as `activities.ingest.stage_source`, and imported from
    nowhere because that one is buried inside an activity that stages a file.
    Two spellings of one rule is exactly the drift this comment exists to flag
    if either moves.
    """
    import pathlib

    return pathlib.PurePosixPath(source_key).stem or source_key


def export_for_version(
    library_id: str,
    document_id: str,
    version_id: str,
    *,
    tenant_id: str,
    run_id: str | None = None,
) -> Export:
    """Build a book for one already-indexed version.

    404-shaped failures rather than 403, the decision every id-addressed route
    here holds to: a version that is another organisation's and one that was
    never real must be indistinguishable, or an id somebody guessed is confirmed
    by the error it earns.
    """
    settings = config.load()
    # Minted here rather than left to `audited`, because the id is also the
    # artifact directory: the book has to land under the run that records it or
    # `GET /runs/{id}` resolves a row pointing at a file in someone else's
    # directory. The caller passes the workflow's own id, which is what makes
    # the run reachable by the id the client already holds.
    run_id = run_id or mint_run_id("epub")
    with audited(
        settings,
        kind="epub",
        run_id=run_id,
        tenant_id=tenant_id,
        document_id=document_id,
        version_id=version_id,
    ) as step:
        step("epub")
        with Catalog(settings.database_url) as catalog:
            document = catalog.document(
                document_id, library_id=library_id, tenant_id=tenant_id
            )
            if document is None:
                raise ExportError(
                    f"no existe {document_id!r} en {library_id!r}",
                    kind="document_not_found",
                )
            if not any(v.id == version_id for v in catalog.versions_of(document_id)):
                raise ExportError(
                    f"no existe la versión {version_id!r} en {library_id!r}",
                    kind="version_not_found",
                )
            source_run = bookexport.source_run_for(catalog, version_id)
            chunks = bookexport.chunks_ref(catalog, source_run) if source_run else None
            if chunks is None:
                # The same shape `can_rebuild` reports: what is missing is the
                # artifact a successful run should have left, and the remedy is
                # a re-index rather than a retry of this.
                raise ExportError(
                    f"la versión {version_id!r} no conserva sus fragmentos, "
                    "así que no hay con qué componer el libro",
                    kind="epub_source_missing",
                )
            language = catalog.library_language(library_id, tenant_id=tenant_id)

        rows = bookexport.read_rows(settings.workspace, source_run, chunks)
        spend = _fill_metadata(
            settings,
            catalog_url=settings.database_url,
            document=document,
            rows=rows,
            run_id=run_id,
            tenant_id=tenant_id,
        )

        title, author = document.title, document.author
        if spend is not None:
            with Catalog(settings.database_url) as catalog:
                refreshed = catalog.document(document_id, tenant_id=tenant_id)
            if refreshed is not None:
                title, author = refreshed.title, refreshed.author

        ref = bookexport.write_book(
            settings.workspace,
            run_id,
            rows,
            version_id=version_id,
            title=title,
            author=author,
            language=language,
        )
        record_book(settings, run_id, ref)
        return Export(
            version_id=version_id,
            run_id=run_id,
            artifact=ref.path,
            bytes=ref.bytes,
            title=title,
            author=author,
            spend=spend,
        )


def _fill_metadata(
    settings: config.Settings,
    *,
    catalog_url: str,
    document,
    rows: list[dict],
    run_id: str,
    tenant_id: str,
) -> Spend | None:
    """Ask the model for a title and an author, once, and only if it can help.

    Returns the spend, or `None` when nothing was asked — which a caller must
    not read as "it was free": a stage that ran for nothing and a stage that did
    not run are different facts, and the activity above books the first as a
    zero row for exactly that reason.
    """
    stem = filename_title(document.source_key)
    if not needs_metadata(document.author, document.title, stem):
        return None
    if not settings.gemini.configured:
        log.info("no provider configured; the book keeps the catalog's own title")
        return None

    from .providers import Provider

    title, author, spend = bookmeta.read_metadata(
        Provider(settings.gemini), bookexport.excerpt(rows, bookmeta.METADATA_CHARS)
    )
    if title or author:
        try:
            with Catalog(catalog_url, pooled=False) as catalog:
                catalog.fill_document_metadata(
                    document.id,
                    title=title,
                    author=author,
                    filename_title=stem,
                    tenant_id=tenant_id,
                )
        except Exception as e:  # noqa: BLE001 — the book is worth more than the row
            log.warning("could not store the metadata of %s: %s", document.id, e)
    charge_metadata(catalog_url, run_id, spend)
    return spend


def charge_metadata(catalog_url: str, run_id: str, spend: Spend | None) -> None:
    """Book what the metadata call cost. Best-effort, like every other charge.

    Losing the record is bad; failing the export that already spent the money is
    worse, because the retry spends it again — the reason `paid.py::_charge`
    gives for swallowing its own failure, and it holds here unchanged.
    """
    if spend is None:
        return
    try:
        with Catalog(catalog_url, pooled=False) as catalog:
            catalog.record_cost(
                run_id,
                stage=spend.stage,
                provider="vertex",
                model=spend.model,
                input_tokens=spend.input_tokens,
                output_tokens=spend.output_tokens,
                usd=spend.usd,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("could not record spend for %s: %s", run_id, e)


def record_book(settings: config.Settings, run_id: str, ref: ArtifactRef) -> None:
    """Best-effort, like every other artifact write. See `activities.ingest._record`."""
    try:
        with Catalog(settings.database_url, pooled=False) as catalog:
            catalog.record_artifact(
                run_id,
                name=ref.kind,
                rel_path=ref.path,
                sha256=ref.sha256,
                size_bytes=ref.bytes,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("could not record the book for %s: %s", run_id, e)
