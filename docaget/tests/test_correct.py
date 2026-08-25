"""The correction pass must be unable to damage content.

Correction is the only step that *rewrites* the document, so it is the only step
that can silently corrupt it. The verifier is therefore deterministic and its job
is to reject, not to approve: a paragraph whose proper nouns, scripture references
or figures did not survive keeps its original text.

These tests are the specification of what "did not survive" means.
"""

from __future__ import annotations

import pytest

from docagent import correct as correct_mod
from docagent.correct import (
    MAX_LENGTH_DELTA,
    CorrectionReport,
    _batches,
    correct_paragraphs,
    corrected_path,
    verify,
)

@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the correction cache at a temp directory.

    Without this the tests share the real on-disk cache and contaminate each
    other — one test's fake "correction" becomes the next test's cache hit. It
    also keeps the suite from leaving junk in cache/.
    """
    monkeypatch.setattr(correct_mod, "CACHE_DIR", tmp_path / "correct")


ORIGINAL = (
    "Al ocuparse del aporte griego a la cultura occidental, Dooyeweerd identifica "
    "como su principal motivo la relación dialéctica que el pensamiento griego "
    "estableció entre los principios de materia y forma, según Gén. 2:15 y lo "
    "expuesto en 1991 por Antonio Cruz."
)


# --- what must be accepted ---------------------------------------------------


def test_accepts_an_accent_only_correction():
    """The overwhelmingly common case: a missing accent restored, nothing else."""
    bad = ORIGINAL.replace("relación", "relacion").replace("estableció", "establecio")
    ok, reason, detail = verify(bad, ORIGINAL)
    assert ok, f"{reason}: {detail}"


def test_accepts_an_identical_paragraph():
    ok, _, _ = verify(ORIGINAL, ORIGINAL)
    assert ok


def test_accepts_punctuation_fixes():
    bad = ORIGINAL.replace(", Dooyeweerd", " Dooyeweerd").replace("forma,", "forma")
    ok, reason, detail = verify(bad, ORIGINAL)
    assert ok, f"{reason}: {detail}"


# --- what must be rejected ---------------------------------------------------


def test_rejects_a_lost_proper_noun():
    """The guard that matters most for this corpus. "Dooyeweerd" is not a word the
    model can improve on."""
    damaged = ORIGINAL.replace("Dooyeweerd", "el autor")
    ok, reason, detail = verify(ORIGINAL, damaged)
    assert not ok
    assert reason == "proper_noun"
    assert "Dooyeweerd" in detail


def test_rejects_an_altered_scripture_reference():
    damaged = ORIGINAL.replace("Gén. 2:15", "Génesis 2:16")
    ok, reason, _ = verify(ORIGINAL, damaged)
    assert not ok
    assert reason in {"scripture", "numbers"}


def test_rejects_a_changed_figure():
    damaged = ORIGINAL.replace("1991", "1891")
    ok, reason, detail = verify(ORIGINAL, damaged)
    assert not ok
    assert reason == "numbers"
    assert "1991" in detail


def test_rejects_a_summary():
    """A summary is the failure mode of asking a model to "improve" prose. It loses
    length and names at once."""
    damaged = "Dooyeweerd habla del aporte griego."
    ok, reason, _ = verify(ORIGINAL, damaged)
    assert not ok
    assert reason in {"length", "numbers", "scripture"}


def test_rejects_added_content():
    damaged = ORIGINAL + " " + ("Esto es una explicación añadida por el modelo. " * 6)
    ok, reason, _ = verify(ORIGINAL, damaged)
    assert not ok
    assert reason == "length"


def test_rejects_an_empty_result():
    ok, reason, _ = verify(ORIGINAL, "   ")
    assert not ok
    assert reason == "empty"


# --- the guard must not fire on legitimate accent fixes ----------------------


def test_a_corrected_accent_on_a_name_is_not_a_lost_name():
    """Names are compared accent-folded, so restoring an accent on a proper noun
    must not read as having lost it — otherwise the guard would block exactly the
    corrections it is meant to allow."""
    bad = "Segun Nicolas Maquiavelo, las razones de Estado justifican mucho en 1532."
    good = "Según Nicolás Maquiavelo, las razones de Estado justifican mucho en 1532."
    ok, reason, detail = verify(bad, good)
    assert ok, f"{reason}: {detail}"


def test_sentence_initial_words_are_not_treated_as_names():
    """"Cuando" opening a sentence is capitalised grammatically, so the model may
    legitimately recase it when merging punctuation."""
    bad = "Cuando la razon reclama para si el arbitraje final. Pero el sentimiento queda."
    good = "Cuando la razón reclama para sí el arbitraje final, pero el sentimiento queda."
    ok, reason, detail = verify(bad, good)
    assert ok, f"{reason}: {detail}"


# --- structure cannot be lost ------------------------------------------------


class FakeVertex:
    """Stands in for the API. ``behaviour`` decides what the model "returns"."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    def generate(self, prompt, **kw):
        import json

        self.calls += 1
        items = json.loads(prompt)["parrafos"]
        return json.dumps({"parrafos": self.behaviour(items)})


def _accent_fixer(items):
    return [
        {"i": it["i"], "texto": it["texto"].replace("relacion", "relación")}
        for it in items
    ]


