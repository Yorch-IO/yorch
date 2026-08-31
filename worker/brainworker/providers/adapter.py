"""Lets the engine run on Application Default Credentials.

`docagent.correct.correct_paragraphs` takes a `docagent.vertex.Vertex` — a REST
client authenticated with an `x-goog-api-key` header. The app has no API key by
design, so this adapter presents the one method that function actually calls and
routes it through the ADC provider instead.

Adapting rather than reimplementing is the whole point. `correct.verify()` is a
deterministic gate built from measured rules: a correction that loses a scripture
reference, a multi-digit number or a proper noun, or that changes length by more
than 25%, is discarded and the paragraph keeps its original text. So is the
per-paragraph cache that survives an interrupted run — added after one was killed
at batch 14 of 19 and threw away everything it had already paid for. None of that
is worth rewriting, and a rewrite would quietly drop the parts whose reasons are
not obvious from the code.
"""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import Callable, Sequence
from typing import Any

from .gemini import RETRIEVAL_DOCUMENT, RETRIEVAL_QUERY, Embedding, Provider, Usage

log = logging.getLogger(__name__)


class VertexAdapter:
    """Duck-types `docagent.vertex.Vertex` for the calls correction makes.

    Only `generate` is implemented. `embed`/`embed_many` are deliberately absent:
    embedding goes through `Provider.embed` directly, which checks batch length
    and vector width, and silently offering a second path to it here would let
    those checks be bypassed.
    """

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.usage = Usage()

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
        history: Sequence[tuple[str, str]] | None = None,
    ) -> str:
        if image_png is not None:
            # OCR is the only caller that passes an image, and it is a paid
            # stage the plan gates behind an explicit confirmation of its own.
            # Failing loudly here beats silently correcting a blank page.
            raise NotImplementedError(
                "image input is not routed through this adapter; OCR needs its "
                "own gate before it can spend"
            )

        result = self.provider.generate(
            prompt,
            system=system,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_schema=json_schema,
            # Forwarded, not just logged: the reasoning budget is per stage, and
            # `docagent.correct` is the caller that passes `stage="correct"`.
            stage=stage,
            # Passed only when there is one. Every stage but gleaning is a single
            # turn, and this class duck-types `docagent.vertex.Vertex` for the
            # engine's correction pass — which knows nothing about the argument.
            **({"history": history} if history else {}),
        )
        self.usage.add(result.usage)
        log.debug(
            "%s: %d in / %d out tokens",
            stage, result.usage.input_tokens, result.usage.output_tokens,
        )
        return result.text

    def generate_json(
        self,
        prompt: str,
        *,
        system: str,
        schema: dict,
        stage: str = "generate",
        history: Sequence[tuple[str, str]] | None = None,
    ) -> Any:
        """Structured output, parsed.

        A schema violation surfaces here, on the call that caused it, rather
        than as a parse failure in a later stage with no indication of which
        input produced it.
        """
        raw = self.generate(
            prompt, system=system, temperature=0.0, json_schema=schema, stage=stage,
            history=history,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"model returned invalid JSON despite a response schema: {e}"
            ) from e


class CachedEmbedder:
    """Implements `docagent.runner.Embedder` over the ADC provider, with a cache.

    Two things this adds to `Provider.embed`, both of which the engine already
    had and this side had lost track of.

    **A disk cache.** `Provider.embed` had none — `cache/` under the workspace has
    been empty since the day it was created, while `profiles/` beside it holds 23
    files. Paid activities get two Temporal attempts, so without a cache a second
    attempt re-pays for every vector of the first. The scarce resource is not the
    money: `online_prediction_requests_per_base_model` is metered
    `1/min/{project}/{base_model}` at ~6 embeddings a minute, so a 600-chunk book
    is ~100 minutes of wall clock and a run that dies at 586 and re-spends 586
    units of a per-minute budget arrives back at the same wall for ever. With the
    cache each attempt asks for strictly less than the one before.

    **`model` and `dimensions`, read by `runner.index_chunks`**, which refuses to
    write vectors from a model the collection does not already hold. Two 3,072-wide
    models are interchangeable to Qdrant and meaningless to each other's cosine.

    This is a separate class rather than a method on `VertexAdapter` on purpose.
    `VertexAdapter` duck-types `docagent.vertex.Vertex` for correction and rule
    learning, and its docstring's reason for having no `embed` still holds: a
    second path to embedding would let `Provider.embed`'s one-instance-per-request
    and vector-width checks be bypassed. This does not bypass them — every miss
    goes through `Provider.embed` — and it is not reachable from the `Vertex`
    shape, so neither can be skipped by calling the wrong object.
    """

    def __init__(
        self,
        provider: Provider,
        cache_dir: pathlib.Path,
        *,
        model: str,
        dimensions: int,
        workers: int = 6,
    ) -> None:
        self.provider = provider
        self.cache_dir = cache_dir
        self.workers = workers
        #: Passed in rather than read off the provider, so the one call site
        #: names the model for the embedder and for the writer in the same
        #: breath. The model is a property of the collection: `brain` holds
        #: 5,335 points of one model and `docagent_v2` 4,064 of another, both
        #: 3,072 wide, and nothing in Qdrant would notice them being mixed.
        self.model = model
        self.dimensions = dimensions
        self.usage = Usage()
        #: How many vectors this embedder did not have to pay for. Reported, not
        #: inferred: "cheap because cached" and "cheap because small" are
        #: different facts about a run.
        self.cache_hits = 0

    def embed_many(
        self,
        texts: Sequence[str],
        *,
        task_type: str = RETRIEVAL_DOCUMENT,
        on_done: Callable[[int, int], None] | None = None,
    ) -> list[Embedding]:
        from docagent import embedcache

        if task_type not in (RETRIEVAL_DOCUMENT, RETRIEVAL_QUERY):
            raise ValueError(f"unknown embedding task {task_type!r}")

        total = len(texts)
        out: list[Embedding | None] = [None] * total
        misses: list[int] = []
        for i, text in enumerate(texts):
            hit = embedcache.read(
                self.cache_dir, self.model, self.dimensions, task_type, text
            )
            if hit is None:
                misses.append(i)
                continue
            values, tokens = hit
            self.cache_hits += 1
            out[i] = Embedding(values=values, usage=Usage(input_tokens=tokens))

        if on_done and total:
            on_done(total - len(misses), total)

        if misses:
            fresh = self.provider.embed(
                [texts[i] for i in misses], task=task_type, workers=self.workers
            )
            for i, item in zip(misses, fresh):
                self.usage.add(item.usage)
                out[i] = item
                embedcache.write(
                    self.cache_dir,
                    self.model,
                    self.dimensions,
                    task_type,
                    texts[i],
                    item.values,
                    item.usage.input_tokens,
                )
            if on_done and total:
                on_done(total, total)

        # `Provider.embed` raises rather than returning a short list, so a None
        # here would be a defect in this method and not a provider failure.
        assert all(v is not None for v in out)
        return [v for v in out if v is not None]
