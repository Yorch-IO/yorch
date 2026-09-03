"""Four defects in the chunker found by a wide audit on 2026-09-03.

Every one of them meets the same four conditions: it raises nothing, the suite
passed with it present, its only symptom is worse retrieval, and there is a line
of a real book that triggers it. Each test names that line.

The measurements quoted here were taken over the 45 files matching
`libros/**/*.corrected.txt`, with each book's own learned profile applied where
one exists, by running the pre-fix and post-fix chunker side by side.
"""

from __future__ import annotations

import pathlib
import tempfile

from docagent.chunk import (
    KIND_BODY,
    KIND_FOOTNOTE,
    KIND_QUESTIONS,
    ChunkRules,
    build_chunks,
    classify_kind,
    heading_level,
    is_endnote,
    split_paragraphs,
)


def _chunk(paragraphs: list[str], rules: ChunkRules | None = None):
    data = "\n\n".join(paragraphs).encode("utf-8")
    return data, build_chunks(data, split_paragraphs(data), rules or ChunkRules())


# --- 1: a citation is not a chapter -----------------------------------------
#
# `heading_level` had four guards, each added after a measured failure, and none
# of them for an endnote list. Measured with the real profiles applied: 34 lines
# across 5 books read as level-1 chapters, and **428 of 4,239 chunks (10.1%)
# carried one as their breadcrumb** — 233 of 359 in 06-SexoEnLaBiblia_INT-S,
# 53 of 76 in CLASE 2, 126 of 600 in 01_RetoDeDios_INT-S. The breadcrumb is
# prepended to what gets embedded (`Chunk.embed_text`), so every one of those
# chunks was embedded under a bibliography line.


def test_an_endnote_numbered_like_a_heading_is_not_a_chapter():
    """Every one of these is a real line from a real book in `libros/`."""
    rules = ChunkRules(heading_l1_max=60)
    for line in (
        "2. Ibídem.",                                    # 01_RetoDeDios, 05-CodigoJesus
        "6. Ibídem, p. 120.",                            # 01_RetoDeDios
        "3. Ibídem. p. 54.",                             # 01_RetoDeDios
        "2.\t Ibidem.",                                  # 06-SexoEnLaBiblia
        "1. Tácito, Anales, 15, 44.",                    # 01_RetoDeDios
        "9. Notimex, México, Dic. 30, 2002.",            # 06-SexoEnLaBiblia
        "2. Flavio Josefo, Antigüedades Judaicas, 18:63, 64.",   # 05-CodigoJesus
        "16 Lacueva, F. (2001). Diccionario Teológico Ilustrado. (p. 274). Editorial Clie.",  # CLASE 2
    ):
        assert heading_level(line, rules) == 0, line


def test_a_learned_profile_does_not_repair_it():
    """The guard has to run before the learned patterns, not after.

    `heading_level` consults the profile's pattern and, when it does not match,
    *falls through* to the built-in numbered detector — so `2. Ibídem.` was a
    level-1 chapter both with 05-CodigoJesus's own profile and without it.
    """
    rules = ChunkRules(heading_l1_pattern=r"^Clave\s+\d+$", heading_l1_max=60)
    assert heading_level("2. Ibídem.", rules) == 0
    assert heading_level("Clave 7", rules) == 1


def test_a_numbered_list_item_in_lowercase_is_not_a_chapter():
    """05-CodigoJesus sets a four-item list inside its prose: "1.\\t la María
    humana,". A heading's title is capitalised; these four read as chapters."""
    rules = ChunkRules(heading_l1_max=60)
    for line in ("1.\t la María humana,", "4.\t la María inauténtica."):
        assert heading_level(line, rules) == 0, line


def test_a_real_numbered_chapter_still_reads_as_one():
    """The other half, and the one that matters: across all 45 corrected texts
    the 34 lines whose level changed were citations or lowercase list items, and
    **no real heading moved.** These are real chapter lines from that corpus."""
    rules = ChunkRules(heading_l1_max=60)
    for line, want in (
        ("1. Doctrina del Hombre", 1),
        ("3. Doctrina de la redención", 1),
        ("1. Palabra hablada en la creación.", 1),          # 03-ElFrutoEterno
        ("14. EL ESCOLASTICISMO.", 1),                      # 4. ESCOLÁSTICA…
        ("16. LA PRERREFORMA (1.366-1.517)", 1),            # 4. ESCOLÁSTICA…
        ("2.1.1 El motivo", 3),
    ):
        assert heading_level(line, rules) == want, line