def test_output_length_and_order_always_match_the_input():
    """The Go version compared paragraph counts after the fact and, when they
    disagreed, used the output anyway. Here a mismatch is structurally impossible."""
    paras = [f"parrafo {i} con una relacion evidente." for i in range(7)]
    out, report = correct_paragraphs(FakeVertex(_accent_fixer), paras)
    assert len(out) == len(paras)
    assert all(f"parrafo {i}" in out[i] for i in range(len(paras)))
    assert report.changed == 7


def test_a_paragraph_the_model_omits_comes_back_unchanged():
    def drops_the_third(items):
        return [
            {"i": it["i"], "texto": it["texto"].upper()}
            for it in items
            if it["i"] != 2
        ]

    paras = [f"parrafo {i}" for i in range(5)]
    out, report = correct_paragraphs(FakeVertex(drops_the_third), paras)
    assert out[2] == "parrafo 2", "an omitted paragraph must not vanish"
    assert report.missing == 1


def test_a_hallucinated_index_cannot_contaminate_another_paragraph():
    def shifts_indices(items):
        return [{"i": it["i"] + 100, "texto": "CONTAMINADO"} for it in items]

    paras = [f"parrafo {i}" for i in range(4)]
    out, report = correct_paragraphs(FakeVertex(shifts_indices), paras)
    assert out == paras
    assert report.missing == 4
    assert "CONTAMINADO" not in "".join(out)


def test_an_api_failure_leaves_the_text_uncorrected_rather_than_aborting():
    class Exploding:
        def generate(self, *a, **k):
            raise RuntimeError("HTTP 503")

    paras = ["uno con relacion", "dos con relacion"]
    out, report = correct_paragraphs(Exploding(), paras)
    assert out == paras
    assert report.missing == 2
    assert report.changed == 0


def test_rejections_are_recorded_and_the_original_survives():
    def loses_the_name(items):
        return [
            {"i": it["i"], "texto": it["texto"].replace("Dooyeweerd", "el autor")}
            for it in items
        ]

    paras = [ORIGINAL]
    out, report = correct_paragraphs(FakeVertex(loses_the_name), paras)
    assert out[0] == ORIGINAL
    assert len(report.rejected) == 1
    assert report.rejected[0].reason == "proper_noun"


# --- batching and paths ------------------------------------------------------


def test_batches_cover_every_paragraph_exactly_once():
    paras = ["x" * 5000 for _ in range(20)]
    batches = _batches(paras)
    flat = [i for b in batches for i in b]
    assert sorted(flat) == list(range(20))
    assert len(batches) > 1, "20 x 5000 chars must not fit in one batch"


def test_corrected_path_keeps_the_original_extension_visible():
    """The name has to say what it came from, because this file — not the PDF — is
    what char_span refers to."""
    assert corrected_path("/tmp/libro.pdf").name == "libro.pdf.corrected.txt"
    assert corrected_path("libro.txt").name == "libro.txt.corrected.txt"


def test_report_summary_is_readable():
    r = CorrectionReport(paragraphs=10, changed=7, unchanged=3, calls=1)
    assert "7 corregidos" in r.summary()
    assert MAX_LENGTH_DELTA == 0.25


# --- the correction must not break invariant #1 ------------------------------


def test_corrected_text_is_still_sliceable_by_char_span(tmp_path):
    """Correction changes byte offsets, which is why it runs before chunking and
    its output is persisted. This proves the contract end to end: chunk the
    *corrected* bytes and every span must slice back out of the file that was
    written.
    """
    from docagent.chunk import ChunkRules, build_chunks, split_paragraphs
    from docagent.correct import write_corrected

    original = [
        "1. Cultura y sociedad",
        "Los temas relacionados en el titulo de este capitulo, es decir cultura y "
        "sociedad, guardan una evidente relacion con otras materias ya vistas.",
        "Dooyeweerd identifica el motivo griego de materia y forma, segun Gén. 2:15.",
    ]
    # What the model would return: accents restored, nothing else.
    corrected_paras = [
        "1. Cultura y sociedad",
        "Los temas relacionados en el título de este capítulo, es decir cultura y "
        "sociedad, guardan una evidente relación con otras materias ya vistas.",
        "Dooyeweerd identifica el motivo griego de materia y forma, según Gén. 2:15.",
    ]
    assert all(verify(a, b)[0] for a, b in zip(original, corrected_paras))

    corrected = "\n\n".join(corrected_paras).encode("utf-8")
    src = tmp_path / "libro.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    path = write_corrected(str(src), corrected)

    on_disk = path.read_bytes()
    assert on_disk == corrected

    chunks = build_chunks(on_disk, split_paragraphs(on_disk), ChunkRules())
    assert chunks
    for c in chunks:
        # Read as bytes: char_span holds byte offsets, and slicing a decoded str
        # would give character indices.
        assert on_disk[c.char_from : c.char_to].strip().decode() == c.text


def test_offsets_shift_when_accents_are_added():
    """Why the corrected text has to be persisted rather than kept in memory: an
    added accent is an extra byte in UTF-8, so every offset after it moves."""
    before = "relacion evidente con otras materias".encode("utf-8")
    after = "relación evidente con otras materias".encode("utf-8")
    assert len(after) == len(before) + 1
    assert before.index(b"evidente") != after.index(b"evidente")
