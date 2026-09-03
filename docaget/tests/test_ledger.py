"""The ledger's file is a history now, and this is what that has to mean.

`dump` was `json.dump` over the whole file, so `costo.json` held exactly one
run: the last. That is the mechanism behind `INFORME_INDEXACION.md`'s statement
that per-document accounting "does not exist and is not reconstructible" — 28
documents were indexed and the file could account for one of them. These tests
stand on the properties that fix costs nothing to keep and everything to lose
again.
"""

from __future__ import annotations

import json

from docagent.ledger import HISTORY_VERSION, Ledger


def _ledger(stage: str, model: str = "gemini-3.6-flash", *, out: int = 1000) -> Ledger:
    led = Ledger()
    led.record(stage, model, input_tokens=1000, output_tokens=out)
    return led


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_second_run_does_not_erase_the_first(tmp_path):
    path = tmp_path / "costo.json"
    _ledger("correct").dump(str(path), run_id="uno", documents=["a.pdf"])
    _ledger("embed", "gemini-embedding-001").dump(
        str(path), run_id="dos", documents=["b.pdf"]
    )

    got = _read(path)
    assert [r["run_id"] for r in got["runs"]] == ["uno", "dos"]
    assert [r["documents"] for r in got["runs"]] == [["a.pdf"], ["b.pdf"]]


def test_the_total_is_the_corpus_and_not_the_last_run(tmp_path):
    # The defect as it was read on 2026-09-03: `total_usd` said $1.335820, which
    # was one book's bill standing in for seven.
    path = tmp_path / "costo.json"
    _ledger("correct").dump(str(path), run_id="uno")
    _ledger("correct").dump(str(path), run_id="dos")

    got = _read(path)
    one = got["runs"][0]["total_usd"]
    assert one > 0
    assert got["total_usd"] == round(one * 2, 6)
    assert got["runs_recorded"] == 2


def test_a_file_in_the_old_shape_becomes_the_first_run(tmp_path):
    # The migration has to be lossless in the direction that matters: the file
    # on disk is the only record of that spend.
    path = tmp_path / "costo.json"
    before = _ledger("correct").to_dict()
    path.write_text(json.dumps(before), encoding="utf-8")

    _ledger("embed", "gemini-embedding-001").dump(str(path), run_id="nuevo")

    got = _read(path)
    assert got["version"] == HISTORY_VERSION
    assert len(got["runs"]) == 2
    migrated = got["runs"][0]
    assert migrated["stages"] == before["stages"]
    assert migrated["total_usd"] == before["total_usd"]
    # Unknown, and said so. `null` is not `[]`: "nobody recorded which documents"
    # and "this run touched none" are different claims.
    assert migrated["run_id"] is None
    assert migrated["at"] is None
    assert migrated["documents"] is None


def test_an_unreadable_file_is_moved_aside_rather_than_overwritten(tmp_path):
    # It is somebody's record of money already spent. The running process is not
    # entitled to decide it was worthless — and the current run must still land.
    path = tmp_path / "costo.json"
    path.write_text("{esto no es json", encoding="utf-8")

    _ledger("correct").dump(str(path), run_id="nuevo")

    kept = list(tmp_path.glob("costo.json.roto-*"))
    assert len(kept) == 1
    assert kept[0].read_text(encoding="utf-8") == "{esto no es json"
    assert [r["run_id"] for r in _read(path)["runs"]] == ["nuevo"]


def test_an_unpriced_stage_is_reported_by_the_history_too(tmp_path):
    # None is not zero, and the property has to survive the aggregation: a
    # corpus total that quietly omits an unpriced stage is the same lie one run
    # at a time.
    path = tmp_path / "costo.json"
    _ledger("correct", "modelo-sin-precio").dump(str(path), run_id="uno")

    got = _read(path)
    assert got["unpriced_stages"] == ["correct"]
    assert got["total_usd"] == 0.0
    assert got["runs"][0]["stages"][0]["cost_usd"] is None


def test_a_dump_leaves_no_temporary_file_behind(tmp_path):
    # The write is atomic because the file is now irreplaceable: a crash
    # mid-write would take every earlier run with it.
    path = tmp_path / "costo.json"
    _ledger("correct").dump(str(path), run_id="uno")
    assert [p.name for p in tmp_path.iterdir()] == ["costo.json"]
