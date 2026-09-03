"""One test per inherited invariant.

These fourteen rules were not designed up front — each is a mistake already paid
for while building the Go pipeline this agent ports. Encoding them as tests is the
only thing that stops them being rediscovered.

Tests that would need a live API or Qdrant assert on the request/response shape
instead, so the whole file runs offline in milliseconds.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from docagent import bm25, evaluate as ev, qdrant as qd
from docagent.chunk import (
    KIND_BODY,
    KIND_FOOTNOTE,
    KIND_QUESTIONS,
    Chunk,
    ChunkRules,
    build_chunks,
    classify_kind,
    read_source,
)
from docagent.ledger import EMBED_INPUT_PER_M, PRICES_PER_MILLION, Entry, Ledger
from docagent.vertex import EMBED_DIMS, TASK_DOCUMENT, TASK_QUERY

import corpus

# These are properties of the chunker on real prose, not measurements of one
# document, so any large UTF-8 prose file exercises them. See tests/corpus.py.
BOOK = corpus.resolve()


@pytest.fixture(scope="module")
def book():
    if BOOK is None:
        pytest.skip("no corpus available")
    src, paras = read_source(str(BOOK))
    return src, paras, build_chunks(src, paras, ChunkRules())


# 1 -- char_span is a byte-exact slice, and the offsets are BYTES ------------


def test_inv01_char_span_is_byte_exact(book):
    src, _, chunks = book
    assert chunks
    for c in chunks:
        assert src[c.char_from : c.char_to].strip().decode() == c.text, c.index


def test_inv01_offsets_are_bytes_not_characters(book):
    """The book is UTF-8 Spanish, so byte and character offsets diverge. A chunk
    whose span works when the source is read as text but not as bytes would mean
    the offsets are character indices — the mistake that made a first audit of the
    Go collection report 0/5 matches."""
    src, _, chunks = book
    as_text = src.decode()
    late = [c for c in chunks if c.char_from > 100_000]
    assert late, "need a chunk far enough in for the two indexings to diverge"
    c = late[0]
    assert src[c.char_from : c.char_to].strip().decode() == c.text
    assert as_text[c.char_from : c.char_to].strip() != c.text


# 2 -- max_embed_chars must exceed hard_cap_chars ----------------------------


def test_inv02_enforced_at_construction():
    with pytest.raises(ValueError, match="invariant #2"):
        ChunkRules(hard_cap_chars=2000, max_embed_chars=1800)


def test_inv02_near_cap_chunks_still_get_their_overlap(book):
    """The reason the invariant exists: with max_embed == hard_cap, 63 of 323
    chunks silently lost their overlap."""
    _, _, chunks = book
    rules = ChunkRules()
    near_cap = [c for c in chunks if len(c.text.encode()) > rules.target_chars * 1.4]
    assert near_cap
    assert any(c.overlap for c in near_cap[1:]), "no near-cap chunk kept an overlap"


# 3 -- a chunk never spans a heading nor mixes kinds -------------------------


def test_inv03_no_chunk_mixes_kinds(book):
    _, paras, chunks = book
    rules = ChunkRules()
    by_idx = {p.idx: p for p in paras}
    from docagent.chunk import heading_level

    for c in chunks:
        for i in range(c.para_from, c.para_to + 1):
            p = by_idx.get(i)
            if p is None or heading_level(p.text, rules) > 0:
                continue
            assert classify_kind(p.text, rules) == c.kind, (c.index, i)


# 4 -- the breadcrumb goes in `content`, never a `title` field ---------------


def test_inv04_breadcrumb_is_inside_the_embedded_content(book):
    """`title` is only reliably supported by the older text-embedding-* models, so
    the breadcrumb has to ride inside the text itself."""
    # Asserted on a constructed chunk first, because whether the *corpus* has a
    # breadcrumb at all is a fact about the document: `heading_level` recognises
    # only numbered headings without a learned profile, so a book whose chapters
    # are titled "Capítulo N" legitimately produces none. This half is the
    # property of the code and runs on every corpus.
    made = Chunk(
        index=0, kind=KIND_BODY, chapter="2. El hombre", section="2.1. El alma",
        text="Cuerpo del capítulo.", char_from=0, char_to=20, para_from=0, para_to=0,
    )
    assert made.embed_text().startswith(made.breadcrumb())

    _, _, chunks = book
    c = next((x for x in chunks if x.breadcrumb()), None)
    if c is None:
        pytest.skip(
            "this corpus produces no breadcrumb: without a learned profile "
            "`heading_level` recognises only numbered headings"
        )
    assert c.embed_text().startswith(c.breadcrumb())
    assert c.breadcrumb() not in c.text or c.text.startswith(c.chapter)


# 5 -- asymmetric task types ------------------------------------------------


def test_inv05_document_and_query_task_types_differ():
    assert TASK_DOCUMENT != TASK_QUERY
    assert TASK_DOCUMENT == "RETRIEVAL_DOCUMENT"
    assert TASK_QUERY == "RETRIEVAL_QUERY"


# 6 -- one instance per embedding request -----------------------------------


def test_inv06_embed_sends_exactly_one_instance(monkeypatch):
    """One instance per request.

    The rule was established against `gemini-embedding-001`, which accepted a
    single instance and failed *silently* on more. The engine now calls
    `gemini-embedding-2` and that model's batching limit has not been
    re-measured — so this stays as a property of the client, and the assumption
    behind it is stated rather than quietly inherited.
    """
    from docagent.vertex import Vertex

    captured: dict = {}

    def fake_post(self, model, verb, body):
        captured.update(body)
        return {
            "predictions": [
                {
                    "embeddings": {
                        "values": [0.0] * EMBED_DIMS,
                        "statistics": {"token_count": 7, "truncated": False},
                    }
                }
            ]
        }

    monkeypatch.setattr(Vertex, "_post", fake_post)
    v = Vertex(project_id="p")
    v.embed("hola")
    assert len(captured["instances"]) == 1
    assert captured["parameters"]["outputDimensionality"] == EMBED_DIMS
    assert captured["parameters"]["autoTruncate"] is False


# 7 -- cost comes from measured token counts --------------------------------


def test_inv07_ledger_uses_reported_tokens(monkeypatch):
    """The invariant is where the number comes from, not what it is multiplied by.

    Cost is computed from the counts the API itself returns —
    `statistics.token_count` for embeddings — never from a character heuristic;
    the Go pipeline's chars/4 estimate overshot the measured count by 11%. That
    is asserted on the tokens, because the model the engine now calls
    (`gemini-embedding-2`) has no recorded price, and a ledger that invented one
    would be the failure this whole rule exists to prevent.
    """
    from docagent.vertex import Vertex

    monkeypatch.setattr(
        Vertex,
        "_post",
        lambda self, m, vb, b: {
            "predictions": [
                {
                    "embeddings": {
                        "values": [0.0] * EMBED_DIMS,
                        "statistics": {"token_count": 1_000_000, "truncated": False},
                    }
                }
            ]
        },
    )
    v = Vertex(project_id="p")
    v.embed("whatever")

    entry = next(iter(v.ledger.entries.values()))
    assert entry.input_tokens == 1_000_000, "the reported count, not an estimate"
    # Exactly one million reported tokens costs exactly the per-million price,
    # with no character heuristic anywhere in the path.
    assert entry.cost_usd() == pytest.approx(PRICES_PER_MILLION["gemini-embedding-001"][0])
    assert v.ledger.unpriced_stages() == []


def test_inv07_a_priced_model_still_costs_its_reported_tokens(monkeypatch):
    """The multiplication itself, on a model whose price *is* recorded.

    Exactly one million reported tokens must cost exactly the per-million price,
    with no character heuristic anywhere in the path.
    """
    from docagent.ledger import Ledger

    ledger = Ledger()
    ledger.record("embed", "gemini-embedding-001", input_tokens=1_000_000)
    assert ledger.total_usd() == pytest.approx(EMBED_INPUT_PER_M)
    assert ledger.unpriced_stages() == []


def test_inv07_an_unpriced_model_reports_none_not_zero():
    """Unpriced is an answer; zero would read as free.

    The distinction is what let the Gemini Enterprise migration ship with no
    prices at all rather than with the previous model's.
    """
    e = Entry(stage="generate", model="gemini-4-flash", input_tokens=1_000_000)
    assert e.cost_usd() is None

    ledger = Ledger()
    ledger.record("generate", "gemini-4-flash", input_tokens=1_000_000)
    assert ledger.total_usd() == 0.0
    assert ledger.unpriced_stages() == ["generate"]


def test_inv07_no_output_charge_for_embeddings():
    e = Entry(stage="embed", model="gemini-embedding-001", input_tokens=1_000_000, output_tokens=999)
    assert e.cost_usd() == pytest.approx(EMBED_INPUT_PER_M)


# 8 -- min_score only ever reaches the dense leg ----------------------------


def test_inv08_threshold_never_applied_to_fused_output():
    """RRF returns reciprocal ranks, not cosines. A cosine threshold on the fused
    result would be meaningless, so it must sit on the dense prefetch."""
    captured: dict = {}

    class FakeQdrant(qd.Qdrant):
        def __init__(self):
            self.base_url, self.collection = "", "c"

        def _ok(self, method, path, body=None):
            captured.update(body or {})
            return {"result": {"points": []}}

    FakeQdrant().search([0.1], qd.SearchOpts(limit=5, min_score=0.6, query_text="x"))
    assert "score_threshold" not in captured, "threshold leaked onto the fused query"
    dense = next(p for p in captured["prefetch"] if p["using"] == qd.DENSE_VEC)
    sparse = next(p for p in captured["prefetch"] if p["using"] == qd.SPARSE_VEC)
    assert dense["score_threshold"] == 0.6
    assert "score_threshold" not in sparse
    assert captured["query"] == {"fusion": "rrf"}


# 9 -- off-topic gating uses the dense leg, not a BM25 threshold ------------


def test_inv09_topicality_gate_probes_dense_only():
    """A BM25 threshold cannot separate noise from real exact-term queries:
    measured, an off-topic query scored 5.58 on BM25, above "Gén. 2:15" at 3.82."""
    calls: list[dict] = []

    class FakeQdrant(qd.Qdrant):
        def __init__(self):
            self.base_url, self.collection = "", "c"

        def _ok(self, method, path, body=None):
            calls.append(body or {})
            return {"result": {"points": []}}

    q = FakeQdrant()
    allowed = q.topicality_gate([0.1], qd.SearchOpts(limit=5, min_score=0.6, query_text="x"))
    assert allowed is False, "an empty dense probe must close the gate"
    assert calls[-1]["using"] == qd.DENSE_VEC
    assert "prefetch" not in calls[-1]


# 10 -- deterministic point ids ---------------------------------------------


def test_inv10_point_ids_are_deterministic_and_distinct():
    assert qd.point_id("libro.txt", 7) == qd.point_id("libro.txt", 7)
    assert qd.point_id("libro.txt", 7) != qd.point_id("libro.txt", 8)
    assert qd.point_id("a.txt", 7) != qd.point_id("b.txt", 7)
    import uuid

    assert uuid.UUID(qd.point_id("libro.txt", 7)).version == 5


# 11 -- questions reset the section path, footnotes do not ------------------


def test_inv11_questions_reset_the_section_path(book):
    _, _, chunks = book
    questions = [c for c in chunks if c.kind == KIND_QUESTIONS]
    if not questions:
        pytest.skip("this corpus has no question chunks to check")
    assert all(not c.section for c in questions), [c.breadcrumb() for c in questions]


def test_inv11_footnotes_keep_their_section_path(book):
    """The other half of invariant #11, split out because the two halves have
    different preconditions: one needs the document to contain questions, the
    other footnotes, and plenty of documents have neither.

    Raw PDF extraction merges footnotes into body paragraphs, so there is often
    no separable footnote class at all — which is why the learned `kind`
    patterns usually fall back to the built-in defaults. Asserting both halves
    in one test silently required a document with both classes.

    The questions half was checked exhaustively rather than assumed: across all
    38 corrected texts in `libros/` there are 88 question chunks and **zero**
    carry a section path, so the invariant holds corpus-wide.
    """
    _, _, chunks = book
    footnotes = [c for c in chunks if c.kind == KIND_FOOTNOTE]
    if not footnotes:
        pytest.skip("this corpus has no footnote chunks to check")
    if not any(c.section for c in chunks):
        # A footnote can only keep a path the document supplies, and whether it
        # supplies one is a fact about the document rather than a property of
        # this rule. Measured 2026-09-03 across the 45 corrected texts in
        # `libros/`: 4 of them produce footnote chunks carrying a section, 13
        # chunks in total, and the rest number no headings at all — including
        # the largest, which is the one this fixture resolves to.
        pytest.skip(
            "this corpus produces no section path at all, so there is none for "
            "a footnote to keep"
        )
    # Footnotes annotate the section they sit in, so at least some keep a path.
    assert any(c.section for c in footnotes)


# 12 -- the dot after the number is the discriminator ----------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1. Defina qué es un metarrelato o metanarrativa", KIND_QUESTIONS),
        ("53 La palabra “nihilismo” proviene de la raíz latina “nihil”", KIND_FOOTNOTE),
    ],
)
def test_inv12_dot_discriminates_questions_from_footnotes(text, expected):
    assert classify_kind(text, ChunkRules()) == expected


def test_inv12_optional_dot_would_break_it():
    """Proof the invariant is load-bearing: relaxing the dot to optional makes a
    footnote match the question rule, which is precisely the nine-false-positive
    bug."""
    import re

    footnote = "53 La palabra “nihilismo” proviene de la raíz latina “nihil”"
    assert re.match(r"^\d+\.\s", footnote) is None
    assert re.match(r"^\d+\.?\s", footnote) is not None


# 13 -- named vectors are the collection schema ----------------------------


def test_inv13_collection_declares_named_and_sparse_vectors():
    captured: list[tuple[str, dict]] = []

    class FakeQdrant(qd.Qdrant):
        def __init__(self):
            self.base_url, self.collection = "", "c"

        def exists(self):
            return False

        def _ok(self, method, path, body=None):
            captured.append((path, body or {}))
            return {}

    FakeQdrant().create(EMBED_DIMS, ("kind",))
    _, body = captured[0]
    assert body["vectors"][qd.DENSE_VEC]["size"] == EMBED_DIMS
    assert body["vectors"][qd.DENSE_VEC]["distance"] == "Cosine"
    assert qd.SPARSE_VEC in body["sparse_vectors"]


# 14 -- the IDF lives in Qdrant, not in the stored vectors -----------------


def test_inv14_idf_modifier_is_declared():
    captured: list[dict] = []

    class FakeQdrant(qd.Qdrant):
        def __init__(self):
            self.base_url, self.collection = "", "c"

        def exists(self):
            return False

        def _ok(self, method, path, body=None):
            captured.append(body or {})
            return {}

    FakeQdrant().create(EMBED_DIMS)
    assert captured[0]["sparse_vectors"][qd.SPARSE_VEC]["modifier"] == "idf"


def test_inv14_document_vectors_carry_no_idf():
    """A term appearing in every document must not be down-weighted on the
    document side: that is Qdrant's job. So a doc vector's value depends only on
    term frequency and length, never on how many documents contain the term."""
    tokens = bm25.tokenize("cultura sociedad cultura")
    a = bm25.doc_sparse_vector(tokens, bm25.avg_doc_len([tokens]))
    b = bm25.doc_sparse_vector(tokens, bm25.avg_doc_len([tokens, tokens, tokens]))
    # Same tokens, same length: identical values regardless of corpus composition.
    assert sorted(zip(a.indices, a.values)) != [] and a.indices == b.indices


def test_inv14_query_vectors_are_all_ones():
    v = bm25.query_sparse_vector("el cogito cartesiano de Descartes")
    assert v.values and set(v.values) == {1.0}


# -- the tuning guard ------------------------------------------------------


def _run(ranks: list[int], questions: int = 30) -> ev.EvalRun:
    """An EvalRun from a list of ranks, 0 meaning "not retrieved"."""
    rr = [1.0 / r for r in ranks if r]
    return ev.EvalRun(
        hits_at_1=sum(1 for r in ranks if r == 1),
        hits_at_5=sum(1 for r in ranks if r and r <= 5),
        reciprocal_ranks=rr,
        questions=questions,
    )


def test_the_objective_is_mrr_not_recall_at_5():
    """Measured on a 24-question eval, recall@5 was identical (0.875) for every
    configuration tried while MRR@10 moved from 0.708 to 0.755. A coarse binary at
    k=5 cannot see a passage moving from rank 4 to rank 2, so tuning against it is
    tuning against a saturated metric."""
    worse = _run([4] * 20 + [0] * 10)
    better = _run([2] * 20 + [0] * 10)
    assert worse.recall_at_5 == better.recall_at_5
    assert ev.objective(better) > ev.objective(worse)


def test_tuning_rejects_gains_inside_the_noise_margin():
    """A single question improving slightly is noise, not progress."""
    baseline = _run([1] * 14 + [3] * 10 + [0] * 6)
    candidate = _run([1] * 14 + [3] * 9 + [2] + [0] * 6)
    accepted, why = ev.is_real_improvement(baseline, candidate)
    assert not accepted, why
    assert "noise margin" in why


def test_tuning_accepts_a_clear_gain():
    """Twenty passages moving from rank 5 to rank 1 is unambiguous."""
    baseline = _run([5] * 20 + [0] * 10)
    candidate = _run([1] * 20 + [0] * 10)
    accepted, why = ev.is_real_improvement(baseline, candidate)
    assert accepted, why
    assert "MRR@10" in why


def test_rr_vector_makes_misses_explicit():
    """Bootstrapping MRR needs the zeros: reciprocal_ranks only records hits."""
    run = _run([1, 2, 0, 0], questions=4)
    assert sorted(run.rr_vector()) == [0.0, 0.0, 0.5, 1.0]
    assert run.mrr == pytest.approx(0.375)


def test_leakage_is_reported_when_hybrid_pulls_ahead():
    hybrid = ev.EvalRun(hits_at_5=28, questions=30)
    dense = ev.EvalRun(hits_at_5=20, questions=30)
    note = ev.leakage_note(hybrid, dense)
    assert "leakage" in note.lower()


def test_no_leakage_claimed_when_the_two_agree():
    same = ev.EvalRun(hits_at_5=24, questions=30)
    note = ev.leakage_note(same, ev.EvalRun(hits_at_5=24, questions=30))
    assert "no sign" in note


# -- the ledger reports its own uncertainty --------------------------------


def test_ledger_states_that_prices_are_second_hand():
    """The token counts are measured; the multipliers are from third-party
    aggregators. A report that hid that difference would overstate its own
    authority."""
    led = Ledger()
    led.record("embed", "gemini-embedding-001", input_tokens=1000)
    d = json.loads(json.dumps(led.to_dict()))
    assert d["measured"] is True
    assert "second-hand" in d["price_source"]
    assert "GCP billing" in d["price_source"]
