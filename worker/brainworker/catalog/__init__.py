"""The Postgres catalog: what exists, where it came from, and what it cost."""

# `migrate` is gone: Prisma owns this schema now, in the yorch-tauri-backend
# checkout beside this one.
# What is left is the assertion that replaced it, and a test-only applier.
from .migrations import (
    MigrationError,
    apply_migrations,
    current_version,
    require_schema,
)
from .repo import (
    Catalog,
    CatalogError,
    Cost,
    Document,
    DuplicateContent,
    LibraryOwnedByAnother,
    ProjectTotals,
    Run,
    RunEvent,
    RunSummary,
    Version,
)

__all__ = [
    "Catalog",
    "CatalogError",
    "Cost",
    "Document",
    "DuplicateContent",
    "LibraryOwnedByAnother",
    "MigrationError",
    "ProjectTotals",
    "Run",
    "RunEvent",
    "RunSummary",
    "Version",
    "apply_migrations",
    "current_version",
    "require_schema",
]
