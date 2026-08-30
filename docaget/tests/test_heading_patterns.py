"""The learned rule for headings that carry no number.

`chunk.HEADING_RE` requires a leading number, so a book numbering its parts in
words ("LIBRO PRIMERO") indexes with no table of contents at all — observed on a
real document, not theorised. These tests pin the rule that fixes it and, just as
importantly, pin that it changes nothing when it was not learned.
"""

from __future__ import annotations

import pytest

from docagent import rules as rl
from docagent.chunk import ChunkRules, DocRules, build_chunks, heading_level, split_paragraphs
from docagent.extract import short_line_candidates

_PROSE = [
    "El conocimiento de Dios y el de nosotros mismos son cosas que van juntas, "
    "y no es fácil discernir cuál precede a cuál ni cuál engendra a la otra.",
    "Con razón se dice que el hombre es un abismo para sí mismo, porque no puede "
    "mirarse sin volver enseguida los ojos hacia aquel que lo hizo.",
    "Ninguno puede mirarse a sí mismo sin que de inmediato vuelva los sentidos a "
    "la consideración de Dios, en quien vive y se mueve y tiene su ser.",
    "La miseria del hombre es el espejo en que se contempla la bondad divina, y "
    "quien no ha visto la primera difícilmente reconocerá la segunda.",
    "De aquí nace aquel horror y espanto con que las Escrituras nos enseñan que "
    "los santos eran heridos siempre que sentían la presencia de Dios.",
    "Porque los hombres nunca reconocen bastante cuán viles son hasta que se han "
    "comparado con la majestad de Dios, y no antes.",
]

#: Three unnumbered headings among eighteen paragraphs — about 14%, the shape a
#: real book has. An earlier version of this fixture had three headings in ten
#: paragraphs, which the degeneracy ceiling correctly rejected as noise.
BOOK = "\n\n".join(
    [
        "LIBRO PRIMERO",
        *_PROSE,
        "LIBRO SEGUNDO",
        *_PROSE,
        "LIBRO TERCERO",
        *_PROSE,
        "1. Defina qué entiende el autor por regeneración",
        "2. Señale los frutos que menciona el capítulo",
    ]
).encode("utf-8")

L1 = r"^LIBRO\s+\w+$"


def _lines() -> list[str]:
    return [p.text for p in split_paragraphs(BOOK)]


# --- the chunker ------------------------------------------------------------


def test_without_a_learned_pattern_an_unnumbered_heading_is_prose():
    """The defect, stated as a test so the fix has something to beat."""
    assert heading_level("LIBRO PRIMERO", ChunkRules()) == 0
    chunks = build_chunks(BOOK, split_paragraphs(BOOK))
    assert not any(c.chapter or c.section for c in chunks)


def test_a_learned_pattern_produces_a_table_of_contents():
    rules = ChunkRules(heading_l1_pattern=L1)
    assert heading_level("LIBRO PRIMERO", rules) == 1
    chapters = {c.chapter for c in build_chunks(BOOK, split_paragraphs(BOOK), rules)}
    assert {"LIBRO PRIMERO", "LIBRO SEGUNDO", "LIBRO TERCERO"} <= chapters


def test_the_review_question_guard_still_runs_first():
    """A numbered imperative must never become a heading, learned rule or not.

    This is the guard that once let a year into the chapter sequence, and the
    learned patterns are checked *after* it precisely so it keeps winning.
    """
    rules = ChunkRules(heading_l1_pattern=r"^\d+\.\s+\w")
    assert heading_level("11. Defina Arrianismo", rules) == 0


def test_a_learned_pattern_still_obeys_the_length_guard():
    long_line = "LIBRO PRIMERO de la creación del mundo y de su gobierno entero"
    rules = ChunkRules(heading_l1_pattern=L1, heading_l1_max=40)
    assert len(long_line) > 40
    assert heading_level(long_line, rules) == 0


def test_the_numbered_path_is_untouched_by_a_learned_pattern():
    rules = ChunkRules(heading_l1_pattern=L1)
    assert heading_level("2.1 Título", rules) == 2
    assert heading_level("2.1 Título", ChunkRules()) == 2


# --- the validator ----------------------------------------------------------


def _validate(**kw) -> rl.Validation:
    return rl.validate(rl.Proposal(**kw), BOOK, _evidence())


class _evidence:
    repeated_lines: dict = {}
    numbered_lines: list = []
    first_lines: list = []
    last_lines: list = []
    pages = 1
    page_height = 0.0
    numbered_paragraphs: list = []
    short_lines: list = []


def test_a_declined_pattern_passes_trivially():
    v = _validate()
    assert "heading_patterns" not in v.failed_rules()


