"""Three defects found by running `index --dry-run` on `01_RetoDeDios_INT-S.pdf`,
a 304-page essay whose 30 chapters are titled "Capítulo N" and carry no number in
the heading itself.

Each test states what was actually observed on that document, not a hypothesis.
"""

import re

import pytest

from docagent.chunk import ChunkRules, DocRules, classify_kind, KIND_BODY, KIND_QUESTIONS
from docagent.ledger import Ledger
from docagent.rules import Finding, Proposal, Validation, adopt, validate


class _Ev:
    """Minimal stand-in for the extractor's evidence object."""

    def __init__(self, repeated: dict[str, int] | None = None):
        self.repeated_lines = repeated or {}
        self.pages = 304


# --- D3: a validated unnumbered heading pattern is discarded by heading_guards --


def test_a_validated_heading_pattern_survives_footnotes_that_look_numbered():
    """Observed on 01_RetoDeDios_INT-S.pdf, dry run of 2026-08-28.

    The validator agreed with the book's own table of contents:

        OK  heading_patterns: nivel 1: '^Capítulo\\s+\\d+$' -> 30 encabezados
        FAIL heading_guards: l1_max=30 yields 16 level-1 headings with
             duplicate numbers [2, 3, 4]

    The 16 are this publisher's footnotes ("2. Ibídem."), which restart at 1 in
    every chapter. `heading_guards` is essential, so one FAIL discarded a pattern
    that had just been checked against the document and found to explain 30
    headings — and the 304-page book was indexed with no outline at all.

    The numbered path is not what supplies this document's structure. When a
    learned pattern validated and explains strictly more level-1 headings than
    the numeric detector finds, the numeric sequence is measuring footnote noise
    and must not block adoption.
    """
    lines = ["El reto de Dios"]
    for chapter in range(1, 31):
        lines.append(f"Capítulo {chapter}")
        # Enough prose that the 30 headings stay under MAX_HIT_RATIO, as they do
        # on the real book: 30 of 1567 paragraphs, 1.9%.
        for para in range(12):
            lines.append(
                f"Cuerpo del capítulo {chapter}, párrafo {para}: prosa corriente "
                "que no es un encabezado ni una nota al pie."
            )
    # This publisher's footnotes: numbered, dotted, and restarting per chapter.
    lines += ["2. Ibídem.", "3. Ibídem.", "4. Ibídem.", "2. Tácito, Anales 15, 44."]

    p = Proposal(
        header_patterns=[r"^El\s+reto\s+de\s+Dios$"],
        heading_l1_pattern=r"^Capítulo\s+\d+$",
        heading_l1_max=30,
        heading_l2_max=60,
    )
    text = "\n\n".join(lines).encode("utf-8")
    v = validate(p, text, _Ev({"El reto de Dios": 128}))

    guards = [f for f in v.findings if f.rule == "heading_guards"]
    assert guards, "heading_guards must report something"
    assert guards[-1].ok, f"heading_guards blocked a validated pattern: {guards[-1].detail}"
    assert "heading_guards" not in v.failed_rules()
    assert v.passed


def test_the_numeric_sequence_still_blocks_when_it_explains_the_document():
    """The exemption above must not disarm the guard generally.

    A document whose real chapters *are* numbered, and whose learned pattern
    explains fewer headings than the numeric path, keeps the arithmetic check —
    this is the case `_check_heading_guards` was written for.
    """
    lines = []
    for chapter in [1, 2, 3, 1, 2]:  # review questions restarting the count
        lines.append(f"{chapter}. Un encabezado corriente")
    lines.append("Prólogo")

    p = Proposal(
        heading_l1_pattern=r"^Prólogo$",  # explains 1 heading, the numeric path 5
        heading_l1_max=40,
        heading_l2_max=60,
    )
    text = "\n\n".join(lines).encode("utf-8")
    v = validate(p, text, _Ev())

    assert "heading_guards" in v.failed_rules()


# --- D1: table-of-contents lines classified as review questions ----------------


