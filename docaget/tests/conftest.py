from __future__ import annotations

import pytest

import corpus

REFERENCE_MARKER = "reference_corpus"


def pytest_report_header() -> str:
    """Name the document the property tests ran against.

    Without this the suite can pass against a substitute corpus and nobody
    reading the output would know which one, which makes any failure
    unreproducible.
    """
    return corpus.describe()


def pytest_configure(config: pytest.Config) -> None:
    problem = corpus.override_problem()
    if problem:
        raise pytest.UsageError(problem)
    config.addinivalue_line(
        "markers",
        f"{REFERENCE_MARKER}: asserts a fact about the Go reference book, not a "
        "property of the code; skipped when running against a substitute corpus.",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if corpus.is_go_reference():
        return
    chosen = corpus.resolve()
    where = chosen.name if chosen else "no corpus"
    skip = pytest.mark.skip(
        reason=f"specific to the Go reference book; corpus in use is {where}"
    )
    for item in items:
        if REFERENCE_MARKER in item.keywords:
            item.add_marker(skip)
