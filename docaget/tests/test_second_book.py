"""Four things that only broke once a *second* book from the same corpus arrived.

The fingerprint identifies a family of documents, which is exactly what makes rule
reuse pay for itself — and exactly what made the second book of this corpus inherit
the first book's synthetic questions and score 0.000. The fix has to keep the reuse
and drop the questions, so both halves are pinned here.

The other three are smaller and were all found by hand-running the pipeline:
a `--doc` filter that could never match, a `--dry-run` that wrote a profile it had
not measured, and a review-question discriminator that existed in two copies.
"""

from __future__ import annotations

import json

import pytest

from docagent import cli
from docagent import chunk as ck
from docagent import graph as g
from docagent import profiles as prof
from docagent import rules as rl
from docagent.chunk import ChunkRules, DocRules


def _profile(**kw) -> prof.Profile:
    kw.setdefault("doc_rules", DocRules())
    kw.setdefault("chunk_rules", ChunkRules())
    return prof.Profile(fingerprint="fp", slug="slug", extractor="pdf_text", **kw)


# --- 1. a shared fingerprint must not share an eval set ----------------------


def test_a_profile_from_another_document_keeps_its_rules_and_loses_its_evalset(monkeypatch):
    """The bug: book 2 matched book 1's fingerprint, inherited its 40 questions —
    every one about a passage book 2 does not contain — and scored 0.000, which the
    tuning loop then treated as a baseline."""
    learned = _profile(
        doc_rules=DocRules(header_patterns=("EL PAPADO",)),
        evalset=[prof.EvalItem(question="¿quién fue Arrio?", chunk_index=1, char_mid=10)],
        scores=prof.Scores(recall_at_5=0.925, mrr_at_10=0.747),
        learned_from="libros/1. AGUSTIN.pdf",
    )
    monkeypatch.setattr(prof, "load", lambda fp: learned)

    out = g.n_load_profile({"fingerprint": "fp", "path": "libros/2. PAPADO.pdf"}, None)

    assert out["profile_source"] == "reused", "the rules are still worth reusing"
    assert out["profile"].doc_rules.header_patterns == ("EL PAPADO",)
    assert out["profile"].evalset == []
    assert out["profile"].scores.recall_at_5 == 0.0, "a stale baseline is worse than none"
    assert out["profile"].learned_from == "libros/2. PAPADO.pdf"
    assert any("eval set dropped" in line for line in out["log"])
    assert out["profile"].slug == prof.slug_for("libros/2. PAPADO.pdf", "fp"), (
        "keeping the inherited slug made this profile save over the file of the "
        "document it was learned from, destroying that document's measured scores"
    )


def test_the_same_document_keeps_its_own_evalset(monkeypatch):
    """The complement, and the one that makes re-runs free: re-indexing the *same*
    file must not pay to regenerate questions it already has."""
    item = prof.EvalItem(question="¿quién fue Arrio?", chunk_index=1, char_mid=10)
    learned = _profile(evalset=[item], learned_from="libros/1. AGUSTIN.pdf")
    monkeypatch.setattr(prof, "load", lambda fp: learned)

    out = g.n_load_profile({"fingerprint": "fp", "path": "libros/1. AGUSTIN.pdf"}, None)

    assert out["profile"].evalset == [item]


def test_no_profile_still_means_learn_from_scratch(monkeypatch):
    monkeypatch.setattr(prof, "load", lambda fp: None)
    out = g.n_load_profile({"fingerprint": "fp", "path": "x.pdf"}, None)
    assert out["profile"] is None and out["profile_source"] == "default"
    assert g.e_needs_learning(out) == "propose"


# --- 2. a dry run has nothing to record --------------------------------------


def test_dry_run_ends_without_persisting():
    """It measures nothing, so routing it to persist would overwrite a profile that
    cost real money with empty scores and a bumped revision count."""
    assert g.e_needs_tuning({"dry_run": True}) == "__end__"
    # And it must not be diverted into the loop either, which is what happened
    # before: no scores and no eval set, so the edge fell through to persist.
    assert g.e_needs_tuning({"dry_run": True, "revert_reindex": True}) == "__end__"