def test_a_table_of_contents_line_is_not_a_review_question():
    """Observed: 99 of 161 paragraphs tagged `preguntas` were index lines.

    `heading_level` already refuses a TOC line (`TOC_LINE_RE`, five or more dot
    leaders) and returns 0. `classify_kind` took that 0 and handed the line
    straight to `NUMBERED_ITEM_RE` (`^\\d+\\.\\s`), which matches it. The guard
    existed and the classifier did not consult it.

    A TOC line tagged `preguntas` also resets the section path (invariant #11),
    so the breadcrumbs of everything after the index were wrong too.
    """
    r = ChunkRules()
    toc = "10. El «concordato evangélico»............................................. 105"
    assert classify_kind(toc, r) == KIND_BODY


def test_a_real_numbered_review_question_is_still_a_question():
    r = ChunkRules()
    assert classify_kind("11. Defina Arrianismo", r) == KIND_QUESTIONS


# --- D2: rhetorical prose classified as review questions by ¿ density ----------


def test_short_rhetorical_prose_is_not_a_review_question():
    """Observed: 62 paragraphs of essay prose tagged `preguntas`.

    `classify_kind`'s docstring records the measurement behind the threshold:
    body prose carries one ¿ per 800-4300 chars while a real question block has
    17 in 2054. That held on the reference book, whose paragraphs are long. This
    one is a rhetorical essay and PyMuPDF splits it at the line pitch, so a
    280-char paragraph with a single rhetorical ¿ cleared 1 mark per 300 chars.

    A block of review questions is recognisable by carrying *several* marks, not
    by carrying one in a short paragraph.
    """
    r = ChunkRules()
    prose = (
        "Al formar a la pareja humana, Dios le ordena la reproducción. ¿Por qué "
        "medio se haría? Obviamente, al tener Adán y Eva estructuras anatómicas "
        "bien diferenciadas, específicamente los órganos de reproducción."
    )
    assert len(prose) < 300 * 2
    assert classify_kind(prose, r) == KIND_BODY


def test_a_dense_block_of_questions_is_still_a_question_block():
    """The measured counterexample from the reference book must keep working."""
    r = ChunkRules()
    block = " ".join(f"¿Qué es el concepto número {i} de la lección?" for i in range(1, 18))
    assert classify_kind(block, r) == KIND_QUESTIONS


# --- D5: soft hyphens split words in the lexical index -------------------------


def test_a_soft_hyphen_does_not_split_a_word():
    """Observed: 2,140 U+00AD in 01_RetoDeDios_INT-S.pdf's extracted text.

    1,772 sit inside a word and 368 are followed by a space, both left there by
    the typesetter's discretionary hyphenation. `_clean_text` already joins the
    ASCII-hyphen form of exactly this (`HYPHEN_BREAK_RE`); it had never seen the
    soft-hyphen form.

    The damage is measurable and lands on the lexical leg:

        tokenize('la reproduc\\xadción humana') -> ['reproduc', 'cion', 'humana']
        tokenize('el propósi\\xadto divino')    -> ['proposi', 'divino']

    "propósito" loses its tail entirely, because the BM25 tokenizer drops tokens
    under 3 characters. Deterministic like the 174 PUA characters, and for the
    same reason: what the character means is not in doubt.
    """
    from docagent.bm25 import tokenize
    from docagent.extract.pdf_text import _clean_text

    assert _clean_text("la reproduc\xadción humana") == "la reproducción humana"
    assert _clean_text("reproduc\xad ción") == "reproducción"
    assert tokenize(_clean_text("el propósi\xadto divino")) == ["proposito", "divino"]