def test_a_pattern_matching_one_line_is_rejected():
    v = _validate(heading_l1_pattern=r"^LIBRO PRIMERO$")
    assert "heading_patterns" in v.failed_rules()
    assert any("una sola vez" in f for f in v.feedback)


def test_an_over_broad_pattern_is_rejected():
    v = _validate(heading_l1_pattern=r"^.")
    assert "heading_patterns" in v.failed_rules()


def test_a_pattern_stealing_review_questions_is_rejected():
    v = _validate(heading_l1_pattern=r"^\d+\.\s", heading_l1_max=200)
    assert "heading_patterns" in v.failed_rules()
    assert any("preguntas de repaso" in f for f in v.feedback)


def test_a_good_pattern_passes_and_reports_its_matches():
    v = _validate(heading_l1_pattern=L1)
    assert "heading_patterns" not in v.failed_rules()
    assert any("3 encabezados sin numerar" in f.detail for f in v.findings)


def test_an_uncompilable_pattern_fails_rather_than_raising():
    v = _validate(heading_l1_pattern=r"^LIBRO (\w+$")
    assert "heading_patterns" in v.failed_rules()


# --- evidence ---------------------------------------------------------------


def test_short_line_candidates_offer_the_headings_and_skip_numbered_ones():
    cands = short_line_candidates(BOOK)
    assert "LIBRO PRIMERO" in cands and "LIBRO TERCERO" in cands
    # Numbered lines are already covered by HEADING_RE; showing them to the
    # learner invites a pattern that duplicates the built-in detector.
    assert not any(c.startswith(("1.", "2.")) for c in cands)
    # Prose is too long to be a heading candidate.
    assert not any(len(c) > 70 for c in cands)


def test_short_line_candidates_deduplicate():
    doubled = BOOK + b"\n\nLIBRO PRIMERO"
    assert short_line_candidates(doubled).count("LIBRO PRIMERO") == 1


# --- adoption ---------------------------------------------------------------


def test_adopt_keeps_passing_rules_and_drops_only_the_failed_one():
    """Partial adoption. Requiring every rule to pass once threw away a header
    pattern that had validated cleanly three times."""
    proposal = rl.Proposal(
        header_patterns=(r"^CULTURA$",),
        heading_l1_max=44,
        heading_l1_pattern=L1,
        question_pattern=r"^\d+\.\s",
        footnote_pattern=r"^\d+\s",
    )
    v = rl.Validation(findings=[rl.Finding("footnote_pattern", False, "degenerada")])
    p = rl.adopt(
        proposal, v, fingerprint="ff", slug="s", extractor="pdf_text", learned_from="x.pdf"
    )
    assert p.doc_rules.header_patterns == (r"^CULTURA$",)
    assert p.chunk_rules.heading_l1_pattern == L1
    assert p.chunk_rules.heading_l1_max == 44
    assert p.question_pattern == r"^\d+\.\s"
    assert p.footnote_pattern is None
    assert rl.adopted_rules(v) == ["header_patterns", "heading_guards", "heading_patterns", "question_pattern"]


@pytest.mark.parametrize(
    "failed,attr,expected",
    [
        ("header_patterns", "doc_rules", DocRules()),
        ("heading_guards", "chunk_rules", ChunkRules()),
    ],
)
def test_a_failed_essential_rule_falls_back_to_the_measured_default(failed, attr, expected):
    proposal = rl.Proposal(header_patterns=(r"^X$",), heading_l1_max=999)
    v = rl.Validation(findings=[rl.Finding(failed, False, "mala")])
    p = rl.adopt(
        proposal, v, fingerprint="ff", slug="s", extractor="pdf_text", learned_from="x.pdf"
    )
    assert getattr(p, attr) == expected


def test_a_failed_heading_pattern_drops_only_the_pattern():
    """It is optional, so the length guards it travelled with must survive."""
    proposal = rl.Proposal(heading_l1_max=44, heading_l1_pattern=r"^.")
    v = rl.Validation(findings=[rl.Finding("heading_patterns", False, "demasiado amplia")])
    p = rl.adopt(
        proposal, v, fingerprint="ff", slug="s", extractor="pdf_text", learned_from="x.pdf"
    )
    assert p.chunk_rules.heading_l1_pattern is None
    assert p.chunk_rules.heading_l1_max == 44


def test_the_guards_do_not_block_a_document_that_numbers_nothing():
    """`heading_guards` is essential, so one FAIL discards the whole proposal.

    A document numbering its parts in words has no numbered heading for the
    guards to admit and never will, so failing there would block adoption of the
    one rule that gives it a table of contents — and would ask the model to raise
    a cap that is not the problem.
    """
    v = _validate(heading_l1_pattern=L1)
    assert "heading_guards" not in v.failed_rules()
    assert v.passed
    assert any("unexercised" in f.detail for f in v.findings)


