"""Qdrant over raw REST: hybrid collection, upsert, RRF fusion, diversification.

Port of ``sociologia/index/qdrant.go``. All payload shapes here were verified
against a running Qdrant v1.18 before being written.

Inherited invariants:

8.  RRF scores are reciprocal ranks, **not** cosines — measured at 1.0 / 0.333 /
    0.25 for three documents. So a cosine threshold can only be applied to the
    dense prefetch, never to the fused output.
9.  Off-topic queries are gated by the dense leg, not by a BM25 threshold.
    Measured: "cómo cambiar el aceite de un motor diésel" scores 5.58 on the
    BM25 leg because the corpus contains those words — above "Gén. 2:15" (3.82)
    and level with "el cogito cartesiano" (5.53). No lexical threshold separates
    noise from real exact-term queries.
10. Point IDs are deterministic UUIDv5, so re-running overwrites instead of
    duplicating.
13. Named vectors are an incompatible schema change: switching to hybrid
    requires recreating the collection.
14. The IDF lives in Qdrant via ``modifier: "idf"``, not in the stored vectors.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from .bm25 import SparseVector

DENSE_VEC = "dense"
SPARSE_VEC = "bm25"
UPSERT_BATCH = 64
READY_TIMEOUT = 60.0
PREFETCH_LIMIT = 50

# Fixed namespace for this project's point IDs, generated once.
_NAMESPACE = uuid.UUID("6b686569-726f-5e73-8f63-696f6c6f6779")


def point_id(doc_id: str, chunk_index: int) -> str:
    """Deterministic UUIDv5 over ``doc_id:chunk_index``."""
    return str(uuid.uuid5(_NAMESPACE, f"{doc_id}:{chunk_index}"))


def doc_id_for(path: str) -> str:
    """Stable short id for a source document, used to filter a corpus by file."""
    name = path.rsplit("/", 1)[-1]
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name}:{digest}"


@dataclass
class Point:
    id: str
    dense: list[float]
    sparse: SparseVector
    payload: dict[str, Any]

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "vector": {
                DENSE_VEC: self.dense,
                SPARSE_VEC: self.sparse.as_payload(),
            },
            "payload": self.payload,
        }


@dataclass
class SearchOpts:
    limit: int = 5
    min_score: float = 0.60  # cosine floor, dense leg only
    dense_only: bool = False
    query_text: str = ""  # needed to build the sparse vector
    filters: dict[str, str] = field(default_factory=dict)  # payload key -> value


@dataclass
class Hit:
    score: float
    payload: dict[str, Any]


@dataclass
class CollectionInfo:
    status: str
    points_count: int
    legacy: bool  # single unnamed vector, i.e. pre-hybrid schema


class QdrantError(RuntimeError):
    pass


class Qdrant:
    def __init__(
        self,
        base_url: str = "http://localhost:6333",
        collection: str = "docagent",
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.collection = collection
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Qdrant":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- plumbing -----------------------------------------------------------

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        resp = self._client.request(method, f"{self.base_url}{path}", json=body)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text

    def _ok(self, method: str, path: str, body: dict | None = None) -> Any:
        status, data = self._call(method, path, body)
        if status != 200:
            raise QdrantError(f"{method} {path} -> HTTP {status}: {str(data)[:400]}")
        return data

    def wait_ready(self, timeout: float = READY_TIMEOUT) -> None:
        """Poll /healthz. docker-compose has no healthcheck because the
        qdrant/qdrant image ships neither curl nor wget, so readiness is the
        client's job — that is what makes ``up -d`` then index safe back to back.
        """
        deadline = time.monotonic() + timeout
        last = "no attempt made"
        while time.monotonic() < deadline:
            try:
                resp = self._client.get(f"{self.base_url}/healthz")
                if resp.status_code == 200:
                    return
                last = f"HTTP {resp.status_code}"
            except httpx.HTTPError as e:
                last = str(e)
            time.sleep(1.0)
        raise QdrantError(
            f"qdrant at {self.base_url} not ready after {timeout}s ({last})\n"
            "is it up? try: docker compose up -d"
        )

    # --- collection ---------------------------------------------------------

    def exists(self) -> bool:
        status, _ = self._call("GET", f"/collections/{self.collection}")
        if status == 200:
            return True
        if status == 404:
            return False
        raise QdrantError(f"unexpected HTTP {status} checking collection")

    def drop(self) -> None:
        self._call("DELETE", f"/collections/{self.collection}")

    def create(self, dims: int, payload_indexes: tuple[str, ...] = ()) -> None:
        """Create the hybrid collection if it does not exist.

        Vectors stay in RAM: a few hundred points at 3072 dims is single-digit MB.
        """
        if self.exists():
            return
        self._ok(
            "PUT",
            f"/collections/{self.collection}",
            {
                "vectors": {
                    # gemini-embedding-001 returns L2-normalised vectors at the
                    # full 3072 dims, so cosine needs no renormalisation here.
                    # Truncating to 768/1536 via Matryoshka would.
                    DENSE_VEC: {"size": dims, "distance": "Cosine"}
                },
                "sparse_vectors": {SPARSE_VEC: {"modifier": "idf"}},
            },
        )
        for fieldname in payload_indexes:
            self._ok(
                "PUT",
                f"/collections/{self.collection}/index?wait=true",
                {"field_name": fieldname, "field_schema": "keyword"},
            )

    def info(self) -> CollectionInfo:
        data = self._ok("GET", f"/collections/{self.collection}")
        result = data["result"]
        vectors = result["config"]["params"].get("vectors") or {}
        # An unnamed config serialises as {"size":…,"distance":…}; a named one as
        # {"dense":{…}}. The presence of the dense key is the discriminator.
        legacy = DENSE_VEC not in vectors
        return CollectionInfo(
            status=result.get("status", "unknown"),
            points_count=int(result.get("points_count") or 0),
            legacy=legacy,
        )

    def upsert(self, points: list[Point]) -> None:
        for start in range(0, len(points), UPSERT_BATCH):
            batch = points[start : start + UPSERT_BATCH]
            self._ok(
                "PUT",
                f"/collections/{self.collection}/points?wait=true",
                {"points": [p.as_json() for p in batch]},
            )

    def scroll(
        self, filters: "dict[str, str] | None" = None
    ) -> list[dict[str, Any]]:
        """Page through matching points as ``{"id": ..., "payload": {...}}``.

        Two things this gives an audit that :meth:`scroll_all` cannot. The
        **filter** keeps a per-document read off the whole collection — one
        collection holds every library of every organisation here. And the
        **id**, which `scroll_all` drops: a point id is
        ``point_id(version_id, chunk_index)``, so checking that the ids present
        are exactly the ones the version derives is what detects a tail left
        behind by a longer previous chunking. A payload alone cannot say that,
        because a stale point's payload is perfectly well-formed.

        Shares `_filter` with search and removal, so an audit cannot select a
        set that a search would have read differently. An empty filter is
        allowed here, unlike in the removal path: reading every point is what
        `scroll_all` has always meant.
        """
        out: list[dict[str, Any]] = []
        offset: Any = None
        selector = self._filter(filters or {})
        while True:
            body: dict[str, Any] = {
                "limit": 256,
                "with_payload": True,
                "with_vector": False,
            }
            if selector:
                body["filter"] = selector
            if offset is not None:
                body["offset"] = offset
            result = self._ok(
                "POST", f"/collections/{self.collection}/points/scroll", body
            )["result"]
            out.extend(
                {"id": p["id"], "payload": p["payload"]} for p in result["points"]
            )
            offset = result.get("next_page_offset")
            if offset is None:
                return out

    def scroll_all(self) -> list[dict[str, Any]]:
        """Page through every point's payload, for whole-collection audits."""
        return [p["payload"] for p in self.scroll()]

    # --- removal ------------------------------------------------------------
    #
    # Both of these share `_filter` with search, so a caller cannot construct a
    # selector for deletion that search would have read differently. They also
    # both refuse an empty filter: `_filter({})` is `None`, and Qdrant reads a
    # missing filter as "every point in the collection" — which is `drop()`, and
    # must never be something you reach by passing an empty dict by accident.

    def count(self, filters: dict[str, str]) -> int:
        """How many points match. Exact, because it is used to report a deletion.

        `exact: false` returns an estimate from the segment metadata, which is
        fine for a progress bar and wrong for telling a user how much of their
        library just disappeared.
        """
        body: dict[str, Any] = {"exact": True}
        if f := self._filter(filters):
            body["filter"] = f
        return int(
            self._ok("POST", f"/collections/{self.collection}/points/count", body)[
                "result"
            ]["count"]
        )

    def delete_by_filter(self, filters: dict[str, str]) -> int:
        """Delete every point matching `filters`; returns how many there were.

        `wait=true` is not optional here. Without it the call returns once the
        operation is *queued*, so a verification read immediately afterwards
        still sees the points and reports a deletion that worked as one that did
        not.

        Counting first costs a second round trip and is what lets the caller say
        what it removed: Qdrant's delete response carries an operation id and a
        status, never a count.
        """
        if not filters:
            raise QdrantError("refusing to delete with no filter; use drop()")
        removed = self.count(filters)
        if removed == 0:
            return 0
        self._ok(
            "POST",
            f"/collections/{self.collection}/points/delete?wait=true",
            {"filter": self._filter(filters)},
        )
        return removed

    def prune_tail(self, doc_id: str, keep: int) -> int:
        """Delete any point for `doc_id` whose `chunk_index` is >= `keep`.

        A rejected chunk-tuning candidate re-indexes fewer chunks than it just
        wrote, and `upsert` only overwrites the ids the new run actually
        produces (`0..keep-1`) — it never deletes ids beyond them. Observed on
        `07-LlavesDelPoder-INT.pdf` (2026-08-30): a 625-chunk candidate was
        rejected in favour of a 502-chunk one, and ids `502..624` — still
        carrying the rejected candidate's `char_span` — survived in the
        collection, silently duplicating the back third of the book under two
        incompatible sets of chunk boundaries.

        Point ids are deterministic (`point_id(doc_id, i)`), so the stale
        range is exactly `[keep, old_count)` and needs no range filter — a
        plain equality filter cannot express ">=" and `count`/`delete_by_filter`
        both require one.
        """
        old_count = self.count({"doc_id": doc_id})
        if old_count <= keep:
            return 0
        return self.delete_by_ids(
            [point_id(doc_id, i) for i in range(keep, old_count)]
        )

    def delete_by_ids(self, ids: "Sequence[str]") -> int:
        """Delete exactly these points.

        Separate from `delete_by_filter` because a filter here is equality-only
        and cannot express a range. The caller that knows the ids is the one that
        derived them, and the two hosts derive them differently: this engine from
        `doc_id`, the product from a content-derived `version_id`. Hence a
        primitive rather than a second `prune_tail`.
        """
        if not ids:
            return 0
        self._ok(
            "POST",
            f"/collections/{self.collection}/points/delete?wait=true",
            {"points": list(ids)},
        )
        return len(ids)

    def set_payload(self, filters: dict[str, str], payload: dict[str, Any]) -> int:
        """Overwrite the named payload keys on every matching point.

        A merge, not a replace: keys absent from `payload` are left alone, which
        is what makes it safe to re-point one field on a point whose other
        fields — `text`, `char_span`, `chunk_id` — must not be touched.
        """
        if not filters:
            raise QdrantError("refusing to set payload with no filter")
        if not payload:
            return 0
        affected = self.count(filters)
        if affected == 0:
            return 0
        self._ok(
            "POST",
            f"/collections/{self.collection}/points/payload?wait=true",
            {"payload": payload, "filter": self._filter(filters)},
        )
        return affected

    # --- retrieval ----------------------------------------------------------

    def _filter(self, filters: dict[str, str]) -> dict | None:
        if not filters:
            return None
        return {
            "must": [
                {"key": k, "match": {"value": v}} for k, v in sorted(filters.items())
            ]
        }

    def search(self, vector: list[float], opts: SearchOpts) -> list[Hit]:
        """Dense-only or hybrid retrieval, with RRF fusion on the server.

        The cosine threshold goes on the dense prefetch, never on the fused
        output (invariant #8).
        """
        if opts.dense_only:
            body: dict[str, Any] = {
                "query": vector,
                "using": DENSE_VEC,
                "limit": opts.limit,
            }
            if opts.min_score > 0:
                body["score_threshold"] = opts.min_score
        else:
            dense: dict[str, Any] = {
                "query": vector,
                "using": DENSE_VEC,
                "limit": PREFETCH_LIMIT,
            }
            if opts.min_score > 0:
                dense["score_threshold"] = opts.min_score
            from .bm25 import query_sparse_vector  # local: avoids a cycle at import

            sparse = {
                "query": query_sparse_vector(opts.query_text).as_payload(),
                "using": SPARSE_VEC,
                "limit": PREFETCH_LIMIT,
            }
            body = {
                "prefetch": [dense, sparse],
                "query": {"fusion": "rrf"},
                "limit": opts.limit,
            }

        body["with_payload"] = True
        if f := self._filter(opts.filters):
            body["filter"] = f

        result = self._ok(
            "POST", f"/collections/{self.collection}/points/query", body
        )["result"]
        return [Hit(score=p["score"], payload=p.get("payload") or {}) for p in result["points"]]

    def topicality_gate(self, vector: list[float], opts: SearchOpts) -> bool:
        """Does anything clear the cosine floor on the dense leg?

        ``score_threshold`` can only be applied to the dense prefetch, so on its
        own it does not stop an off-topic query from returning purely lexical
        matches. The dense leg therefore decides whether the query is about this
        corpus at all; if nothing clears the floor, the caller suppresses the
        lexical hits. Costs one extra round-trip and no tokens — the query vector
        is already in hand. (Invariant #9.)
        """
        if opts.min_score <= 0:
            return True
        probe = SearchOpts(
            limit=1,
            min_score=opts.min_score,
            dense_only=True,
            query_text=opts.query_text,
            filters=opts.filters,
        )
        return bool(self.search(vector, probe))


def diversify(hits: list[Hit], per_section: int, top_k: int) -> list[Hit]:
    """Cap how many results may come from one section, then backfill.

    Measured without it, an on-topic query spent 7 of its 10 slots on
    near-identical chunks of a single section — fewer distinct sections than a
    nonsense query got. With a cap of 2, the same queries returned 8-9 distinct
    sections out of 10.
    """
    if per_section <= 0:
        return hits[:top_k]

    seen: dict[str, int] = {}
    out: list[Hit] = []
    for h in hits:
        key = str(h.payload.get("breadcrumb", ""))
        if seen.get(key, 0) >= per_section:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(h)
        if len(out) == top_k:
            return out

    # The cap left us short (few distinct sections matched): backfill in score
    # order rather than return fewer results than asked for.
    have = {id(h) for h in out}
    for h in hits:
        if len(out) == top_k:
            break
        if id(h) not in have:
            out.append(h)
    return out