def test_a_rejected_level_2_pattern_does_not_discard_a_valid_level_1():
    """Observed on the second dry run of 01_RetoDeDios_INT-S.pdf.

    Both heading levels report under one rule name:

        OK   heading_patterns: nivel 1: '^Capítulo\\s+\\d+$' -> 30 encabezados
        FAIL heading_patterns: nivel 2: '^[A-ZÁÉÍÓÚÑ][^.?¿!]{3,60}$' -> 32.0%
        FAIL heading_guards: ... duplicate numbers [2, 3, 4]

    so `failed_rules()` cannot tell the two apart, and the level-2 pattern the
    model over-reached on poisoned a level-1 pattern that had validated. The
    same shape as the partial-adoption defect `Validation.passed` records, one
    level down.
    """
    lines = ["El reto de Dios"]
    for chapter in range(1, 31):
        lines.append(f"Capítulo {chapter}")
        for para in range(12):
            lines.append(
                f"Cuerpo del capítulo {chapter}, párrafo {para}: prosa corriente "
                "que no es un encabezado ni una nota al pie."
            )
    lines += ["2. Ibídem.", "3. Ibídem.", "4. Ibídem.", "2. Tácito, Anales 15, 44."]

    p = Proposal(
        header_patterns=[r"^El\s+reto\s+de\s+Dios$"],
        heading_l1_pattern=r"^Capítulo\s+\d+$",
        heading_l2_pattern=r"^[A-ZÁÉÍÓÚÑ].{3,200}$",  # over-reaches: matches the prose too
        heading_l1_max=30,
        heading_l2_max=60,
    )
    text = "\n\n".join(lines).encode("utf-8")
    v = validate(p, text, _Ev({"El reto de Dios": 128}))

    assert v.heading_levels_ok == {1}
    assert "heading_patterns" in v.failed_rules()  # level 2 really did fail
    assert "heading_guards" not in v.failed_rules()
    assert v.passed


def test_adoption_keeps_the_heading_level_that_validated():
    """The last layer of the same defect.

    `adopt` drops both heading patterns whenever the rule name `heading_patterns`
    appears in `failed_rules()`. On 01_RetoDeDios_INT-S.pdf that meant a level-1
    pattern the checker had confirmed against 30 headings was thrown out because
    the model had over-reached on level 2 — and the third dry run still ended
    with `dropped ['footnote_pattern', 'heading_patterns']` and a 304-page book
    with no outline, after `heading_guards` had already been fixed.

    Partial adoption is the repo's own measured rule; it just was not applied
    between the two heading levels.
    """
    p = Proposal(
        heading_l1_pattern=r"^Capítulo\s+\d+$",
        heading_l2_pattern=r"^[A-ZÁÉÍÓÚÑ].{3,200}$",
        heading_l1_max=30,
        heading_l2_max=60,
    )
    v = Validation()
    v.heading_levels_ok = {1}
    v.findings = [Finding("heading_patterns", False, "nivel 2: degenerado")]

    learned = adopt(
        p, v, fingerprint="f", slug="s", extractor="pdf_text", learned_from="x.pdf"
    )
    assert learned.chunk_rules.heading_l1_pattern == r"^Capítulo\s+\d+$"
    assert learned.chunk_rules.heading_l2_pattern is None


# --- the embedding model the project actually serves ---------------------------


def test_the_embedding_model_matches_the_collections_vector_space():
    """`vertex.py` pointed at a model this project does not serve.

    The constant carried its own measurement — "gemini-embedding-001 is no
    longer served, listing the endpoint's models on 2026-08-19 returned 23 ids"
    — and listing a model is not having access to it. Measured against the live
    endpoint on 2026-08-28, with ADC:

        yorch-platform-prod / gemini-embedding-2   -> HTTP 404
        yorch-platform-prod / gemini-embedding-001 -> HTTP 200
        verveux            / both                  -> HTTP 403

    A real run died there after paying $1.27 for correction and $0.05 for the
    eval set, with nothing indexed.

    Availability is the smaller half. `docagent_v2`'s 1,447 points were embedded
    with `gemini-embedding-001`, and both models are 3,072-wide — so Qdrant
    would have accepted vectors from the other one without a word, and a cosine
    between two models' embeddings means nothing. The failure mode is a healthy
    log over a corrupted ranking, which is the one this repository exists to
    refuse.
    """
    from docagent import vertex

    assert vertex.EMBED_MODEL == "gemini-embedding-001"
    assert vertex.EMBED_DIMS == 3072


# --- a rate-limit 429 is not a ReadTimeout ------------------------------------