def test_dry_run_end_is_a_declared_destination():
    """add_conditional_edges validates against the list it was given, so a return
    value missing from it fails at invoke time, not at import."""
    from langgraph.graph import END

    assert g.e_needs_tuning({"dry_run": True}) == END


# --- 3. --doc took a path where a doc_id was compared ------------------------


def test_doc_filter_is_hashed_the_same_way_the_payload_was():
    """`--doc libros/x.pdf` compared the raw path against the stored doc_id, so the
    filter matched nothing and every restricted query came back empty."""
    from docagent.qdrant import doc_id_for

    assert doc_id_for("libros/x.pdf") != "libros/x.pdf"
    assert doc_id_for("libros/x.pdf") == doc_id_for("libros/x.pdf")


# --- 4. the review-question discriminator exists once ------------------------


def test_imperatives_is_one_object_shared_by_chunk_and_rules():
    """Two copies of one discriminator drifting apart is the founding bug of this
    project: a checking regex that required a dot the implementation made optional
    pronounced a rule clean while it mislabelled nine footnotes."""
    assert rl._IMPERATIVES is ck.IMPERATIVES


def test_a_numbered_imperative_is_a_review_question_not_a_chapter():
    """The sequence check saw chapters [1,2,3,4,11,12,…,17] on the first book: the
    review questions at the end were being read as level-1 headings."""
    rules = ChunkRules()
    assert ck.heading_level("11. Defina Arrianismo", rules) == 0
    assert ck.heading_level("2. Señale las causas del cisma", rules) == 0
    assert ck.heading_level("¿Qué es el arrianismo?", rules) == 0
    # ...without swallowing a real heading that merely starts with a number.
    assert ck.heading_level("2. EL PAPADO Y EL MONAQUISMO", rules) == 1


# --- 5. diagnostic suites are per-document data ------------------------------


def test_suite_is_read_from_the_sidecar_beside_the_document(tmp_path):
    doc = tmp_path / "2. PAPADO.pdf"
    doc.write_bytes(b"%PDF")
    (tmp_path / "2. PAPADO.pdf.diag.json").write_text(
        json.dumps({"on_topic": ["¿origen del papado?"], "exact": ["Cluny"]}),
        encoding="utf-8",
    )
    suite, note = cli.diag_suite(str(doc))
    assert suite == {"on_topic": ("¿origen del papado?",), "exact": ("Cluny",)}
    assert "2. PAPADO.pdf.diag.json" in note


def test_a_missing_sidecar_says_so_out_loud(tmp_path):
    """Silently falling back asks a church-history book about Dooyeweerd and
    hilemorfismo, and the resulting zeros read as a retrieval failure."""
    doc = tmp_path / "sin-suite.pdf"
    doc.write_bytes(b"%PDF")
    suite, note = cli.diag_suite(str(doc))
    assert suite is cli.DEFAULT_DIAG_SUITE
    assert "NO SIDECAR" in note


def test_an_incomplete_sidecar_is_an_error_not_a_partial_run(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"on_topic": ["x"]}), encoding="utf-8")
    with pytest.raises(SystemExit, match="exact"):
        cli.diag_suite("", str(p))


def test_the_shipped_sidecars_parse():
    """They are data, and data in the repo rots quietly."""
    import pathlib

    for path in pathlib.Path("libros").rglob("*.diag.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["on_topic"] and data["exact"], path


def test_the_newest_profile_wins_when_two_files_share_a_fingerprint(tmp_path, monkeypatch):
    """Observed on the real corpus: `profiles/` held two files for fingerprint
    110b1333 — one with eight measured revisions, one left over from an aborted
    experiment with all scores at 0.0. `load` returned the first in sorted order,
    so which profile the family used depended on the title of the book that had
    learned it."""
    monkeypatch.setattr(prof, "PROFILE_DIR", tmp_path)
    from dataclasses import replace

    stale = replace(
        _profile(learned_from="2.pdf", learned_at=100.0, revisions=1), slug="1-papado-fp"
    )
    fresh = replace(
        _profile(learned_from="1.pdf", learned_at=200.0, revisions=8), slug="2-agustin-fp"
    )
    stale.save()
    fresh.save()

    got = prof.load("fp")
    assert got is not None and got.slug == "2-agustin-fp", "picked by name, not by recency"
    assert got.revisions == 8
