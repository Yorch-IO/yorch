"""Which real document the property-based tests run against.

Several test modules assert *properties* of the chunker on real prose — spans
are byte-exact, no chunk mixes kinds, an eval item still finds its passage after
a re-chunk. They were written against ``../sociologia/output_corrected_peluquiado.txt``,
the Go pipeline's output, and skipped themselves when it was absent. In a
checkout without that file, twenty tests silently did nothing — including the
ones guarding invariant #1, which is the mistake the whole project is built to
avoid repeating.

Those properties hold for *any* sufficiently large UTF-8 prose document, so the
resolver falls back to the largest corrected text in ``libros/``. What it must
never do is choose silently: a property test that fails is only actionable if
you know which document produced it, so ``pytest_report_header`` prints the
selection on every run.

``test_port_fidelity.py`` deliberately does **not** use this. Its assertions are
exact counts measured from the Go implementation (328 chunks, kinds 309/10/9),
so it is meaningful against that one file and meaningless against any other.
"""

from __future__ import annotations

import os
import pathlib

ENV_OVERRIDE = "DOCAGENT_TEST_CORPUS"

REPO = pathlib.Path(__file__).resolve().parents[2]

#: The Go pipeline's output. Preferred when present, so a checkout that has it
#: behaves exactly as before this module existed.
GO_REFERENCE = REPO / "sociologia" / "output_corrected_peluquiado.txt"

#: Corrected texts left behind by real indexing runs.
LOCAL_CORPUS_GLOB = "libros/**/*.corrected.txt"

#: Below this, the byte-versus-character divergence test has no chunk far
#: enough into the document to distinguish the two indexings.
MIN_USEFUL_BYTES = 120_000


def _local_candidates() -> list[pathlib.Path]:
    docaget = pathlib.Path(__file__).resolve().parents[1]
    found = [p for p in docaget.glob(LOCAL_CORPUS_GLOB) if p.is_file()]
    # Largest first, then by name: ties must not depend on filesystem order, or
    # a failure on one machine is unreproducible on another.
    return sorted(found, key=lambda p: (-p.stat().st_size, p.name))


def override_problem() -> str | None:
    """Why an explicitly requested corpus cannot be used, if one was requested.

    An override that is missing or too small must fail the session loudly.
    Falling back silently would run the suite against a different document than
    the one asked for; skipping silently would hide that the request was
    ignored. Both turn a typo into a confusing test failure somewhere else.
    """
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if not override:
        return None
    path = pathlib.Path(override)
    if not path.is_file():
        return f"{ENV_OVERRIDE}={override!r} is not a file"
    size = path.stat().st_size
    if size < MIN_USEFUL_BYTES:
        return (
            f"{ENV_OVERRIDE}={override!r} is {size:,} bytes; at least "
            f"{MIN_USEFUL_BYTES:,} are needed for the byte-versus-character "
            "divergence test to have a chunk far enough into the document"
        )
    return None


def resolve() -> pathlib.Path | None:
    """The document to run property tests against, or None if there is none."""
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        path = pathlib.Path(override)
        # An unusable override is reported by override_problem(), which fails
        # the session in conftest before any test runs.
        return path if path.is_file() else None

    if GO_REFERENCE.is_file():
        return GO_REFERENCE

    for candidate in _local_candidates():
        if candidate.stat().st_size >= MIN_USEFUL_BYTES:
            return candidate
    return None


def is_go_reference() -> bool:
    """Whether the resolved corpus is the Go pipeline's own output.

    A few assertions are facts about that document rather than properties of the
    code — for instance that a particular question pattern validates cleanly on
    it. On a book whose numbered headings look like numbered review questions,
    the validator rejects the same pattern, and it is *right* to. Those tests
    are marked ``reference_corpus`` and skip elsewhere.
    """
    return resolve() == GO_REFERENCE


def describe() -> str:
    """One line naming the corpus in use, for the pytest header."""
    chosen = resolve()
    if chosen is None:
        return (
            f"corpus: NONE — property tests will skip. Set {ENV_OVERRIDE} to a "
            f"UTF-8 prose file of at least {MIN_USEFUL_BYTES:,} bytes."
        )
    kind = "Go reference" if chosen == GO_REFERENCE else "local substitute"
    return f"corpus: {chosen.name} ({chosen.stat().st_size:,} bytes, {kind})"
