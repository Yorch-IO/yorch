"""Packaging what a run indexed as a book, as activities.

Two on the pipeline path and one standalone, and the split between the first two
is the retry policy rather than tidiness: `resolve_book_metadata` makes a
provider call and gets `_PAID_RETRY`'s two attempts, `build_epub` reads a file
and writes one and gets three. Folding them together would give a re-run of the
packaging a second chance to re-buy the metadata.

Every one of them `await asyncio.to_thread(...)`. Not optional and not a
precaution: six activities in this worker were `async def` around a synchronous
network call and held the only event loop for the length of a document — which
meant the heartbeat they recorded could never be *sent*, because the call that
recorded it was holding the loop that had to send it. Reading a book's chunks
off disk and zipping them is not a network call, but it is seconds of CPU on the
loop that serves every other workflow's queries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import booking, bookexport, bookmeta, config
from ..artifacts import ArtifactRef
from ..catalog import Catalog
from ..pipeline import Registered, Spend

log = logging.getLogger(__name__)

#: Bookkeeping must never stall a stage behind a catalog that is merely down.
#: See `activities/ingest._record`.
RECORD_TIMEOUT = 3.0


def _settings() -> config.Settings:
    return config.load()


def _document(settings: config.Settings, registered: Registered):
    """The catalog's row for this document, or `None` if it could not be read.

    `pooled=False` for the reason every best-effort write here uses it: a pool
    retries a refused connection in the background, so a catalog that is merely
    down turns one immediate error into a ten-second stall — per activity, on a
    stage that has a perfectly good answer without it.
    """
    try:
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            return catalog.document(
                registered.document_id, tenant_id=registered.tenant_id
            )
    except Exception as e:  # noqa: BLE001 — a nameless book beats no book
        log.warning("could not read %s: %s", registered.document_id, e)
        return None


def _language(settings: config.Settings, library_id: str, tenant_id: str) -> str:
    try:
        with Catalog(
            settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
        ) as catalog:
            return catalog.library_language(library_id, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — `dc:language` drives hyphenation, not truth
        return "es"


def _refuse(e: booking.ExportError) -> ApplicationError:
    """Carry the `kind` across the Temporal boundary.

    `kind` is what `app/src/lib/api.ts` keys its guidance on, so flattening it
    into a message would cost the UI its ability to offer a fix. Non-retryable
    because every kind this raises is a statement about what exists — and, since
    the lookup is tenant-scoped, also about who is asking. Neither answer
    changes on a second attempt.
    """
    return ApplicationError(str(e), e.kind, type="ExportError", non_retryable=True)


@activity.defn(name="resolve_book_metadata")
async def resolve_book_metadata(
    run_id: str, registered: Registered, chunks: ArtifactRef
) -> Spend | None:
    """A title and an author for a document the catalog has neither for.

    Returns `None` when nothing was asked, which is the ordinary case: it runs
    once per document *ever*, and never at all for a video, because
    `register_video` already fills the author from the channel.

    The decision is made here rather than in the workflow on purpose. A workflow
    cannot read the catalog, so hoisting it would mean a query activity whose
    only job is to answer whether to run this one — two round trips to save one.
    """
    settings = _settings()
    document = _document(settings, registered)
    if document is None:
        # Not knowing is not a reason to spend. A catalog that cannot be read
        # cannot say whether this document already has an author, and paying to
        # find out would re-buy the answer on every run of a blinking stack.
        log.warning("no document row for %s; asking nothing", registered.document_id)
        return None

    stem = booking.filename_title(document.source_key)
    if not booking.needs_metadata(document.author, document.title, stem):
        return None
    if not settings.gemini.configured:
        return None

    from ..providers import Provider

    rows = await asyncio.to_thread(
        bookexport.read_rows, settings.workspace, run_id, chunks
    )
    title, author, spend = await asyncio.to_thread(
        bookmeta.read_metadata,
        Provider(settings.gemini),
        bookexport.excerpt(rows, bookmeta.METADATA_CHARS),
    )
    if title or author:
        try:
            with Catalog(
                settings.database_url, pooled=False, timeout=RECORD_TIMEOUT
            ) as catalog:
                catalog.fill_document_metadata(
                    document.id,
                    title=title,
                    author=author,
                    filename_title=stem,
                    tenant_id=registered.tenant_id,
                )
        except Exception as e:  # noqa: BLE001 — the book is worth more than the row
            log.warning("could not store the metadata of %s: %s", document.id, e)
    booking.charge_metadata(settings.database_url, run_id, spend)
    return spend


@activity.defn(name="build_epub")
async def build_epub(
    run_id: str, library_id: str, registered: Registered, chunks: ArtifactRef
) -> ArtifactRef:
    """Package this run's chunks as an EPUB, and record it.

    Reads the title and the author from the catalog rather than taking them as
    parameters, so that whatever is current wins: the metadata activity may have
    just written them, a person may have edited them, or neither may have
    happened and the filename stem stands. One source of truth, read at the last
    possible moment.
    """
    settings = _settings()
    document = _document(settings, registered)
    language = _language(settings, library_id, registered.tenant_id)

    # A catalog that could not be read costs the book its title, not its
    # existence: `epub.build` falls back to a generic chapter name, and the file
    # a reader downloads is still the document they indexed.
    title = document.title if document else ""
    author = document.author if document else None

    def _build() -> ArtifactRef:
        rows = bookexport.read_rows(settings.workspace, run_id, chunks)
        return bookexport.write_book(
            settings.workspace,
            run_id,
            rows,
            version_id=registered.version_id,
            title=title,
            author=author,
            language=language,
        )

    ref = await asyncio.to_thread(_build)
    booking.record_book(settings, run_id, ref)
    return ref


@activity.defn(name="export_version_epub")
async def export_version_epub(
    library_id: str, document_id: str, version_id: str, tenant: str
) -> dict[str, Any]:
    """Build a book for a version indexed before the stage existed.

    Thin, like `promote_version`: everything that matters lives in
    `booking.py` so that this plane and the paid one cannot drift. The tenant is
    positional and has no default — a defaulted one is what made activation
    unreachable for every organisation but the legacy one, and Temporal maps
    payloads onto parameters by arity, so a default here is also a parameter a
    caller can silently fail to fill.
    """
    workflow_id = _current_workflow_id()
    try:
        export = await asyncio.to_thread(
            lambda: booking.export_for_version(
                library_id,
                document_id,
                version_id,
                tenant_id=tenant,
                run_id=workflow_id,
            )
        )
    except booking.ExportError as e:
        raise _refuse(e) from e
    log.info("exported %s of %s for %s", version_id, document_id, tenant)
    return export.as_dict()


def _current_workflow_id() -> str | None:
    from .. import audit

    return audit.current_workflow_id()
