"""The narrowings a recording corpus makes askable: dates, a source, a passage.

Two layers. The pure half — how a question's fields become a filter, and how
the engine renders three filter shapes — needs no store. The other half writes
a handful of points into a **disposable** collection on the real Qdrant and
searches them with the filters, because the property that matters is that
`recorded_day` really is compared as an integer range and `scripture_refs`
really is matched as a list, and a fake would agree with whatever this file
assumed.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from brainworker import scripture
from brainworker.answering.retrieve import EPOCH_ORDINAL, narrowings
from brainworker.answering.types import Question
from brainworker.indexing import INTEGER_INDEXES, PAYLOAD_INDEXES, QdrantWriter
from docagent.bm25 import SparseVector
from docagent.qdrant import Qdrant

QDRANT = os.environ.get("BRAIN_QDRANT_URL", "http://127.0.0.1:6433")


# --- pure --------------------------------------------------------------------


def test_the_epoch_constant_is_what_the_writer_and_the_filter_both_mean():
    from datetime import date

    from brainworker.activities import paid

    assert EPOCH_ORDINAL == date(1970, 1, 1).toordinal()
    assert paid.EPOCH_ORDINAL == EPOCH_ORDINAL


def test_narrowings_are_absent_by_default_and_shaped_by_value():
    q = Question(library_id="lib", text="x")
    assert narrowings(q) == {}
    q = Question(library_id="lib", text="x", recorded_from="1993-01-01", recorded_to="1999-12-31",
                 scripture="Rom 8:28", source_name="iVoox")
    out = narrowings(q)
    assert out["source_name"] == "iVoox"
    assert out["recorded_day"] == {"gte": 8401, "lte": 10956}
    assert out["scripture_refs"] == ["Romanos 8:28"]
    assert "scripture_chapters" not in out


def test_a_chapter_narrows_on_the_chapter_list_and_a_bare_book_on_nothing():
    assert narrowings(Question(library_id="l", text="x", scripture="Juan 3")) == {
        "scripture_chapters": ["Juan 3"]
    }
    assert narrowings(Question(library_id="l", text="x", scripture="Juan")) == {}
    assert narrowings(Question(library_id="l", text="x", scripture="a las 10:30")) == {}


def test_one_bound_is_an_open_range():
    assert narrowings(Question(library_id="l", text="x", recorded_from="2020-01-01")) == {
        "recorded_day": {"gte": 18262}
    }


def test_the_engine_renders_the_three_shapes():
    q = Qdrant(QDRANT, "unused")
    rendered = q._filter({
        "library_id": "lib", "recorded_day": {"gte": 1, "lte": 9}, "scripture_refs": ["Juan 3:16"],
    })
    assert rendered == {"must": [
        {"key": "library_id", "match": {"value": "lib"}},
        {"key": "recorded_day", "range": {"gte": 1, "lte": 9}},
        {"key": "scripture_refs", "match": {"any": ["Juan 3:16"]}},
    ]}
    assert q._filter({}) is None


def test_the_payload_lists_come_from_the_chunks_own_text():
    refs, chapters = scripture.payload_fields("Lean Juan 3:16 y Juan 3:17; luego Rom. 8.")
    assert refs == ["Juan 3:16", "Juan 3:17"]
    assert chapters == ["Juan 3"]


# --- against the real Qdrant, in a collection nobody else uses ---------------------


class _Chunk:
    def __init__(self, index: int, text: str) -> None:
        self.index = index
        self.text = text
        self.kind = "transcripcion"
        self.chapter = ""
        self.section = ""
        self.context = ""
        self.char_from = 0
        self.char_to = len(text)
        self.cell_ref = None

    def breadcrumb(self) -> str:
        return ""


class _Row:
    def __init__(self, chunk: _Chunk, dense: list[float]) -> None:
        self.chunk = chunk
        self.dense = dense
        self.sparse = SparseVector(indices=[1], values=[1.0])


@pytest.fixture
def collection():
    name = f"test_narrowings_{uuid.uuid4().hex[:8]}"
    try:
        httpx.get(f"{QDRANT}/collections", timeout=3.0).raise_for_status()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Qdrant at {QDRANT}: {e}")
    yield name
    httpx.delete(f"{QDRANT}/collections/{name}", timeout=5.0)


def _write(q: Qdrant, *, version: str, day: int | None, source: str, texts: list[str]) -> None:
    writer = QdrantWriter(
        q, tenant_id="tnt_000000000000000000000009", library_id="lib_s3_test",
        document_id=f"doc_{version}", version_id=version, source_title=version,
        model="test", dimensions=4, recorded_day=day, source_name=source,
    )
    rows = [_Row(_Chunk(i, t), [1.0, 0.0, 0.0, 0.0]) for i, t in enumerate(texts)]
    writer.upsert(rows)


def _search(q: Qdrant, filters: dict) -> list[str]:
    body = {
        "vector": {"name": "dense", "vector": [1.0, 0.0, 0.0, 0.0]},
        "limit": 20, "with_payload": True,
        "filter": q._filter(filters),
    }
    r = httpx.post(f"{QDRANT}/collections/{q.collection}/points/search", json=body, timeout=5.0)
    r.raise_for_status()
    return sorted(p["payload"]["text"] for p in r.json()["result"])


def test_the_filters_narrow_real_points_by_day_source_and_passage(collection):
    with Qdrant(QDRANT, collection) as q:
        q.create(4, PAYLOAD_INDEXES, integer_indexes=INTEGER_INDEXES)
        # 1995-04-02 is day 9222; 2020-05-10 is day 18392.
        _write(q, version="ver_1995", day=9222, source="iVoox",
               texts=["Lean Juan 3:16 con atención", "y ahora Romanos 8:28"])
        _write(q, version="ver_2020", day=18392, source="Anchor",
               texts=["Juan 3:16 otra vez", "sin cita alguna"])
        _write(q, version="ver_undated", day=None, source="",
               texts=["Romanos 8:1 sin fecha"])

        scope = {"library_id": "lib_s3_test"}
        assert len(_search(q, scope)) == 5

        nineties = _search(q, {**scope, "recorded_day": {"gte": 8401, "lte": 10956}})
        assert nineties == ["Lean Juan 3:16 con atención", "y ahora Romanos 8:28"]

        # An undated document is absent from every range, never in 1970.
        assert _search(q, {**scope, "recorded_day": {"gte": 0, "lte": 30000}}) == \
            sorted(["Lean Juan 3:16 con atención", "y ahora Romanos 8:28",
                    "Juan 3:16 otra vez", "sin cita alguna"])

        assert _search(q, {**scope, "source_name": "Anchor"}) == ["Juan 3:16 otra vez", "sin cita alguna"]

        assert _search(q, {**scope, "scripture_refs": ["Juan 3:16"]}) == \
            ["Juan 3:16 otra vez", "Lean Juan 3:16 con atención"]
        assert _search(q, {**scope, "scripture_chapters": ["Romanos 8"]}) == \
            ["Romanos 8:1 sin fecha", "y ahora Romanos 8:28"]

        # Combined: the nineties AND the verse.
        assert _search(q, {**scope, "recorded_day": {"lte": 10956}, "scripture_refs": ["Juan 3:16"]}) == \
            ["Lean Juan 3:16 con atención"]

        # The indexes exist on the collection, integer for the day.
        info = httpx.get(f"{QDRANT}/collections/{collection}", timeout=5.0).json()["result"]
        schema = info["payload_schema"]
        assert schema["recorded_day"]["data_type"] == "integer"
        assert schema["scripture_refs"]["data_type"] == "keyword"


def test_creating_the_collection_twice_adds_a_new_index_to_an_existing_one(collection):
    """The early return that would have left `brain` unindexed for every filter
    added after it existed. Measured rather than assumed: the second `create`
    must add the index the first did not ask for."""
    with Qdrant(QDRANT, collection) as q:
        q.create(4, ("library_id",))
        info = httpx.get(f"{QDRANT}/collections/{collection}", timeout=5.0).json()["result"]
        assert "recorded_day" not in info["payload_schema"]
        q.create(4, PAYLOAD_INDEXES, integer_indexes=INTEGER_INDEXES)
        info = httpx.get(f"{QDRANT}/collections/{collection}", timeout=5.0).json()["result"]
        assert info["payload_schema"]["recorded_day"]["data_type"] == "integer"
