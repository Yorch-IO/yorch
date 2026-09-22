"""Vertex AI's Ranking API: a cross-encoder over the fused candidates.

The one thing this product had none of, measured before it was built. On 640
eval questions across eight real books (2026-09-21, `scripts/rerank_ceiling.py`)
the fused RRF list held the wanted chunk far more often than the served
`top_k` did — a gap of **+0.100 recall at `brief` and +0.073 at `standard`**
against a pooled noise margin of about ±0.014 — and reranking those same
candidates with `semantic-ranker-default-005` recovered **+0.094 and +0.066** of
it, sixteen (book, level) pairs improved and none regressed, MRR inside the
served window up by about 0.2 at both. At `thorough` the ceiling was +0.020,
inside the noise, because 48 of 120 already holds nearly everything reachable;
that level does not rerank, and the reason is a measurement rather than a
budget. Latency p50 0.22 s, p90 0.28 s over 1,200 calls.

**No key, no new dependency, ``locations/global``.** ADC through
``google.auth`` (already installed under ``google-genai``) and one ``httpx``
POST, so the image keeps its "no compiler" property and this call carries the
same auth posture as every other one the product makes. It is a *different
service* from Gemini — ``discoveryengine.googleapis.com`` — and needed no extra
role on this installation, which was checked by calling it, not assumed.

**Scores are passed through, never re-normalised.** RAGFlow's
``rerank_model.py::_normalize_rank`` states the contract this borrows: min-max
only a provider whose scores fall outside [0, 1], and pass a calibrated one
through unchanged so a downstream threshold keeps its meaning. This API answers
in [0, 1] already, and re-scaling a batch of three would turn the worst
candidate into a zero it did not earn.

**The reranker sees the chunk's own text, never ``embed_text``.** Their
reason, kept: neural rerankers "score stemmed / accent-split tokens far lower",
and a breadcrumb plus an overlap ahead of the passage is a different document
from the passage.

**Priced per query, not per token.** Sourced 2026-09-21 from the product's
pricing page: "Ranking $1.00 / 1,000 count … A query is defined as having up
to 100 documents … 132 documents to rank = 2 queries". A list price read off
the page rather than a third-party multiplier — rarer here than it should be —
and still not a bill.
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass

from .gemini import ProviderError

log = logging.getLogger(__name__)

#: See the module docstring for the source. Per query, where a query is up to
#: `RANK_DOCS_PER_QUERY` records; the arithmetic is `queries_for`.
RANK_USD_PER_1000_QUERIES = 1.00
RANK_DOCS_PER_QUERY = 100

#: The default model. `-005` takes 1,024 tokens per record, which is about five
#: times the longest chunk `hard_cap_chars` allows, so nothing is truncated.
DEFAULT_MODEL = "semantic-ranker-default-005"

#: Bounded, backing off, on the statuses that mean "not you". A 502 arrived
#: two hundred calls into the first real measurement; the embedder's recorded
#: lesson is that a per-minute bucket wants patience and a retry window of a
#: few seconds buys nothing. `Retry-After` wins when the service sends one.
ATTEMPTS = 5
MAX_BACKOFF = 30.0
RETRYABLE = frozenset({429, 500, 502, 503, 504})


def queries_for(records: int) -> int:
    """How many billable queries one request of this many records is."""
    return math.ceil(records / RANK_DOCS_PER_QUERY) if records > 0 else 0


def usd_for(records: int) -> float:
    return queries_for(records) * RANK_USD_PER_1000_QUERIES / 1000


def normalise(scores: list[float]) -> list[float]:
    """RAGFlow's contract: leave a calibrated batch alone, min-max one that is
    not, and clamp rather than zero a batch with no spread."""
    if not scores:
        return scores
    lo, hi = min(scores), max(scores)
    if 0.0 <= lo and hi <= 1.0:
        return scores
    if hi == lo:
        return [min(1.0, max(0.0, s)) for s in scores]
    return [(s - lo) / (hi - lo) for s in scores]


@dataclass(frozen=True)
class Ranked:
    """The scores, in the order the texts were given, plus what it cost."""

    scores: list[float]
    records: int
    seconds: float

    @property
    def queries(self) -> int:
        return queries_for(self.records)

    @property
    def usd(self) -> float:
        return usd_for(self.records)


class Ranker:
    """One ranking client per process, built on first use like `Provider.client`
    so that a worker can start on a machine with no ADC and say so later."""

    def __init__(self, project_id: str, model: str = DEFAULT_MODEL) -> None:
        if not project_id:
            raise ProviderError(
                "No Gemini project configured; the ranking API bills the same one.",
                kind="provider_unconfigured",
            )
        self.project_id = project_id
        self.model = model
        self.url = (
            "https://discoveryengine.googleapis.com/v1/projects/"
            f"{project_id}/locations/global/rankingConfigs/default_ranking_config:rank"
        )
        self._creds = None
        self._refresh = None
        self._client = None

    def _token(self) -> str:
        if self._creds is None:
            try:
                import google.auth
                import google.auth.transport.requests

                self._creds, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                self._refresh = google.auth.transport.requests.Request()
            except Exception as e:  # noqa: BLE001
                raise ProviderError(
                    "Could not resolve Application Default Credentials for the "
                    f"ranking API. ({e})",
                    kind="provider_no_credentials",
                ) from e
        if not self._creds.valid:
            self._creds.refresh(self._refresh)
        return self._creds.token

    def _post(self, body: dict):
        import httpx

        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client.post(
            self.url,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "x-goog-user-project": self.project_id,
            },
            json=body,
        )

    def rank(self, query: str, texts: list[str]) -> Ranked:
        """Score every text against the query. Order preserved; scores in [0, 1].

        Raises `ProviderError` after the attempts run out or on a status that
        will not change by asking again, so the caller can decide what an
        unranked list is worth — `retrieve.search` keeps the fused order.
        """
        if not texts:
            return Ranked(scores=[], records=0, seconds=0.0)
        records = [{"id": str(i), "content": t or ""} for i, t in enumerate(texts)]
        body = {"model": self.model, "query": query, "records": records, "topN": len(records)}
        t0 = time.perf_counter()
        last_status = 0
        last_text = ""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                r = self._post(body)
            except Exception as e:  # noqa: BLE001 — httpx transport errors
                if attempt == ATTEMPTS:
                    raise ProviderError(
                        f"ranking API unreachable: {e}", kind="provider_unavailable",
                        retryable=True,
                    ) from e
                time.sleep(_backoff(attempt, 0.0))
                continue
            if r.status_code == 200:
                scored = {int(x["id"]): float(x.get("score", 0.0)) for x in r.json().get("records", [])}
                scores = normalise([scored.get(i, 0.0) for i in range(len(texts))])
                return Ranked(scores=scores, records=len(texts), seconds=time.perf_counter() - t0)
            last_status, last_text = r.status_code, r.text[:300]
            if r.status_code in RETRYABLE and attempt < ATTEMPTS:
                retry_after = float(r.headers.get("retry-after") or 0) if r.headers.get("retry-after", "").isdigit() else 0.0
                delay = _backoff(attempt, retry_after)
                log.warning("ranking %s, retrying in %.1fs [%d/%d]", r.status_code, delay, attempt, ATTEMPTS)
                time.sleep(delay)
                continue
            break
        raise ProviderError(
            f"ranking API answered {last_status}: {last_text}",
            kind=_kind(last_status), retryable=last_status in RETRYABLE,
        )


def _backoff(attempt: int, retry_after: float) -> float:
    if retry_after > 0:
        return min(retry_after, MAX_BACKOFF)
    return min(2.0 ** (attempt - 1), MAX_BACKOFF) + random.uniform(0, 0.25)


def _kind(status: int) -> str:
    if status == 429:
        return "provider_quota"
    if status in (401, 403):
        return "provider_forbidden"
    if status == 404:
        return "provider_model_missing"
    if status >= 500:
        return "provider_unavailable"
    return "provider_refused"