def test_the_guards_still_fail_when_nothing_supplies_the_structure():
    """Only a proposed pattern earns the pass above. Without one, a guard that
    admits no chapters is the same defect it always was."""
    v = _validate(heading_l1_max=5)
    assert "heading_guards" in v.failed_rules()
    assert any("heading_l1_pattern" in f for f in v.feedback)


def test_the_guards_free_pass_depends_on_the_pattern_surviving():
    """The pass above is granted on the grounds that the learned pattern gives
    the document its structure. A pattern that was itself rejected gives it
    none, so the grounds evaporate and the guards must fail with feedback that
    names the real problem — not "raise heading_l1_max", which cannot help a
    document with no numbers in it."""
    v = _validate(heading_l1_pattern=r"^.")
    assert "heading_patterns" in v.failed_rules()
    assert "heading_guards" in v.failed_rules()
    assert any("short_lines" in f for f in v.feedback)


# --- when a heading pattern is worth another paid call ----------------------


def test_a_rejected_pattern_blocks_when_nothing_else_can_give_an_outline():
    """`heading_patterns` is optional in the module's static table and that was
    a real defect on documents that number nothing.

    Observed on a real run: the model proposed a pattern matching prose, the
    checker refused it and wrote precise feedback — "it is taking review
    questions; exclude them" — and because nothing *essential* had failed, that
    feedback was never sent back. One attempt, pattern dropped, document indexed
    with no table of contents. The rule is the only thing that can produce an
    outline here, so failing it has to earn the refine round.
    """
    v = _validate(heading_l1_pattern=r"^\d+\.\s", heading_l1_max=200)
    assert "heading_patterns" in v.failed_rules()
    assert "heading_patterns" in v.essential
    assert not v.passed, "a refine round must be earned"


def test_the_same_failure_is_forgiven_on_a_document_that_numbers_its_headings():
    """The other half of the same judgement. Where the numeric detector already
    produces an outline, a rejected pattern costs nothing and is not worth
    another paid call — which is what `OPTIONAL_RULES` is for."""
    numbered = "\n\n".join(
        ["1. Del conocimiento de Dios", *_PROSE, "2. Del conocimiento del hombre", *_PROSE]
    ).encode("utf-8")
    proposal = rl.Proposal(heading_l1_pattern=r"^[A-Z]", heading_l1_max=40)
    v = rl.validate(proposal, numbered, _evidence())
    assert "heading_patterns" in v.failed_rules()
    assert "heading_patterns" not in v.essential
    assert v.passed, "the numeric detector already gives this document an outline"


def test_a_pattern_that_validates_leaves_the_promotion_harmless():
    """Promotion only bites on failure. A good pattern passes either way."""
    v = _validate(heading_l1_pattern=L1)
    assert "heading_patterns" in v.essential
    assert v.passed


def test_the_pattern_the_prompt_recommends_survives_validation():
    """The prompt tells the model to key on the absence of sentence punctuation
    and to use a *negated* character class. A previous run's proposal admitted
    `¿?` among the allowed characters and was refused for taking review
    questions, which have the same shape as a title — so the guidance has to be
    checkable, not just plausible."""
    for pattern in (r"^[^.?¿!]{3,70}$", r"^[A-ZÁÉÍÓÚÑ][^.?¿!]{3,60}$"):
        v = _validate(heading_l1_pattern=pattern, heading_l1_max=70)
        assert "heading_patterns" not in v.failed_rules(), pattern
        assert v.passed, pattern


# --- printer's signature lines ----------------------------------------------


def test_a_run_of_page_numbers_is_not_a_chapter():
    """Measured on 02-PuertasEternas_INT.pdf, which carries its printing
    signature on its own line in the front matter.

    `HEADING_RE` sees a leading number and the length cap sees 27 characters,
    well under `heading_l1_max`, so the line read as chapter 12 and became the
    breadcrumb of 13 chunks — the epigraph, the whole introduction and its four
    named sections (ALDABA, QUICIO, DINTEL, UMBRAL). Nothing failed: the chunks
    indexed and retrieved correctly, they just answered under a chapter that does
    not exist.
    """
    assert heading_level("12 13 14 15 16 v6 5 4 3 2 1", ChunkRules()) == 0


def test_a_numbered_heading_keeps_its_level_when_it_carries_a_title():
    """The guard must not cost the ordinary case, including the tightest one it
    lets through: "2.1.1 Foo" is three digits against three letters."""
    assert heading_level("1. Introducción", ChunkRules()) == 1
    assert heading_level("2.1.1 Foo", ChunkRules()) == 3
    assert heading_level("12. La puerta de las ovejas", ChunkRules()) == 1