def test_the_citation_tail_rule_needs_the_closing_period():
    """", 44" alone is not enough, or "16. LA PRERREFORMA (1.366-1.517)" would
    lose its level. The vocabulary of a citation is decisive on its own."""
    assert is_endnote("1. Tácito, Anales, 15, 44.")
    assert not is_endnote("16. LA PRERREFORMA (1.366-1.517)")
    assert is_endnote("2. Ibídem")          # no period, but the word settles it


# --- 2: a citation is not a review question ---------------------------------
#
# The sibling defect, recorded in `classify_kind`'s own docstring as reported
# and not fixed. `preguntas` **resets the section path** (invariant #11) and
# `nota` does not, so a bibliography tagged as review questions wipes the
# breadcrumb of everything that follows it. Measured over the corpus: 107 chunks
# moved out of `preguntas` (334 -> 227) and 115 into `nota` (30 -> 145).
#
# The fix recorded as tried-and-reverted required a ¿/?/imperative to
# corroborate the dot, which left every footnote chunk with no section. This one
# keys on the vocabulary of a citation instead, so a footnote is still a
# footnote and still keeps its path.


def test_a_numbered_citation_is_a_footnote_not_a_review_question():
    """The reach of the rule, stated by measurement rather than by claim: after
    this fix **90 numbered paragraphs across the corpus still read as
    `preguntas` while asking nothing**, in two shapes it deliberately does not
    touch — a citation with no comma-then-digit tail ("2. Juan Crisóstomo, La
    incomprensible naturaleza de Dios, Homilía V.") and a numbered enumeration
    the author set in prose ("1.\t Toda persona es autónoma en materia
    religiosa."). The second is not a footnote either, so a rule that caught it
    would have to be a learned `question_pattern`, not this one."""
    rules = ChunkRules()
    for line in (
        "2. Ibídem.",
        "1. Paul Johnson, Historia del cristianismo, Vergara Editor, S.A., Buenos Aires, 1989.",
        "5. Luis María Mora, Los maestros de principios de siglo, Editorial ABC, Bogotá, 1938, p. 121.",
    ):
        assert classify_kind(line, rules) == KIND_FOOTNOTE, line


def test_a_real_review_question_is_still_a_review_question():
    rules = ChunkRules()
    for line in (
        "11. Defina Arrianismo",
        "26. Señale algunos de los peligros",
        "1. Defina qué es un metarrelato o metanarrativa",
    ):
        assert classify_kind(line, rules) == KIND_QUESTIONS, line


def test_a_cited_title_that_asks_something_is_left_alone():
    """The conservative half. "5. Luisa Jeter de Walker, ¿Cuál camino?,
    Editorial Vida, Florida, 1968." is a citation whose *title* is a question,
    and there are 9 of them in the corpus. Reclassifying on the citation tail
    alone would take a numbered question with it, so a line that asks something
    keeps the classification it had."""
    rules = ChunkRules()
    line = "5. Luisa Jeter de Walker, ¿Cuál camino?, Editorial Vida, Florida, 1968."
    assert classify_kind(line, rules) == KIND_QUESTIONS


def test_a_footnote_keeps_its_section_and_a_question_resets_it():
    """Invariant #11, asserted on a document rather than on the corpus.

    This is the property the reverted attempt broke, and it is what makes the
    reclassification safe: the citation becomes a `nota`, and a `nota` keeps the
    path.
    """
    body = "Prosa corriente del capítulo, que no es un encabezado. " * 3
    data, chunks = _chunk([
        "1. Del conocimiento de Dios",
        "1.1 La revelación general",
        body,
        "2. Ibídem, p. 120. Véase también lo dicho más arriba sobre el asunto.",
        "1.2 La revelación especial",
        body,
        "3. Defina la revelación general y explique su relación con la especial.",
    ])
    notes = [c for c in chunks if c.kind == KIND_FOOTNOTE]
    questions = [c for c in chunks if c.kind == KIND_QUESTIONS]
    assert notes and questions
    assert all(c.section for c in notes), [c.breadcrumb() for c in notes]
    assert all(not c.section for c in questions)


