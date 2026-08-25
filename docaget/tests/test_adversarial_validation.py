"""The validator must catch the bug that got past a human.

The original mistake: a rule for review questions was written as "numbered but
not a heading", checked with a script that required a dot after the number, and
declared clean. The real implementation made the dot optional, so it tagged nine
footnotes as questions. The error survived review and only surfaced when the
classifier's output was printed over the whole book.

These tests replay that exact situation. The buggy pattern must be **rejected**,
and the correct one **accepted** — and the rejection must come from an
independent signal, not from restating the regex.
"""

from __future__ import annotations

import pathlib

import pytest

from docagent.extract import Evidence
from docagent.rules import (
    Proposal,
    _looks_like_footnote,
    _looks_like_question,
    validate,
)

import corpus

# Validation is adversarial by design, so it needs a real document to be
# adversarial against — but not one specific document. See tests/corpus.py.
BOOK = corpus.resolve()

# The buggy rule: the dot after the number is OPTIONAL, which is what let
# "53 La palabra “nihilismo”…" through as a review question.
BUGGY_QUESTION_PATTERN = r"^\d+\.?\s"
# The rule that survived measurement: the dot is REQUIRED.
CORRECT_QUESTION_PATTERN = r"^\d+\.\s"
CORRECT_FOOTNOTE_PATTERN = r"^\d+\s"


@pytest.fixture(scope="module")
def book_bytes() -> bytes:
    if BOOK is None:
        pytest.skip("no corpus available")
    return BOOK.read_bytes()


@pytest.fixture
def evidence() -> Evidence:
    return Evidence(
        source=str(BOOK),
        extractor="plain",
        pages=1,
        repeated_lines={},
        numbered_lines=["1. Cultura y sociedad", "1.1. Occidente y los motivos"],
    )


def _validate(pattern_q, pattern_f, book_bytes, evidence):
    return validate(
        Proposal(
            header_patterns=(),
            heading_l1_max=40,
            heading_l2_max=120,
            question_pattern=pattern_q,
            footnote_pattern=pattern_f,
        ),
        book_bytes,
        evidence,
    )


def test_the_buggy_optional_dot_pattern_is_rejected(book_bytes, evidence):
    """This is the whole point of the module."""
    v = _validate(BUGGY_QUESTION_PATTERN, None, book_bytes, evidence)
    q = [f for f in v.findings if f.rule == "question_pattern"]
    assert q and not q[0].ok, v.notes()
    # And the rejection must name what actually went wrong, so a refine round has
    # something to act on.
    assert "independent signal" in q[0].detail
    assert v.feedback, "a rejection with no feedback cannot drive a refine round"


def test_the_correct_required_dot_pattern_is_accepted(book_bytes, evidence):
    v = _validate(
        CORRECT_QUESTION_PATTERN, CORRECT_FOOTNOTE_PATTERN, book_bytes, evidence
    )
    assert v.passed, v.notes()


@pytest.mark.reference_corpus
def test_rejection_is_driven_by_an_independent_signal_not_the_regex(book_bytes, evidence):
    """The validator must not simply re-run the proposed regex and agree with
    itself. Proof: the buggy and correct patterns differ only in one optional dot,
    yet they get opposite verdicts — which can only come from looking at what the
    matched text *is*."""
    buggy = _validate(BUGGY_QUESTION_PATTERN, None, book_bytes, evidence)
    good = _validate(CORRECT_QUESTION_PATTERN, None, book_bytes, evidence)
    assert "question_pattern" in buggy.failed_rules()
    assert "question_pattern" not in good.failed_rules()
    assert good.fully_passed


def test_degenerate_rules_are_rejected(book_bytes, evidence):
    """A rule matching everything has stopped discriminating; one matching almost
    nothing tells us nothing."""
    everything = _validate(r"^.", None, book_bytes, evidence)
    assert not everything.fully_passed
    assert any("degenerate" in f.detail for f in everything.findings)

    nothing = _validate(r"^ZZZ_NUNCA_APARECE", None, book_bytes, evidence)
    assert not nothing.fully_passed
    assert any("degenerate" in f.detail for f in nothing.findings)


# --- partial adoption --------------------------------------------------------


