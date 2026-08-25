"""Lets the engine's correction pass run on Application Default Credentials.

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
from collections.abc import Sequence
from typing import Any

from .gemini import Provider, Usage

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
