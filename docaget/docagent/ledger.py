"""Single accounting point for everything the agent spends.

Inherited invariant #7: cost is computed from the token counts the APIs
themselves return — ``statistics.token_count`` for embeddings,
``usageMetadata`` for generation — never from a character heuristic. The Go
pipeline's chars/4 estimate overshot the measured count by 11%.

Prices are USD per 1,000,000 tokens, keyed by exact model id, checked 2026-07-26
against third-party pricing aggregators — NOT Google's own pricing page, which
does not render for scraping. **The token counts here are measured; these
multipliers are second-hand.** Verify against the project's actual GCP billing
before trusting an absolute figure.

Keying by exact model id, rather than by family prefix, is what stopped the
Gemini Enterprise migration from producing confidently wrong numbers. The engine
now runs `gemini-3.6-flash` and `gemini-embedding-2`, for which no price had been
recorded here; a prefix match would have silently applied the 2.5 rates to them.
An unpriced model yields ``None`` — reported as "sin precio", never as zero,
because zero reads as free.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field

EMBED_INPUT_PER_M = 0.15  # gemini-embedding-001; embeddings have no output charge
FLASH_INPUT_PER_M = 0.15  # gemini-2.5-flash
FLASH_OUTPUT_PER_M = 1.25  # gemini-2.5-flash output is ~8x its input

#: {model_id: (input_per_million, output_per_million)}. Add an entry only with a
#: date and a source. Absent means unpriced, which is an answer.
PRICES_PER_MILLION: dict[str, tuple[float, float]] = {
    "gemini-embedding-001": (EMBED_INPUT_PER_M, 0.0),
    "gemini-2.5-flash": (FLASH_INPUT_PER_M, FLASH_OUTPUT_PER_M),
    # Added 2026-08-20 with the Gemini Enterprise migration. Same second-hand
    # provenance as the two above; see the module docstring.
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-embedding-2": (0.20, 0.0),
}


@dataclass
class Entry:
    """One category of spend: a stage of the pipeline."""

    stage: str
    model: str
    calls: int = 0
    retries: int = 0
    failures: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def cost_usd(self) -> float | None:
        """Dollars, or None when this model has no recorded price.

        None rather than 0.0: they are different claims, and a run whose models
        are unpriced must not read as a run that was free.
        """
        rates = PRICES_PER_MILLION.get(self.model)
        if rates is None:
            return None
        return self.input_tokens / 1e6 * rates[0] + self.output_tokens / 1e6 * rates[1]


@dataclass
class Ledger:
    """Thread-safe accumulator. The embedding pass runs concurrently, so every
    mutation takes the lock."""

    entries: dict[str, Entry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        stage: str,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        calls: int = 1,
        retries: int = 0,
        failures: int = 0,
        cache_hits: int = 0,
    ) -> None:
        with self._lock:
            e = self.entries.get(stage)
            if e is None:
                e = Entry(stage=stage, model=model)
                self.entries[stage] = e
            e.calls += calls
            e.retries += retries
            e.failures += failures
            e.cache_hits += cache_hits
            e.input_tokens += input_tokens
            e.output_tokens += output_tokens

    def total_usd(self) -> float:
        """The priced part of the spend. Read it with :meth:`unpriced_stages`.

        Returning only what is known beats refusing to answer, but a total that
        silently omitted an unpriced stage would be worse than either — hence
        the companion method, and hence `to_dict` carrying both.
        """
        return sum(c for e in self.entries.values() if (c := e.cost_usd()) is not None)

    def unpriced_stages(self) -> list[str]:
        return [e.stage for e in self.entries.values() if e.cost_usd() is None]

    def to_dict(self) -> dict:
        return {
            "total_usd": round(self.total_usd(), 6),
            "measured": True,
            "unpriced_stages": self.unpriced_stages(),
            "prices_per_million": {
                model: {"input": rates[0], "output": rates[1]}
                for model, rates in PRICES_PER_MILLION.items()
            },
            "price_source": (
                "third-party aggregators, 2026-07-26 — token counts are measured, "
                "multipliers are second-hand; verify against GCP billing. Models "
                "absent from prices_per_million are reported unpriced, not free."
            ),
            "stages": [
                {
                    **asdict(e),
                    "cost_usd": None if (c := e.cost_usd()) is None else round(c, 6),
                }
                for e in self.entries.values()
            ],
        }

    def summary(self) -> str:
        lines = [f"{'stage':<22}{'calls':>7}{'in':>10}{'out':>9}{'USD':>11}"]
        lines.append("-" * 59)
        for e in self.entries.values():
            cost = e.cost_usd()
            shown = f"{cost:>11.6f}" if cost is not None else f"{'sin precio':>11}"
            lines.append(
                f"{e.stage:<22}{e.calls:>7}{e.input_tokens:>10,}"
                f"{e.output_tokens:>9,}{shown}"
            )
        lines.append("-" * 59)
        lines.append(f"{'TOTAL':<22}{'':>7}{'':>10}{'':>9}{self.total_usd():>11.6f}")
        return "\n".join(lines)

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            f.write("\n")
