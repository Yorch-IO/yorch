"""Vertex AI access for the whole pipeline: correction, extraction, planning, embedding.

**No API keys.** Authentication is Application Default Credentials, which is the
one decision this module exists to enforce. A key is a bearer secret that has to
be stored, mounted, rotated and kept out of logs, out of the catalog and out of
Temporal history; ADC is resolved by the Google auth library from a mounted
credential file locally and from the runtime service account in a managed
deployment, and there is nothing left for this codebase to leak. Workflows pass
a model name and a project id, both of which are safe in a payload that persists
to Postgres for the namespace's whole retention period.

Everything that talks to Vertex goes through one client so that four things have
exactly one implementation: ADC resolution and the error message when it fails,
retry with backoff, the distinction between a quota problem and a permission
problem, and token accounting. The engine's own `docagent.vertex` module still
speaks raw REST with an `x-goog-api-key` header; migrating it to call this is
the next step, and until then the two must not both be used in one run.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from google import genai
from google.genai import errors, types

from ..config import Gemini

log = logging.getLogger(__name__)

#: Three attempts, matching the engine's `MAX_ATTEMPTS`. Beyond that a retry is
#: no longer riding out a blip; it is queueing behind a sustained outage while
#: the user watches a spinner.
MAX_ATTEMPTS = 3

#: Base for exponential backoff, in seconds. Jitter is added because every
#: activity in a batch fails at the same instant when a quota is exhausted, and
#: un-jittered backoff makes them all retry at the same instant too.
BACKOFF_BASE = 1.5

#: A rate-limit 429 gets its own, longer patience, and this is a correction
#: rather than a preference. The comment above says the three attempts match the
#: engine — they did, and then the engine measured the quota and grew a second
#: policy that was never brought across.
#:
#: The metric is `aiplatform.googleapis.com/online_prediction_requests_per_base_model`,
#: whose unit `serviceusage` reports as `1/min/{project}/{base_model}` — a
#: *per-minute* bucket, measured at ~6 embeddings a minute sustained. Three
#: attempts at 1.5^n is about seven seconds of patience against a sixty-second
#: window, so every attempt lands inside the same exhausted bucket and the
#: activity fails having learnt nothing. A run stalled at `embedding 1/600` under
#: 127 of these.
#:
#: The delays are 2, 8, 32, 60, 60: capped, because a sleep longer than the
#: window buys nothing, and `Retry-After` wins over all of it when the service
#: says how long it wants.
RATE_LIMIT_ATTEMPTS = 6
RATE_LIMIT_MAX_BACKOFF = 60.0

RETRIEVAL_DOCUMENT = "RETRIEVAL_DOCUMENT"
RETRIEVAL_QUERY = "RETRIEVAL_QUERY"

#: Concurrent embedding requests. Matches the engine's default, which was chosen
#: against the same per-request quota this shares.
EMBED_WORKERS = 6


class ProviderError(RuntimeError):
    """A Vertex failure, classified so the UI can offer a fix rather than a message.

    `kind` is the machine-readable half. The app's GUIDANCE map keys on exactly
    these strings, so adding one here means adding advice there.
    """

    def __init__(self, message: str, *, kind: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class Usage:
    """Measured token counts. Deliberately not dollars.

    Prices are a third-party multiplier that goes stale; token counts are what
    the API actually reported. The catalog stores both in separate columns for
    the same reason, and any surface showing a dollar figure has to say so.
    """

    input_tokens: int = 0
    #: Everything billed at the output rate, **including thinking tokens**.
    output_tokens: int = 0
    #: The reasoning half of `output_tokens`, broken out so a bill can be
    #: explained. Not additional to it — a subset of it.
    thinking_tokens: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.thinking_tokens += other.thinking_tokens
        self.calls += other.calls


@dataclass
class Generation:
    text: str
    usage: Usage


@dataclass
class Embedding:
    values: list[float]
    usage: Usage = field(default_factory=Usage)

    @property
    def tokens(self) -> int:
        """What this one embedding was billed for.

        Named `tokens` because that is what `docagent.runner.Vector` reads, so an
        `Embedding` satisfies the engine's protocol without a wrapper. It is a
        view on `usage`, not a second number.
        """
        return self.usage.input_tokens


def _attempts_for(err: ProviderError) -> int:
    """How many tries this kind of failure deserves.

    A quota is a bucket that refills on a clock; an outage is not. Waiting out
    the first is the fix, and giving up on it after seven seconds is how a paid
    stage fails without having tested the thing it was waiting for.
    """
    return RATE_LIMIT_ATTEMPTS if err.kind == "provider_quota" else MAX_ATTEMPTS


def _backoff(err: ProviderError, attempt: int, retry_after: float) -> float:
    """2, 8, 32, 60, 60 for a quota; 1.5^n for everything else.

    `Retry-After` wins when the service sent one: it knows when its own bucket
    refills and we are guessing.
    """
    if err.kind != "provider_quota":
        return BACKOFF_BASE ** attempt + random.uniform(0, 0.5)
    if retry_after > 0:
        return min(retry_after, RATE_LIMIT_MAX_BACKOFF)
    return min(2.0 ** (2 * attempt - 1), RATE_LIMIT_MAX_BACKOFF) + random.uniform(0, 0.5)


def _retry_after(e: errors.APIError) -> float:
    """Seconds the service asked us to wait, or 0 if it did not say.

    The SDK does not surface response headers uniformly across transports, so
    this reads whatever is there and treats anything unparseable as absent —
    a missing hint costs a guessed delay, not a failure.
    """
    for holder in (getattr(e, "response", None), e):
        headers = getattr(holder, "headers", None) or {}
        try:
            for name, value in headers.items():
                if str(name).lower() == "retry-after":
                    return float(str(value).strip())
        except (AttributeError, TypeError, ValueError):
            continue
    return 0.0


def _classify(e: errors.APIError) -> ProviderError:
    """Turn a Vertex error into something the UI can act on.

    The three cases below are separated because their fixes are unrelated: a
    quota problem is waited out, a permission problem needs a role granted, and
    a bad-argument problem is a defect in this repository. Collapsing them into
    "the API failed" sends the user to the wrong one.
    """
    status = getattr(e, "code", None) or 0
    message = str(e)

    if status == 429 or "RESOURCE_EXHAUSTED" in message:
        return ProviderError(
            f"Vertex AI quota exhausted: {message}", kind="provider_quota", retryable=True
        )
    if status in (401, 403) or "PERMISSION_DENIED" in message:
        return ProviderError(
            "Vertex AI refused the credentials. The account needs "
            f"roles/aiplatform.user on the billing project. ({message})",
            kind="provider_forbidden",
        )
    if status == 404:
        return ProviderError(
            f"Vertex AI has no such model in this location: {message}",
            kind="provider_model_missing",
        )
    if status >= 500:
        return ProviderError(
            f"Vertex AI is unavailable: {message}", kind="provider_unavailable",
            retryable=True,
        )
    return ProviderError(f"Vertex AI rejected the request: {message}", kind="provider_refused")


class Provider:
    """One Vertex client, shared by every activity in a worker process."""

    def __init__(self, settings: Gemini) -> None:
        if not settings.configured:
            raise ProviderError(
                "No Gemini project configured. Set BRAIN_GEMINI_PROJECT_ID to the "
                "project that will be billed.",
                kind="provider_unconfigured",
            )
        self.settings = settings
        self._client: genai.Client | None = None

    @property
    def client(self) -> genai.Client:
        """Built on first use so that constructing a Provider cannot fail on ADC.

        The distinction matters: a worker must start and report itself healthy
        on a machine where the user has not yet run `gcloud auth
        application-default login`, and tell them so — not crash at import.
        """
        if self._client is None:
            try:
                self._client = genai.Client(
                    vertexai=True,
                    project=self.settings.project_id,
                    location=self.settings.location,
                )
            except Exception as e:
                raise ProviderError(
                    "Could not resolve Application Default Credentials. Run "
                    "`gcloud auth application-default login`, or give the "
                    f"runtime a service account with roles/aiplatform.user. ({e})",
                    kind="provider_no_credentials",
                ) from e
        return self._client

    # -- retry -------------------------------------------------------------

    def _call(self, what: str, fn):
        last: ProviderError | None = None
        # The ceiling is the larger of the two policies; which one applies is
        # decided per failure, because a run can hit a 503 and then a 429.
        for attempt in range(1, max(MAX_ATTEMPTS, RATE_LIMIT_ATTEMPTS) + 1):
            try:
                return fn()
            except errors.APIError as e:
                last = _classify(e)
                budget = _attempts_for(last)
                if not last.retryable or attempt >= budget:
                    raise last from e
                delay = _backoff(last, attempt, _retry_after(e))
                log.warning(
                    "%s failed (%s), retrying in %.1fs [%d/%d]",
                    what, last.kind, delay, attempt, budget,
                )
                time.sleep(delay)
            except ProviderError:
                raise
            except Exception as e:
                # Transport-level failures never reach `APIError`. They are
                # retryable for the same reason a 503 is.
                last = ProviderError(
                    f"{what} failed: {type(e).__name__}: {e}",
                    kind="provider_unavailable",
                    retryable=True,
                )
                if attempt >= MAX_ATTEMPTS:
                    raise last from e
                time.sleep(BACKOFF_BASE ** attempt + random.uniform(0, 0.5))
        raise last or ProviderError(f"{what} failed", kind="provider_refused")

    # -- generation --------------------------------------------------------

    def _prepare(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        response_schema: Any | None = None,
        model: str | None = None,
        stage: str | None = None,
        history: Sequence[tuple[str, str]] | None = None,
        thinking_budget: int | None = None,
    ) -> tuple[Any, types.GenerateContentConfig, str]:
        """Everything a call needs, built once and shared by both callers.

        Split out of `generate` when streaming arrived: the config, the history
        and the thinking budget are identical whether the response comes back
        whole or in pieces, and two copies of this would be two places for a
        stage's reasoning budget to be resolved differently.

        `history` is a list of prior `(sent, received)` exchanges, prepended as
        alternating user/model turns. Only semantic extraction's gleaning pass
        uses it: asking the same model to add what it missed *in the same
        conversation* is what makes the second pass cheaper than a second
        independent extraction, since it does not have to be told not to repeat
        itself.

        `response_schema` switches the call to JSON mode. Semantic extraction
        uses it rather than asking for JSON in the prompt and parsing whatever
        comes back: a schema violation then surfaces as an API error on the call
        that caused it, instead of as a parse failure three stages later with no
        indication of which chunk produced it.

        **`max_output_tokens` is measured against thinking too.** A budget that
        looks generous for the answer can be consumed entirely by reasoning,
        returning `finish_reason=MAX_TOKENS` and *no text at all* — observed at
        16 tokens on `gemini-3.6-flash`. Leave it unset unless there is a reason,
        and never set it near the expected answer length.
        """
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            system_instruction=system,
        )
        # Per stage, not per product: the planner classifies, correction is
        # mechanical and separately verified, and answering is the one genuine
        # judgement call. `thinking_for` resolves the engine's own stage names
        # too, so `stage="correct"` does not quietly miss.
        #
        # An explicit argument wins, full stop. This function stays dumb about
        # *why* one was passed: deciding whether a per-question effort level may
        # override the configured stage budget is policy, and it lives at the one
        # call site that has one (`answering/answer.py`), not here.
        budget = (
            thinking_budget
            if thinking_budget is not None
            else self.settings.thinking_for(stage)
        )
        if budget is not None:
            config.thinking_config = types.ThinkingConfig(thinking_budget=budget)
        if response_schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = response_schema

        contents: Any = prompt
        if history:
            contents = []
            for sent, received in history:
                contents.append(
                    types.Content(role="user", parts=[types.Part(text=sent)])
                )
                contents.append(
                    types.Content(role="model", parts=[types.Part(text=received)])
                )
            contents.append(
                types.Content(role="user", parts=[types.Part(text=prompt)])
            )

        return contents, config, model or self.settings.model

    def generate(self, prompt: str, **kw: Any) -> Generation:
        """One completion, whole. See `_prepare` for every argument."""
        contents, config, model_id = self._prepare(prompt, **kw)

        def run():
            return self.client.models.generate_content(
                model=model_id, contents=contents, config=config
            )

        response = self._call("generate", run)
        return Generation(text=response.text or "", usage=_usage_of(response))

    def generate_stream(
        self, prompt: str, *, on_delta: Callable[[str], None], **kw: Any
    ) -> Generation:
        """One completion, handed to `on_delta` as it is written.

        Same arguments as `generate`, plus the callback. The return value is the
        same `Generation` the whole-response path returns, so a caller that also
        wants the finished text — every caller does, because the streamed text is
        a draft and the parsed envelope is what is authoritative — does not have
        to reassemble it.

        **Retry stops at the first chunk.** `_call` retries a failed request up
        to six times, which is right for a call whose result nobody has seen. Once
        a delta has been handed to `on_delta` it is on somebody's screen, and a
        retry would replay it from the beginning: the reader would watch the
        answer restart. So the request *and its first chunk* are opened inside
        `_call` — which is where a quota refusal or a 503 actually lands — and
        everything after that is outside it.

        **Usage arrives at the end, not at the beginning.** Each chunk may carry
        `usage_metadata` and the last one carries the totals; reading the first
        would report a few tokens for the most expensive call in a turn, and
        `Spend` would under-report the bill with nothing saying so. The last
        chunk that carries any is the one that counts.
        """
        contents, config, model_id = self._prepare(prompt, **kw)

        def start():
            stream = self.client.models.generate_content_stream(
                model=model_id, contents=contents, config=config
            )
            # `next` is what actually issues the request for most transports, so
            # pulling the first chunk here is what puts it under the retry.
            it = iter(stream)
            return it, next(it, None)

        stream_iter, first = self._call("generate", start)

        parts: list[str] = []
        usage = Usage(calls=1)

        def take(chunk: Any) -> None:
            nonlocal usage
            piece = _chunk_text(chunk)
            if piece:
                parts.append(piece)
                on_delta(piece)
            if getattr(chunk, "usage_metadata", None) is not None:
                usage = _usage_of(chunk)

        try:
            if first is not None:
                take(first)
            for chunk in stream_iter:
                take(chunk)
        except errors.APIError as e:
            # Deliberately not retried — see the docstring. Classified anyway, so
            # the UI still gets a kind it can act on.
            raise _classify(e) from e
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(
                f"generate failed mid-stream: {type(e).__name__}: {e}",
                kind="provider_unavailable",
            ) from e

        return Generation(text="".join(parts), usage=usage)

    # -- embeddings --------------------------------------------------------

    def embed(
        self,
        texts: Sequence[str],
        *,
        task: str = RETRIEVAL_DOCUMENT,
        workers: int = EMBED_WORKERS,
    ) -> list[Embedding]:
        """Embed many texts — one API request each, run concurrently.

        **One instance per request is not a style choice.** Measured against
        `gemini-embedding-2` on 2026-08-19: a request carrying four texts returns
        *one* embedding and no error. That is the engine's inherited invariant #6
        ("batching silently fails"), established for `gemini-embedding-001` and
        still true for its replacement. Batching here would have paired chunk 0's
        vector with chunk 0 and left every other chunk unembedded, or — with a
        looser length check — silently shifted every vector by one.

        Throughput therefore comes from concurrency, not from batching.

        `task` is asymmetric on purpose and inherited from invariant #5:
        `RETRIEVAL_DOCUMENT` when indexing, `RETRIEVAL_QUERY` when answering. The
        model embeds the two differently and using one for both measurably
        degrades retrieval.
        """
        if task not in (RETRIEVAL_DOCUMENT, RETRIEVAL_QUERY):
            raise ValueError(f"unknown embedding task {task!r}")
        if not texts:
            return []
        if len(texts) == 1:
            return [self._embed_one(texts[0], task)]

        with cf.ThreadPoolExecutor(max_workers=min(workers, len(texts))) as pool:
            return list(pool.map(lambda t: self._embed_one(t, task), texts))

    def _embed_one(self, text: str, task: str) -> Embedding:
        def run():
            return self.client.models.embed_content(
                model=self.settings.embedding_model,
                contents=text,
                config=types.EmbedContentConfig(
                    task_type=task,
                    output_dimensionality=self.settings.embedding_dimensions,
                ),
            )

        response = self._call("embed", run)
        embeddings = list(response.embeddings or [])
        if len(embeddings) != 1:
            raise ProviderError(
                f"asked for one embedding and got {len(embeddings)}",
                kind="provider_refused",
            )

        values = list(embeddings[0].values or [])
        if len(values) != self.settings.embedding_dimensions:
            raise ProviderError(
                f"embedding has {len(values)} dimensions, expected "
                f"{self.settings.embedding_dimensions} — a Qdrant collection's "
                "vector size is fixed at creation, so this cannot be written",
                kind="provider_refused",
            )
        return Embedding(values=values, usage=_embedding_usage(response))

    # -- health ------------------------------------------------------------

    def probe(self) -> str:
        """Cheapest call that proves credentials, project and model all work.

        One short embedding rather than a generation: it is the least expensive
        request Vertex bills for, and a health check that costs real money is
        one people disable.
        """
        self.embed(["ping"], task=RETRIEVAL_QUERY)
        return (
            f"{self.settings.model} / {self.settings.embedding_model} "
            f"@ {self.settings.location}"
        )


def _chunk_text(chunk: Any) -> str:
    """The visible text of one streamed chunk, or nothing.

    `.text` is a convenience property that concatenates the candidate's parts,
    and it is `None` — and on some SDK versions raises a warning — for a chunk
    that carries only a `finish_reason`, only `usage_metadata`, or only a
    thinking part. Every stream ends with at least one such chunk, so reading it
    naively turns the normal end of every answer into an exception.
    """
    try:
        return chunk.text or ""
    except Exception:
        return ""


def _usage_of(response: Any) -> Usage:
    """Read whatever the response carries, tolerating its absence.

    **Thinking tokens count as output, and they dominate short calls.** Gemini
    3.x reasons before answering and reports that separately as
    `thoughts_token_count`, which `candidates_token_count` excludes — but which
    is billed at the output rate. Measured 2026-08-20 on a trivial prompt:
    `gemini-3.6-flash` produced 2 visible output tokens and **125 thinking
    tokens**. Reading only the visible half under-reported that call by 60x.

    Reporting zero when `usage_metadata` is absent is wrong but recoverable;
    raising would fail a run that actually succeeded.
    """
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return Usage(calls=1)

    prompt = getattr(meta, "prompt_token_count", 0) or 0
    visible = getattr(meta, "candidates_token_count", 0) or 0
    thoughts = getattr(meta, "thoughts_token_count", 0) or 0
    if not visible and not thoughts:
        # Fall back to the total, which includes both, rather than to zero.
        total = getattr(meta, "total_token_count", 0) or 0
        visible = max(0, total - prompt)

    return Usage(
        input_tokens=prompt,
        output_tokens=visible + thoughts,
        thinking_tokens=thoughts,
        calls=1,
    )


def _embedding_usage(response: Any) -> Usage:
    """Token counts for an embedding batch.

    **Not** on `usage_metadata`, which is `None` for embeddings — measured
    against `gemini-embedding-2` on 2026-08-19. They live per embedding, on
    `statistics.token_count`, which is the engine's inherited invariant #7
    ("cost is taken from statistics.token_count / usageMetadata") and the reason
    that invariant names two places instead of one. Reading only the response
    level silently reports every embedding as free.
    """
    total = 0
    for item in getattr(response, "embeddings", None) or []:
        stats = getattr(item, "statistics", None)
        if stats is None:
            continue
        count = getattr(stats, "token_count", None)
        if count is None and isinstance(stats, dict):
            count = stats.get("token_count")
        total += int(count or 0)
    return Usage(input_tokens=total, output_tokens=0, calls=1)
