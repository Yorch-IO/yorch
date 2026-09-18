"""Reading a `chunks.jsonl`, and the two fields that must never reach a page.

`overlap` is the previous chunk's tail, carried so retrieval can show a fragment
in context, and it is non-empty on about **four rows in five** — measured on the
real workspace when the EPUB export was built. A renderer that emitted it would
duplicate a paragraph on nearly every page, which reads as a corrupt book rather
than as a bug. `embed_text` is worse: the breadcrumb and the overlap
concatenated ahead of the text.

`Passage` is a whitelist, so the leak is structurally impossible. This asserts it
anyway, because a whitelist is a guarantee only for as long as nobody widens it.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from brainworker.transform import reading
from brainworker.transform.types import SourceSpan

LEAK = "ZZ-PLANTED-OVERLAP-ZZ"
LEAK_EMBED = "ZZ-PLANTED-EMBEDTEXT-ZZ"


def _rows() -> list[dict]:
    return [
        {
            "index": i,
            "kind": "nota" if i == 4 else "cuerpo",
            "chapter": f"Capítulo {i // 3 + 1}",
            "section": "Sección A" if i % 3 == 0 else "",
            "text": f"Texto del fragmento {i}. Véase https://ejemplo.org/{i}."
            if i == 7
            else f"Texto del fragmento {i}.",
            "context": "",
            "char_from": i * 100,
            "char_to": i * 100 + 40,
            "cell_ref": "",
            "overlap": LEAK,
            "embed_text": LEAK_EMBED,
        }
        for i in range(9)
    ]


def test_a_passage_carries_neither_overlap_nor_embed_text():
    passages = reading.passages_of(_rows())
    dumped = json.dumps([asdict(p) for p in passages], ensure_ascii=False)
    assert LEAK not in dumped
    assert LEAK_EMBED not in dumped
    assert "overlap" not in dumped
    assert "embed_text" not in dumped


def test_neither_field_reaches_the_excerpt_or_a_chapter_s_source_text():
    passages = reading.passages_of(_rows())
    excerpt = reading.excerpt(passages)
    body = reading.text_for(passages, [SourceSpan(first=0, last=8)])
    for text in (excerpt, body):
        assert LEAK not in text
        assert LEAK_EMBED not in text


def test_chapters_are_consecutive_runs_and_not_groups_by_title():
    """Two chapters may share a title — a running header that survived into the
    index does exactly that, and this corpus has measured books where it did.
    Grouping by title would produce one span covering material from opposite
    ends of the document, which every ordering rule downstream would then be
    unable to satisfy.
    """
    rows = [
        {"index": 0, "kind": "cuerpo", "chapter": "A", "text": "x"},
        {"index": 1, "kind": "cuerpo", "chapter": "B", "text": "x"},
        {"index": 2, "kind": "cuerpo", "chapter": "A", "text": "x"},
    ]
    chapters = reading.chapters_of(reading.passages_of(rows))
    assert [(c.title, c.first, c.last) for c in chapters] == [
        ("A", 0, 0),
        ("B", 1, 1),
        ("A", 2, 2),
    ]


def test_a_document_with_no_detected_chapters_becomes_one_untitled_chapter():
    """Exactly what the index holds, and what every breadcrumb on it says."""
    rows = [{"index": i, "kind": "cuerpo", "chapter": "", "text": "x"} for i in range(5)]
    chapters = reading.chapters_of(reading.passages_of(rows))
    assert len(chapters) == 1
    assert chapters[0].title == ""
    assert (chapters[0].first, chapters[0].last) == (0, 4)


def test_the_reference_sweep_takes_footnotes_headings_and_urls():
    passages = reading.passages_of(_rows())
    found = reading.references_of(passages)
    assert "Texto del fragmento 4." in found, "a footnote is a reference"
    assert "https://ejemplo.org/7" in found, "a link is a reference wherever it sits"


def test_a_bibliography_heading_is_matched_and_a_mention_is_not():
    """A heading match, never a body match: a paragraph that mentions the word
    "bibliography" is not a bibliography."""
    rows = [
        {"index": 0, "kind": "cuerpo", "chapter": "Bibliografía",
         "text": "Smith, J. Una obra. 1999."},
        {"index": 1, "kind": "cuerpo", "chapter": "Introducción",
         "text": "La bibliografía de este campo es extensa."},
    ]
    found = reading.references_of(reading.passages_of(rows))
    assert found == ["Smith, J. Una obra. 1999."]


def test_a_reference_is_carried_verbatim_and_never_parsed():
    """A reference that could not be parsed and is carried as its literal string
    is honest; one parsed into a wrong author is not — the rule `bookmeta._clean`
    applies when it refuses to print "desconocido" on a cover."""
    ugly = "vid. supra, n. 14; cf. tambien el apendice (sin paginar)"
    rows = [{"index": 0, "kind": "nota", "chapter": "", "text": ugly}]
    assert reading.references_of(reading.passages_of(rows)) == [ugly]


def test_the_sweep_is_capped_and_deduplicated():
    rows = [
        {"index": i, "kind": "nota", "chapter": "", "text": "La misma referencia."}
        for i in range(50)
    ]
    assert reading.references_of(reading.passages_of(rows)) == ["La misma referencia."]

    rows = [
        {"index": i, "kind": "nota", "chapter": "", "text": f"Referencia {i}."}
        for i in range(reading.MAX_REFERENCES + 80)
    ]
    assert len(reading.references_of(reading.passages_of(rows))) == reading.MAX_REFERENCES


def test_text_for_folds_the_headings_back_in():
    """A chapter composed from the middle of a book still has to know what part
    of it that was — and the chunker *consumes* a heading paragraph, so reading
    only `text` would hand the model material with its own titles removed."""
    passages = reading.passages_of(_rows())
    body = reading.text_for(passages, [SourceSpan(first=3, last=5)])
    assert "# Capítulo 2" in body
    assert "Texto del fragmento 3." in body
    assert "Texto del fragmento 0." not in body
