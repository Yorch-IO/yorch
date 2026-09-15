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
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

#: Bumped when the file's *shape* changes, so a reader can tell a history from
#: the single-run file this replaced.
HISTORY_VERSION = 2

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

    def dump(
        self,
        path: str,
        *,
        run_id: str | None = None,
        documents: list[str] | None = None,
    ) -> None:
        """Append this run to the history at `path`, keeping every earlier one.

        **This used to be `json.dump` over the whole file, so the ledger held
        exactly one run: the last.** Every run before it was gone, which is why
        `INFORME_INDEXACION.md` records that per-document accounting "does not
        exist and is not reconstructible" for the 28 documents indexed before
        this — the file was read on 2026-09-03 holding $1.335820, the total of
        one book, while the corpus behind it had cost several times that. An
        engine whose one job at the end of a run is to say what it spent must
        not spend the next run erasing the answer.

        `to_dict()` is unchanged and is exactly what the file used to contain,
        which is what makes the migration honest rather than lossy: an
        old-shaped file *is* a run record, so it is moved into `runs` rather
        than dropped. What it cannot supply is `run_id`, `at` and `documents`,
        and those stay `null` — "nobody recorded this" is a different claim
        from "this run touched no documents", and only one of them is true.

        Written through a temporary file and `os.replace`, which was not worth
        it while the file held one run and is worth it now: a crash mid-write
        would take the whole history with it, and there is nowhere to get it
        back from.
        """
        history = _read_history(path)
        history["runs"].append(
            {
                "run_id": run_id,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "documents": list(documents) if documents is not None else None,
                **self.to_dict(),
            }
        )
        history["total_usd"] = round(
            sum(r.get("total_usd") or 0.0 for r in history["runs"]), 6
        )
        history["runs_recorded"] = len(history["runs"])
        history["unpriced_stages"] = sorted(
            {s for r in history["runs"] for s in (r.get("unpriced_stages") or [])}
        )
        _write_atomically(path, history)


def _read_history(path: str) -> dict:
    """The history already at `path`, in its current shape, whatever it was.

    Three cases, and the third is the one worth stating: a file this cannot
    parse is **moved aside**, never overwritten. It is somebody's record of
    money already spent, and the run holding the lock on this process is not
    entitled to decide it was worthless.
    """
    try:
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
    except FileNotFoundError:
        return _empty_history()
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        kept = f"{path}.roto-{time.strftime('%Y%m%dT%H%M%S')}"
        try:
            os.replace(path, kept)
            print(f"  costo.json ilegible; se conserva en {kept}")
        except OSError:  # pragma: no cover - nothing left to do but not lose the run
            pass
        return _empty_history()

    if isinstance(existing, dict) and isinstance(existing.get("runs"), list):
        return existing
    if isinstance(existing, dict) and "stages" in existing:
        # The pre-history shape. It is one run and it becomes one run.
        return {**_empty_history(), "runs": [
            {"run_id": None, "at": None, "documents": None, **existing}
        ]}
    return _empty_history()


def _empty_history() -> dict:
    return {
        "version": HISTORY_VERSION,
        "total_usd": 0.0,
        "runs_recorded": 0,
        "measured": True,
        "unpriced_stages": [],
        "price_source": (
            "cada corrida guarda la tabla de precios con la que se calculó, "
            "porque los multiplicadores han cambiado y una cifra vieja debe "
            "seguir siendo comprobable con los precios de su propio día."
        ),
        "runs": [],
    }


def _write_atomically(path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".costo-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:  # pragma: no cover
            pass
        raise