# --- 3: a table of contents with spaced dot leaders -------------------------
#
# `TOC_LINE_RE` was `\.{5,}`, and a typeset leader comes out of PyMuPDF as
# ". . . ." — five dots with spaces between them, which that pattern never
# matched. Measured: 24 index and prologue lines across 4 books, 17 of them
# numbered and therefore tagged `preguntas`, each one resetting the section path.


def test_a_contents_line_with_spaced_dot_leaders_is_not_a_review_question():
    rules = ChunkRules()
    for line in (
        "1. Lazos familiares.. . . . . . . . . . . . . . . . . . . . . . . 11",
        "6. La mafia de la mendicidad.. . . . . . . . . . . . . . . . . . . 99",
        "8. La soledad compartida . . . . . . . . . . . . . . . . . . . . . 165",
    ):
        assert classify_kind(line, rules) == KIND_BODY, line
        assert heading_level(line, rules) == 0, line


def test_the_unspaced_leader_this_replaces_still_matches():
    """The shape the rule was written for, from 01_RetoDeDios_INT-S: 99 index
    lines tagged `preguntas`."""
    rules = ChunkRules()
    line = "10. El «concordato evangélico»......105"
    assert classify_kind(line, rules) == KIND_BODY


# --- 4: a paragraph too short to stand alone was dropped --------------------
#
# `_windowize` cut the pending run the moment the next unit would overshoot
# `target_chars`, and `emit` then discarded a run under `min_chunk_chars`
# outright. A title is short and the paragraph after it is long, so the title
# was exactly what got cut off and thrown away.
#
# Measured over the 45 corrected texts with each book's own profile applied:
# **158 paragraphs never reached any chunk.** 44 of them appear exactly once in
# their document and are content — "Sobre el autor", "El verbo divino",
# "Hispanización", and the wrapped tails of sentences ("naturales de la
# tolerancia."). The other 114 are running headers, and **dropping them was
# never a header filter: 49 of the 55 distinct lines involved already appear
# inside a chunk elsewhere in the same book** ("La nueva glosolalia" 6 times in
# chunks and dropped once, "El misterio del amor" 11 and 3). So the rule removed
# real content and left the noise it looked like it was removing.


def test_a_short_paragraph_travels_with_the_text_that_follows_it():
    """Forward, not backward: a title belongs to the section it opens.

    Two things had to change for this, and they are not redundant. The run is
    no longer cut off when it is still under `min_chunk_chars`, which is what
    carries the title into the *next* chunk; and `emit` merges a short run
    backwards when nothing follows it, which is the case the test below covers.
    Without the first, the title lands at the end of the preceding chunk
    instead — in the index, but under the previous section's text.
    """
    title = "La corona no hace al rey"          # 24 bytes, under min_chunk_chars
    before = "Ppp. " * 240                       # 1200 bytes: forces the cut
    after = "Sss. " * 300                        # 1500 bytes: overshoots target
    data, chunks = _chunk([before.strip(), title, after.strip()])
    holding = [c for c in chunks if title in c.text]
    assert holding, [c.text[:40] for c in chunks]
    assert "Sss." in holding[0].text, "the title must travel with the text it opens"
    assert "Ppp." not in holding[0].text


def test_a_short_paragraph_at_the_end_of_a_run_reaches_the_index():
    """The tail case, where there is no following paragraph to travel with: it
    joins the chunk before it instead."""
    title = "Sobre el autor"
    before = "Ppp. " * 240
    data, chunks = _chunk([before.strip(), title])
    assert any(title in c.text for c in chunks), [c.text[:40] for c in chunks]


def test_merging_a_short_run_never_breaches_the_hard_cap():
    rules = ChunkRules()
    title = "Prólogo"
    data, chunks = _chunk(
        ["Ppp. " * 240, title, "Sss. " * 390], rules     # 1950-byte tail
    )
    for c in chunks:
        assert len(c.text.encode("utf-8")) <= rules.hard_cap_chars, c.index


def test_every_chunk_is_still_a_byte_exact_slice_after_a_merge():
    data, chunks = _chunk(["Ppp. " * 240, "La corona no hace al rey", "Sss. " * 300])
    for c in chunks:
        assert data[c.char_from : c.char_to].strip().decode("utf-8") == c.text, c.index


