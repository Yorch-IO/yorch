"""Fixtures for retrieval tests that use the real stores.

No embedding call is made. The question's vector is taken from a point already
in Qdrant, which makes the search genuinely end to end — real index, real
fusion, real filters — while costing nothing and staying deterministic. A random
vector stands in for an off-corpus question.
"""

from __future__ import annotations

import os
import random

import httpx
import pytest

from brainworker.graph.schema import LEGACY_TENANT_ID

from brainworker import config

QDRANT = os.environ.get("BRAIN_QDRANT_URL", "http://127.0.0.1:6433")
COLLECTION = "brain"


@pytest.fixture(scope="session")
def indexed():
    """One real point that exists in **both** stores: Qdrant and the graph.

    Both, because retrieval spans them: Qdrant ranks and the graph supplies the
    locator an answer must cite. Picking any indexed point is not enough — the
    collection also holds points written by activity tests that never projected
    a graph, and a fixture that chose one of those would make the citation test
    fail for a reason that has nothing to do with retrieval.
    """
    try:
        r = httpx.post(
            f"{QDRANT}/collections/{COLLECTION}/points/scroll",
            json={"limit": 64, "with_payload": True, "with_vector": True},
            timeout=5.0,
        )
        r.raise_for_status()
        points = r.json()["result"]["points"]
    except Exception as e:
        pytest.skip(f"no Qdrant collection {COLLECTION!r} at {QDRANT}: {e}")

    if not points:
        pytest.skip(f"collection {COLLECTION!r} is empty — index a document first")

    from brainworker.graph import Graph, GraphError

    url = os.environ.get("BRAIN_MEMGRAPH_URL", "bolt://127.0.0.1:7788")
    by_chunk = {p["payload"].get("chunk_id"): p for p in points if p.get("payload")}
    try:
        with Graph(url, timeout=5.0) as graph:
            cited = {
                r["chunk_id"]
                for r in graph.query(
            "citations_for_chunks",
            {"tenant_id": LEGACY_TENANT_ID, "chunk_ids": [c for c in by_chunk if c], "limit": 200},
                )
            }
    except GraphError as e:
        pytest.skip(f"no Memgraph at {url}: {e}")

    for chunk_id in cited:
        point = by_chunk.get(chunk_id)
        if point is None:
            continue
        vectors = point.get("vector") or {}
        dense = vectors.get("dense") if isinstance(vectors, dict) else vectors
        if not dense:
            continue
        payload = point["payload"]
        # The library id comes from the point itself. The collection holds every
        # library and `search` always filters by one, so a hardcoded name would
        # silently make every retrieval test a test of the empty set.
        return {
            "payload": payload,
            "vector": list(dense),
            "library_id": payload.get("library_id", ""),
        }

    pytest.skip(
        "no indexed point has a citation in the graph — run a full ingest first"
    )


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BRAIN_QDRANT_URL", QDRANT)
    monkeypatch.setenv("BRAIN_GEMINI_PROJECT_ID", "proj-test")
    monkeypatch.setenv(
        "BRAIN_MEMGRAPH_URL",
        os.environ.get("BRAIN_MEMGRAPH_URL", "bolt://127.0.0.1:7788"),
    )
    return config.load()


class VectorProvider:
    """A provider whose only real job is to hand back a prepared vector."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.tasks: list[str] = []

        class _S:
            model = "gemini-3.6-flash"
            embedding_model = "gemini-embedding-2"
            # See the note on the same fields in `test_answer.py`: typed like
            # the real `Gemini` so the reasoning policy can be exercised rather
            # than crashed into.
            stage_thinking: dict[str, int | None] = {}
            thinking_budget: int | None = None

        self.settings = _S()

    def embed(self, texts, *, task, workers=6):
        from brainworker.providers.gemini import Embedding

        self.tasks.append(task)
        return [Embedding(values=self.vector) for _ in texts]


@pytest.fixture
def library(indexed) -> str:
    return indexed["library_id"]


@pytest.fixture
def on_topic(indexed):
    return VectorProvider(indexed["vector"])


@pytest.fixture
def off_topic(indexed):
    rng = random.Random(20260820)
    return VectorProvider([rng.uniform(-1, 1) for _ in indexed["vector"]])