@pytest.mark.reference_corpus
def test_an_optional_rule_failing_does_not_block_adoption(book_bytes, evidence):
    """The bug this behaviour fixes: a header pattern that validated cleanly three
    times in a row was discarded because a degenerate footnote pattern failed
    beside it. The run then had no header pattern at all, leaving 175 running-header
    lines in the text — 552 paragraphs instead of 377."""
    evidence.repeated_lines = {"CULTURA, SOCIEDAD Y CRISTIANISMO": 175}
    v = validate(
        Proposal(
            header_patterns=(r"^CULTURA,\s+SOCIEDAD\s+Y\s+CRISTIANISMO$",),
            heading_l1_max=40,
            heading_l2_max=120,
            question_pattern=CORRECT_QUESTION_PATTERN,
            footnote_pattern=r"^ZZZ_DEGENERADO",  # fires on nothing
        ),
        book_bytes,
        evidence,
    )
    assert v.failed_rules() == {"footnote_pattern"}
    assert v.passed, "an optional failure must not block adoption"
    assert not v.fully_passed
    # The header pattern, which is what matters, survived.
    assert any(f.rule == "header_patterns" and f.ok for f in v.findings)


def test_an_essential_rule_failing_does_block_adoption(book_bytes, evidence):
    """Heading guards change what comes out of the extractor, so a failure there
    has to force a refine round rather than be silently accepted."""
    evidence.repeated_lines = {"CULTURA, SOCIEDAD Y CRISTIANISMO": 175}
    v = validate(
        Proposal(header_patterns=(), heading_l1_max=400, heading_l2_max=120),
        book_bytes,
        evidence,
    )
    assert "heading_guards" in v.failed_rules()
    assert not v.passed


def test_unanchored_header_pattern_is_rejected(book_bytes, evidence):
    """An unanchored header pattern strips in-body mentions of the same phrase —
    the reason header removal is content-based *and* anchored."""
    evidence.repeated_lines = {"CULTURA, SOCIEDAD Y CRISTIANISMO": 175}
    v = validate(
        Proposal(header_patterns=("CULTURA",), heading_l1_max=40, heading_l2_max=120),
        book_bytes,
        evidence,
    )
    assert not v.passed
    assert any("not anchored" in f.detail for f in v.findings)


def test_header_pattern_matching_no_real_repeat_is_rejected(book_bytes, evidence):
    """A pattern the model invented rather than derived from the evidence."""
    evidence.repeated_lines = {"CULTURA, SOCIEDAD Y CRISTIANISMO": 175}
    v = validate(
        Proposal(
            header_patterns=(r"^REVISTA\s+DE\s+FILOSOF[ÍI]A$",),
            heading_l1_max=40,
            heading_l2_max=120,
        ),
        book_bytes,
        evidence,
    )
    assert not v.passed
    assert any("matches none of the lines" in f.detail for f in v.findings)


def test_correct_header_pattern_is_accepted(book_bytes, evidence):
    evidence.repeated_lines = {"CULTURA, SOCIEDAD Y CRISTIANISMO": 175}
    v = validate(
        Proposal(
            header_patterns=(r"^CULTURA,\s+SOCIEDAD\s+Y\s+CRISTIANISMO$",),
            heading_l1_max=40,
            heading_l2_max=120,
        ),
        book_bytes,
        evidence,
    )
    assert v.passed, v.notes()


def test_too_loose_level1_guard_is_rejected(book_bytes, evidence):
    """With l1_max at 200 the review question "1. Defina qué es un metarrelato o
    metanarrativa" becomes a fifth chapter. The independent signal is the
    hierarchy's own coherence: more chapters than subsections is impossible."""
    v = validate(
        Proposal(header_patterns=(), heading_l1_max=400, heading_l2_max=120),
        book_bytes,
        evidence,
    )
    guards = [f for f in v.findings if f.rule == "heading_guards"]
    assert guards and not guards[0].ok, v.notes()


# --- the independent signals themselves --------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "1. ¿Cuáles son tal vez los motivos presentes en la cultura?",
        "1. Defina qué es un metarrelato o metanarrativa",
        "26. Señale algunos de los peligros de acoger sin reservas",
    ],
)
def test_question_signal_recognises_questions(text):
    assert _looks_like_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "2 Para ver una reseña del pensamiento y la dialéctica de Hegel, consultar el capítulo tercero",
        "3 Ver la reseña del pensamiento de Kierkegaard en el quinto capítulo de la conferencia",
    ],
)
def test_footnote_signal_recognises_footnotes(text):
    assert _looks_like_footnote(text)
    assert not _looks_like_question(text)