def test_a_quota_429_is_given_time_to_refill(monkeypatch):
    """Observed indexing 01_RetoDeDios_INT-S.pdf: 600 chunks, one embedding call
    each, and the run stalled at `embedding 1/600` under 127 HTTP 429s.

    The quota is per minute and the burst drained it: measured immediately after
    killing that run, one serial request answered 200 and eight parallel ones
    all answered 200, while `embed_many` at six workers — and at *one* worker,
    11 of 12 — kept getting 429. So the wall is a refill window, not
    concurrency.

    `_retry` was shaped for the flaky-transport failures documented in
    `doc/CLAUDE.md`: three attempts, 2s then 8s, "same shape as
    correctChunkRetry in fix/main.go". That is ~10 seconds of patience against a
    window that needs up to 60, and `embed_many` raises when one text exhausts
    its retries — so a whole 600-chunk run dies on a condition that clears by
    waiting. A 429 says "later"; a read timeout says "again".
    """
    from docagent import vertex

    slept: list[float] = []
    monkeypatch.setattr(vertex.time, "sleep", slept.append)

    v = vertex.Vertex.__new__(vertex.Vertex)
    v._note_retry = lambda *a, **k: None
    v.ledger = Ledger()
    calls = {"n": 0}

    def always_429(model, verb, body):
        calls["n"] += 1
        raise vertex.VertexError(429, "quota exceeded")

    v._post = always_429
    with pytest.raises(vertex.VertexError):
        v._retry("embed", "m", "predict", {})

    assert calls["n"] > 3, "a quota 429 must outlast the 3 attempts a timeout gets"
    assert sum(slept) >= 60, f"only waited {sum(slept)}s for a per-minute window"
    assert max(slept) <= 60, "a single sleep longer than the window buys nothing"


def test_a_server_error_keeps_the_measured_three_attempts():
    """The 429 change must not loosen the path that was already measured."""
    from docagent import vertex

    v = vertex.Vertex.__new__(vertex.Vertex)
    v._note_retry = lambda *a, **k: None
    v.ledger = Ledger()
    calls = {"n": 0}

    def always_500(model, verb, body):
        calls["n"] += 1
        raise vertex.VertexError(503, "unavailable")

    v._post = always_500
    import unittest.mock

    with unittest.mock.patch.object(vertex.time, "sleep"):
        with pytest.raises(vertex.VertexError):
            v._retry("embed", "m", "predict", {})
    assert calls["n"] == vertex.MAX_ATTEMPTS


# --- an interrupted run must keep the embeddings it paid for -------------------


def test_an_embedding_is_cached_so_a_resumed_run_does_not_re_pay(tmp_path, monkeypatch):
    """The gap that made the quota wall unrecoverable.

    `doc/CLAUDE.md` states the resilience rule the correction pass follows: "the
    correction cache persists per batch, so an interrupted run keeps what it
    paid for". Embedding had no such thing. Measured 2026-08-28 on
    `01_RetoDeDios_INT-S.pdf`: a run reached **586 of 600** embeddings and then
    died on

        Quota exceeded for
        aiplatform.googleapis.com/online_prediction_requests_per_base_model
        with base model: gemini-embedding

    `embed_many` raises when one text exhausts its retries, so nothing was
    indexed and all 586 were thrown away. The next attempt re-pays them — and
    the scarce resource here is not the $0.018, it is the quota, whose unit
    `serviceusage` reports as `1/min/{project}/{base_model}`. Re-spending 586
    units of a per-minute budget to arrive at the same wall is how a run never
    converges.

    With the cache, a resumed run asks only for what it never got.
    """
    from docagent import vertex
    from docagent.ledger import Ledger

    monkeypatch.chdir(tmp_path)
    v = vertex.Vertex.__new__(vertex.Vertex)
    v.ledger = Ledger()
    calls = {"n": 0}

    def one_prediction(model, verb, body):
        calls["n"] += 1
        return {
            "predictions": [
                {
                    "embeddings": {
                        "values": [0.5] * vertex.EMBED_DIMS,
                        "statistics": {"token_count": 7, "truncated": False},
                    }
                }
            ]
        }

    v._post = one_prediction

    first = v.embed("un párrafo cualquiera")
    second = v.embed("un párrafo cualquiera")

    assert calls["n"] == 1, "the second call must come from the cache"
    assert second.values == first.values
    assert second.tokens == first.tokens

    # A different task type is a different vector and must not share an entry.
    v.embed("un párrafo cualquiera", vertex.TASK_QUERY)
    assert calls["n"] == 2