# --- 5: a runaway sentence lost a character at every cut --------------------
#
# `_word_spans` cuts at a space and returned the *next* span starting on that
# space. `_split_oversized` strips the unit's text but not its offset, so
# `_windowize` measured the chunk's end one byte early and the last character of
# every such chunk was dropped. **No measurement on the real corpus: 0 of its
# 4,209 chunks reach this path**, which needs a single sentence over
# `hard_cap_chars` with no accepted boundary in it. Kept because the shape is
# reachable and the cost is a truncated word, or a half-decoded character.


def test_a_runaway_sentence_keeps_every_character():
    body = ("palabra " * 500).strip()      # ~4 KB, one sentence, no terminator
    data, chunks = _chunk([body])
    assert len(chunks) > 1, "the paragraph must be split for this to mean anything"
    joined = "".join(c.text for c in chunks).replace(" ", "")
    assert joined == body.replace(" ", "")


def test_no_chunk_span_begins_on_whitespace():
    body = ("palabra " * 500).strip()
    data, chunks = _chunk([body])
    for c in chunks:
        assert not data[c.char_from : c.char_from + 1].isspace(), c.index
        assert data[c.char_from : c.char_to].decode("utf-8") == c.text, c.index


# --- 6: rows sharing a baseline were ordered alphabetically -----------------


def test_two_rows_on_one_baseline_come_out_left_to_right():
    """`_page_rows` broke a baseline tie with the row's own *text*, so a table's
    cells were assembled in dictionary order rather than in reading order.

    Measured over the 84 PDFs in `libros/`: 9,302 of 109,444 lines share a
    baseline with another, 418 pages reorder, **679 paragraphs across 41
    documents come out different**, and the level-1 heading count over the whole
    set moves only 148 -> 147. Shipped instance, in
    `libros/done/Hermeneutica Capitulo 4.pdf.corrected.txt`: "cada siete años se
    15:2 perdona toda clase de deudas".
    """
    import fitz

    from docagent.chunk import DocRules, split_paragraphs
    from docagent.extract import pdf_text

    doc = fitz.open()
    page = doc.new_page(width=700, height=300)
    # Same baseline. The right-hand cell sorts first alphabetically, so the old
    # tiebreak put it first and the sentence came out backwards. Long enough
    # together to clear `MIN_TEXT_PER_PAGE`, or the page is discarded as having
    # no text layer before any of this is reached.
    page.insert_text((40, 100), "Zaragoza y la ribera del Ebro")
    page.insert_text((360, 100), "Alba de Tormes y su castillo")
    path = pathlib.Path(tempfile.mkdtemp()) / "baseline.pdf"
    doc.save(path)
    doc.close()

    extracted = pdf_text.extract(str(path), DocRules())
    text = " ".join(p.text for p in split_paragraphs(extracted.text or b""))
    assert "Zaragoza y la ribera del Ebro Alba de Tormes" in text, text


def test_a_merged_chunk_still_fits_what_goes_to_the_api():
    """`max_embed_chars` caps breadcrumb + overlap + text — what the embeddings
    API is actually sent (invariant #2).

    Asserted here because the test that covers it over a whole book,
    `test_port_fidelity.py`, **skips in any checkout without the Go reference
    corpus**, which is this one — all ten of its tests do. Measured over the 45
    corrected texts with each book's profile applied: 0 chunks exceed the cap
    before or after the merge, and the largest `embed_text` is 2,253 bytes
    against a 2,600 cap. The re-check inside the merge is therefore defensive:
    with the default rules the gap invariant #2 guarantees (600 bytes) is far
    wider than the most a merge can add (`min_chunk_chars`, 40), so it can only
    fire under a profile that sets `max_embed_chars` just above the hard cap.
    """
    rules = ChunkRules(target_chars=1200, hard_cap_chars=2000, max_embed_chars=2100)
    data, chunks = _chunk(
        ["1. Del conocimiento de Dios", "Aaa. " * 236, "Bbb. " * 379, "Sobre el autor"],
        rules,
    )
    assert any("Sobre el autor" in c.text for c in chunks), "the tail must have merged"
    for c in chunks:
        assert len(c.embed_text().encode("utf-8")) <= rules.max_embed_chars, c.index
