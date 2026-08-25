"""The Postgres catalog: what exists, where it came from, and what it cost."""

# The module is `migrations`, not `migrate`, so that `catalog.migrate` is
# unambiguously the function. Re-exporting a function under its own module's
# name shadows the module, and the resulting `AttributeError: 'function' object
# has no attribute 'migrate'` points at the caller rather than at the cause.
from .migrations import MigrationError, current_version, migrate
from .repo import (
    Catalog,
    CatalogError,
    Cost,
    Document,
    DuplicateContent,
    ProjectTotals,
    Run,
    RunSummary,
    Version,
)

__all__ = [
    "Catalog",
    "CatalogError",
    "Cost",
    "Document",
    "DuplicateContent",
    "MigrationError",
    "ProjectTotals",
    "Run",
    "RunSummary",
    "Version",
    "current_version",
    "migrate",
]
