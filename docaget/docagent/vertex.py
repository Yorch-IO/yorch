"""Gemini Enterprise over raw REST: embeddings, generation, and page OCR.

Port of ``sociologia/index/embed.go`` and ``index/context.go``.

**Authentication is Application Default Credentials, and there is no
alternative.** This module used to send an ``x-goog-api-key`` header, which
worked while the endpoint was regional Vertex AI. Gemini Enterprise (Agent
Platform) is served from ``aiplatform.googleapis.com`` and that service refuses
API keys outright — ``401 UNAUTHENTICATED / CREDENTIALS_MISSING, "API keys are
not supported by this API"``, verified 2026-08-11 with an unrestricted key on
the exact request its own docs print. The refusal comes from the service, not
from a key restriction, so there is nothing to configure around it. Do not
reintroduce a key path.

**The endpoint is global, not regional, and that is load-bearing.** Gemini 3.x
publishes only to the global endpoint: on ``us-central1`` every 3.x id answers
404 while the 2.5 family answers 200. Regionalising this silently pins the
engine to Gemini 2.5.

Inherited invariants:

4. The breadcrumb goes inside the request's ``content``, never a ``title`` field.
5. ``RETRIEVAL_DOCUMENT`` when indexing, ``RETRIEVAL_QUERY`` when querying — the
   model embeds the two asymmetrically on purpose.
6. ``gemini-embedding-001`` accepts exactly **one instance per request**, so
   throughput comes from concurrency, not batching.
7. Cost is taken from ``statistics.token_count`` / ``usageMetadata``.
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import httpx

from . import embedcache
from .ledger import Ledger

LOCATION = "global"
#: Listing the endpoint's models on 2026-08-19 returned 23 ids with
#: ``gemini-embedding-2`` as the only embedding one, and that was read as
#: ``gemini-embedding-001`` no longer being served. **Listing a model is not
#: having access to it.** Measured against the live endpoint on 2026-08-28 with
#: ADC:
#:
#:     yorch-platform-prod / gemini-embedding-2   -> HTTP 404
#:     yorch-platform-prod / gemini-embedding-001 -> HTTP 200
#:     verveux             / both                 -> HTTP 403
#:
#: A run died on that 404 after paying $1.27 for correction, indexing nothing.
#:
#: Availability is the smaller half of the reason. Every point in `docagent_v2`
#: was embedded with ``gemini-embedding-001``; both models are 3,072-wide, so
#: Qdrant accepts the other one's vectors silently and a cosine between two
#: models' embeddings means nothing. Changing this constant re-indexes a
#: collection into a second vector space with a healthy log and a corrupted
#: ranking — so it moves only together with a full re-embed of everything in it.
EMBED_MODEL = "gemini-embedding-001"
#: Kept in step with the app's `BRAIN_GEMINI_MODEL` default. Chosen over
#: 3.5-flash for a 17% cheaper output token, which is where this workload's bill
#: actually sits.
FLASH_MODEL = "gemini-3.6-flash"
#: Measured from a real call: the width is not exposed by ``models.get``.
#: Unchanged from ``gemini-embedding-001``, which is the only reason existing
#: Qdrant collections survive the model change.
EMBED_DIMS = 3072

TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"

MAX_ATTEMPTS = 3

#: A rate-limit 429 gets its own, longer patience. The quota that matters here
#: refills per minute, and `_retry`'s 3 attempts at 2s then 8s spend about ten
#: seconds against it — then `embed_many` raises, and one text failing takes the
#: whole run with it by design. Measured 2026-08-28 indexing a 600-chunk book:
#: the run stalled at `embedding 1/600` under 127 429s, while a single serial
#: request and eight parallel ones both answered 200 the moment the burst
#: stopped. Concurrency was not the wall; the refill window was.
#:
#: 2, 8, 32, 60, 60 — 162 seconds, comfortably past a per-minute window, and
#: capped because a sleep longer than the window buys nothing. A 429 says
#: "later"; a read timeout says "again", and that one keeps its measured three.
RATE_LIMIT_ATTEMPTS = 6
RATE_LIMIT_MAX_BACKOFF = 60.0

# Bounded per-phase timeouts. Measured on real correction batches: 22,946 chars of
# input took 55.8s and produced 5,243 output tokens, so 240s of read is roughly 4x
# headroom while still finite. The blanket 300s timeout it replaces applied to
# connect and pool acquisition too, where 300s is absurd.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 240.0
WRITE_TIMEOUT = 60.0
KEEPALIVE_EXPIRY = 30.0

#: No region prefix on the host: the global endpoint is `aiplatform.googleapis.com`
#: itself, with `locations/global` in the path.
_BASE = f"https://aiplatform.googleapis.com/v1/projects/{{project}}/locations/{LOCATION}/publishers/google/models"

_ADC_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

#: Resolved once per process. `google.auth.default()` is a blocking call that may
#: reach the metadata server, and a `Vertex` is constructed per run.
_adc_cache: tuple[Any, str | None] | None = None


def _adc() -> tuple[Any, str | None]:
    global _adc_cache
    if _adc_cache is None:
        import google.auth

        _adc_cache = google.auth.default(scopes=_ADC_SCOPES)
    return _adc_cache


#: The CWD-relative default, kept so the CLI and the test suite behave exactly
#: as before. `Vertex` resolves it at call time rather than capturing it, which
#: is what lets `tests/conftest.py` monkeypatch it away from the real cache.
#:
#: The implementation moved to `embedcache.py` so the Temporal worker's provider
#: adapter — which reaches Vertex AI through the google-genai SDK and had no
#: cache at all — can share it rather than grow a second one.
EMBED_CACHE_DIR = pathlib.Path("cache/embed")


class VertexError(RuntimeError):
    def __init__(self, status: int, body: str, retry_after: float = 0.0) -> None:
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        # 4xx other than 429 means a bad request or bad credentials: retrying
        # cannot help and would only burn time.
        return self.status == 429 or self.status >= 500


@dataclass
class EmbedResult:
    values: list[float]
    tokens: int
    truncated: bool


def load_env_var(key: str, env_file: str = ".env") -> str:
    """Environment first, then ``.env`` in the working directory.

    Port of ``fix/main.go``'s loadEnvVar. Every tool in this project reads
    ``.env`` from the working directory, not from the source directory.
    """
    if v := os.environ.get(key):
        return v
    try:
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line[len(key) + 1 :]
    except OSError as e:
        raise RuntimeError(f"could not open {env_file} and {key} is not set") from e
    raise RuntimeError(f"{key} not found in {env_file}")


class Vertex:
    #: A class attribute, not only an instance one, because a test may build a
    #: `Vertex` with `__new__` to exercise one method without credentials — and
    #: the cache lookup must still resolve rather than raise.
    _embed_cache_dir: "pathlib.Path | None" = None

    def __init__(
        self,
        api_key: str | None = None,
        project_id: str | None = None,
        ledger: Ledger | None = None,
        read_timeout: float = READ_TIMEOUT,
        embed_cache_dir: "pathlib.Path | None" = None,
    ) -> None:
        if api_key is not None:
            # Accepted and ignored so a stale call site fails loudly here rather
            # than with a 401 from the service three stages into a paid run.
            raise ValueError(
                "Gemini Enterprise does not accept API keys; this client uses "
                "Application Default Credentials. Remove the api_key argument "
                "and run `gcloud auth application-default login`."
            )
        # Resolved lazily: constructing a client must not require credentials,
        # so that a caller can build one, discover the project is unset, and say
        # so — rather than dying inside google.auth with a stack trace.
        self._project_id = project_id or os.environ.get("PROJECT_ID", "").strip()
        self.ledger = ledger or Ledger()
        self._read_timeout = read_timeout
        # None means "resolve the module constant at call time", which is what
        # keeps `tests/conftest.py`'s monkeypatch effective and lets a caller
        # that knows its workspace hand one over instead.
        self._embed_cache_dir = embed_cache_dir
        self._client = self._new_client()

    @property
    def embed_cache_dir(self) -> "pathlib.Path":
        return self._embed_cache_dir if self._embed_cache_dir is not None else EMBED_CACHE_DIR

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            # Split timeouts, not one blanket value. A single 300s blanket timeout
            # times three attempts means one silently-dropped connection stalls a
            # long run for 15 minutes — observed, with the process polling a dead
            # socket at 0% CPU while looking alive.
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT,
                read=self._read_timeout,
                write=WRITE_TIMEOUT,
                pool=CONNECT_TIMEOUT,
            ),
            # No auth header here: an OAuth token expires roughly hourly and a
            # long indexing run outlives one, so it is attached per request from
            # credentials that refresh themselves.
            headers={"Content-Type": "application/json"},
            limits=httpx.Limits(
                # One connection per worker in the embedding pool.
                max_connections=16,
                max_keepalive_connections=16,
                # Drop idle pooled connections rather than reuse one the server
                # has quietly closed. Correction batches are minutes apart, which
                # is exactly long enough for a keep-alive socket to go stale.
                keepalive_expiry=KEEPALIVE_EXPIRY,
            ),
        )

    def _reset_client(self) -> None:
        """Throw away the connection pool and start a fresh one.

        Called before retrying a transport failure. Measured: a long sequential run
        would complete 10-14 correction calls and then hang on an ESTABLISHED
        socket the server had quietly stopped answering. Retrying alone did not
        help — the retry came 2 seconds later, so the connection was not idle long
        enough for ``keepalive_expiry`` to drop it, and httpx handed back the same
        dead connection three times. Three read timeouts at 150s each is 450
        seconds of silence that looks exactly like normal work.

        Batch size was not the cause: a 22,946-character batch measured 55.8s,
        well inside the timeout.
        """
        try:
            self._client.close()
        except Exception:
            pass
        self._client = self._new_client()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Vertex":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- low level ----------------------------------------------------------

    @property
    def project_id(self) -> str:
        """The billing project, from PROJECT_ID or whatever ADC reports.

        ADC's project is a fallback, not the first choice: on a machine with
        several configured projects it is whichever one `gcloud` last set, and
        billing the wrong one is silent.
        """
        if not self._project_id:
            self._project_id = _adc()[1] or ""
        if not self._project_id:
            raise RuntimeError(
                "No Gemini project. Set PROJECT_ID, or run "
                "`gcloud auth application-default set-quota-project <id>`."
            )
        return self._project_id

    def _auth_header(self) -> dict[str, str]:
        credentials, _ = _adc()
        if not credentials.valid:
            import google.auth.transport.requests

            credentials.refresh(google.auth.transport.requests.Request())
        return {"Authorization": f"Bearer {credentials.token}"}

    def _post(self, model: str, verb: str, body: dict) -> dict:
        url = f"{_BASE.format(project=self.project_id)}/{model}:{verb}"
        resp = self._client.post(url, json=body, headers=self._auth_header())
        if resp.status_code != 200:
            retry_after = 0.0
            if ra := resp.headers.get("Retry-After"):
                try:
                    retry_after = float(ra)
                except ValueError:
                    pass
            raise VertexError(resp.status_code, resp.text, retry_after)
        return resp.json()

    def _retry(self, stage: str, model: str, verb: str, body: dict) -> dict:
        """Retry transient failures with backoff, honouring Retry-After.

        Same shape as ``correctChunkRetry`` in fix/main.go: 3 attempts, 2s then
        8s backoff.
        """
        last: Exception | None = None
        for attempt in range(1, max(MAX_ATTEMPTS, RATE_LIMIT_ATTEMPTS) + 1):
            try:
                return self._post(model, verb, body)
            except VertexError as e:
                last = e
                limit = RATE_LIMIT_ATTEMPTS if e.status == 429 else MAX_ATTEMPTS
                if not e.retryable or attempt == limit:
                    break
                backoff = max(2.0 ** (2 * attempt - 1), e.retry_after)
                if e.status == 429:
                    backoff = min(backoff, RATE_LIMIT_MAX_BACKOFF)
                self._note_retry(stage, model, attempt, f"HTTP {e.status}", backoff, limit)
                time.sleep(backoff)
            except httpx.HTTPError as e:
                # Transport failure: always retryable, but the pooled connection is
                # suspect and must not be reused — see _reset_client.
                last = e
                self._reset_client()
                if attempt == MAX_ATTEMPTS:
                    break  # transport failures keep their measured three
                backoff = 2.0 ** (2 * attempt - 1)
                self._note_retry(stage, model, attempt, type(e).__name__, backoff)
                time.sleep(backoff)
        self.ledger.record(stage, model, calls=0, failures=1)
        assert last is not None
        raise last

    def _note_retry(
        self, stage: str, model: str, attempt: int, why: str, backoff: float,
        limit: int = MAX_ATTEMPTS,
    ) -> None:
        """Retries must be visible. A silent retry loop is indistinguishable from
        progress, which is how 450 seconds of a dead socket went unnoticed.

        The limit is passed rather than read from `MAX_ATTEMPTS`, because a 429
        now gets `RATE_LIMIT_ATTEMPTS` and printing "3/3" while the loop goes on
        to a fourth attempt makes the log say the run is about to fail when it
        is not.
        """
        self.ledger.record(stage, model, calls=0, retries=1)
        print(
            f"    retry {attempt}/{limit} on {stage}: {why}, "
            f"waiting {backoff:.0f}s",
            file=sys.stderr,
            flush=True,
        )

    # --- embeddings ---------------------------------------------------------

    def embed(
        self, text: str, task_type: str = TASK_DOCUMENT, stage: str = "embed"
    ) -> EmbedResult:
        """Embed one text. One instance per request is the model's hard limit.

        Cached on disk, for the same reason the correction pass is: an
        interrupted run must keep what it paid for. Here the scarce resource is
        not the money but the quota — `online_prediction_requests_per_base_model`
        is metered `1/min/{project}/{base_model}` — and a run that dies at 586 of
        600 and then re-spends 586 units to reach the same wall never converges.
        """
        cache_dir = self.embed_cache_dir
        if (hit := embedcache.read(cache_dir, EMBED_MODEL, EMBED_DIMS, task_type, text)):
            values, tokens = hit
            self.ledger.record(stage, EMBED_MODEL, calls=0, cache_hits=1)
            return EmbedResult(values, tokens, False)
        body = {
            "instances": [{"task_type": task_type, "content": text}],
            "parameters": {"outputDimensionality": EMBED_DIMS, "autoTruncate": False},
        }
        data = self._retry(stage, EMBED_MODEL, "predict", body)

        preds = data.get("predictions") or []
        if not preds:
            raise VertexError(200, f"empty predictions: {data}")
        emb = preds[0]["embeddings"]
        values = emb["values"]
        if len(values) != EMBED_DIMS:
            raise VertexError(200, f"expected {EMBED_DIMS} dims, got {len(values)}")
        stats = emb.get("statistics", {})
        tokens = int(stats.get("token_count", 0))

        self.ledger.record(stage, EMBED_MODEL, input_tokens=tokens)
        result = EmbedResult(values, tokens, bool(stats.get("truncated")))
        if not result.truncated:
            # A truncated embedding is a warning about the input, not a result
            # worth reusing.
            embedcache.write(
                cache_dir, EMBED_MODEL, EMBED_DIMS, task_type, text, values, tokens
            )
        return result

    def embed_many(
        self,
        texts: Iterable[str],
        task_type: str = TASK_DOCUMENT,
        workers: int = 6,
        on_done: Callable[[int, int], None] | None = None,
        stage: str = "embed",
    ) -> list[EmbedResult]:
        """Embed many texts with a bounded worker pool, preserving order.

        A single text failing after all retries raises: a partially indexed
        collection silently returns wrong results, which is worse than no
        collection. (This is the opposite of the correction pass, where falling
        back to uncorrected text still leaves something readable.)
        """
        items = list(texts)
        results: list[EmbedResult | None] = [None] * len(items)
        done = 0

        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self.embed, t, task_type, stage): i
                for i, t in enumerate(items)
            }
            for fut in cf.as_completed(futures):
                i = futures[fut]
                results[i] = fut.result()  # propagates
                done += 1
                if on_done:
                    on_done(done, len(items))

        return [r for r in results if r is not None]

    # --- generation ---------------------------------------------------------

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        stage: str = "generate",
        temperature: float = 0.2,
        json_schema: dict | None = None,
        image_png: bytes | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        """One generateContent call. Returns the first candidate's text.

        ``json_schema`` switches on structured output, which is how the caller
        avoids the alignment failure the Go ``fixer`` hit when it relied on the
        model preserving a paragraph count.
        """
        parts: list[dict[str, Any]] = []
        if image_png is not None:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(image_png).decode("ascii"),
                    }
                }
            )
        parts.append({"text": prompt})

        gen_config: dict[str, Any] = {"temperature": temperature}
        if max_output_tokens:
            gen_config["maxOutputTokens"] = max_output_tokens
        if json_schema is not None:
            gen_config["responseMimeType"] = "application/json"
            gen_config["responseSchema"] = json_schema

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_config,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        data = self._retry(stage, FLASH_MODEL, "generateContent", body)

        usage = data.get("usageMetadata", {})
        self.ledger.record(
            stage,
            FLASH_MODEL,
            input_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)),
        )

        candidates = data.get("candidates") or []
        if not candidates:
            raise VertexError(200, f"no candidates: {data}")
        content_parts = candidates[0].get("content", {}).get("parts") or []
        if not content_parts:
            reason = candidates[0].get("finishReason", "unknown")
            raise VertexError(200, f"empty candidate (finishReason={reason})")
        return content_parts[0].get("text", "")
