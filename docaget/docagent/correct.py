"""Conservative orthographic correction, paragraph by paragraph.

Successor to ``sociologia/fix/main.go``, with two deliberate changes.

**It is conservative.** PyMuPDF does not produce the intra-word spacing damage the
Go ``fixer`` existed to repair — measured on the reference book, the Go extractor
left 1,464 stray single letters ("y s o", "sea n uevo") and PyMuPDF leaves zero.
So this pass fixes accents, spelling, agreement and punctuation and explicitly
does **not** rewrite the author's prose. In a theology text, reformulating
sentences changes doctrinal shading, and an index that no longer reflects what the
author wrote is worse than one with a missing accent.

**It cannot lose structure.** The Go version sent a chapter as free text and
compared paragraph counts afterwards; when the model merged paragraphs the counts
disagreed and commit 54b8a61 settled for using the output anyway. Here the model
returns JSON keyed by paragraph index, so a merge is impossible rather than
detected late.

**Every correction is verified deterministically and can be rejected.** A
paragraph whose proper nouns, scripture references or figures did not survive
keeps its original text. The model proposes; it does not get the last word.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from .vertex import Vertex

# Paragraphs are batched to keep each call a reasonable size. Smaller than the Go
# version's 60 KB chapters: a failed batch costs less to redo, and JSON output has
# its own token overhead.
MAX_BATCH_CHARS = 24_000
CACHE_DIR = pathlib.Path("cache/correct")
PROMPT_VERSION = "v1-conservador"

# A correction may not change a paragraph's length by more than this. Fixing
# accents moves it by a fraction of a percent; a 25% swing means content was
# added or dropped.
MAX_LENGTH_DELTA = 0.25

SYSTEM = """Eres un corrector ortotipográfico de textos académicos en español (teología, filosofía, sociología).

Recibirás párrafos numerados extraídos de un libro. Corrige ÚNICAMENTE:
- Tildes faltantes o incorrectas (titulo → título, relacion → relación, mas → más cuando es adverbio).
- Errores de ortografía y de concordancia de género y número.
- Puntuación: comas, puntos, comillas y guiones mal puestos o ausentes.
- Espacios sobrantes o faltantes alrededor de signos.

PROHIBIDO ABSOLUTAMENTE:
- NO reescribas ni reformules. Conserva el orden de las palabras y la estructura de cada oración.
- NO partas ni unas oraciones. NO cambies la voz ni el tiempo verbal.
- NO resumas, NO expliques, NO añadas ni quites contenido.
- NO toques nombres propios (Dooyeweerd, Antonio Cruz, Lyotard, Kierkegaard, Masanobu Fukuoka, Milton Friedman, Maquiavelo, Descartes, Locke, Rousseau, Comte, Marx, Freud, Stott…).
- NO toques términos técnicos (metarrelato, metanarrativa, posmodernidad, hilemorfismo, soberanía de las esferas, antítesis…).
- NO toques referencias bíblicas ni sus cifras (Gén. 2:15, Pr. 22:15, Éxo. 20:4-6).
- NO toques ninguna cifra, año ni número de nota al pie.
- NO cambies mayúsculas de palabras que ya las tienen correctamente.

Si un párrafo ya está correcto, devuélvelo IDÉNTICO.

Devuelve un objeto JSON con la clave "parrafos": una lista de objetos {"i": <índice recibido>, "texto": "<texto corregido>"}. Un objeto por cada párrafo recibido, con su mismo índice."""

SCHEMA = {
    "type": "object",
    "properties": {
        "parrafos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "texto": {"type": "string"}},
                "required": ["i", "texto"],
            },
        }
    },
    "required": ["parrafos"],
}

# --- what must survive a correction -----------------------------------------

# Scripture references: "Gén. 2:15", "Pr 22:15", "Éxo. 20:4-6".
SCRIPTURE_RE = re.compile(r"\b\p{Lu}\p{L}{1,4}\.?\s*\d+[:.]\d+(?:-\d+)?".replace(r"\p{Lu}", r"[A-ZÁÉÍÓÚÑ]").replace(r"\p{L}", r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]"))
# Any run of two or more digits: years, verse numbers, footnote markers, figures.
NUMBER_RE = re.compile(r"\d{2,}")
# Capitalised tokens of four or more letters, which in running text are proper
# nouns. Sentence-initial words are excluded by the caller.
CAPITALISED_RE = re.compile(r"\b[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3,}\b")

# Capitalised words that are ordinary Spanish rather than names, so a legitimate
# correction may touch them.
_NOT_NAMES = frozenset(
    """
    esta este esto estos estas para pero porque cuando donde como aunque
    sobre entre desde hasta segun sino todos todas otros otras mismo misma
    ademas tambien entonces despues antes siempre nunca cada solo pues
    dicho hecho parte caso modo manera vida mundo tiempo forma
    """.split()
)


def _fold(s: str) -> str:
    """Lowercase and strip accents, so a corrected accent does not read as a lost
    proper noun."""
    n = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


@dataclass
class Rejection:
    index: int
    reason: str
    detail: str


@dataclass
class CorrectionReport:
    paragraphs: int = 0
    changed: int = 0
    unchanged: int = 0
    rejected: list[Rejection] = field(default_factory=list)
    cache_hits: int = 0
    calls: int = 0
    missing: int = 0  # paragraphs the model never returned

    def summary(self) -> str:
        return (
            f"{self.changed} corregidos · {self.unchanged} sin cambios · "
            f"{len(self.rejected)} rechazados · {self.missing} no devueltos · "
            f"{self.calls} llamadas, {self.cache_hits} de caché"
        )


def verify(original: str, corrected: str) -> tuple[bool, str, str]:
    """Deterministic gate on one paragraph's correction.

    Returns ``(ok, reason, detail)``. Anything that fails keeps its original text,
    so a bad correction degrades to no correction rather than to damaged content.
    """
    corrected = corrected.strip()
    if not corrected:
        return False, "empty", "the model returned nothing"

    delta = abs(len(corrected) - len(original)) / max(1, len(original))
    if delta > MAX_LENGTH_DELTA:
        return (
            False,
            "length",
            f"{len(original)} → {len(corrected)} chars ({delta:+.0%}), beyond ±{MAX_LENGTH_DELTA:.0%}",
        )

    for name, rx in (("scripture", SCRIPTURE_RE), ("numbers", NUMBER_RE)):
        lost = sorted(set(rx.findall(original)) - set(rx.findall(corrected)))
        if lost:
            return False, name, f"lost {lost[:5]}"

    # Proper nouns, ignoring the word that opens each sentence (which is
    # capitalised for grammatical reasons, not because it is a name).
    body = re.sub(r"(^|[.!?¿¡]\s+)([A-ZÁÉÍÓÚÑ])", lambda m: m.group(1) + m.group(2).lower(), original)
    names = {
        w for w in CAPITALISED_RE.findall(body) if _fold(w) not in _NOT_NAMES
    }
    folded_out = _fold(corrected)
    lost_names = sorted(n for n in names if _fold(n) not in folded_out)
    if lost_names:
        return False, "proper_noun", f"lost {lost_names[:5]}"

    return True, "", ""


def correct_paragraphs(
    vertex: Vertex,
    paragraphs: list[str],
    stage: str = "correct",
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[str], CorrectionReport]:
    """Correct a list of paragraphs, returning the same number in the same order.

    Any paragraph the model omits, or whose correction fails verification, comes
    back unchanged.

    ``progress`` is called once per batch. It matters: this pass is sequential and
    minutes long, and without output a stalled run is indistinguishable from a
    working one — which is exactly how a hung socket went unnoticed for 20 minutes.
    """
    report = CorrectionReport(paragraphs=len(paragraphs))
    out = list(paragraphs)
    cache = _load_cache()
    dirty = False
    batches = _batches(paragraphs)

    for n, batch in enumerate(batches, start=1):
        # Cache is per paragraph, not per batch: a re-run with different batching
        # still gets its hits.
        pending = [(i, paragraphs[i]) for i in batch if _key(paragraphs[i]) not in cache]
        for i in batch:
            if (hit := cache.get(_key(paragraphs[i]))) is not None:
                report.cache_hits += 1
                out[i] = hit

        if not pending:
            if progress:
                progress(n, len(batches), f"{len(batch)} párrafos, todos en caché")
            continue

        if progress:
            chars = sum(len(t) for _, t in pending)
            progress(n, len(batches), f"{len(pending)} párrafos, {chars:,} chars")

        payload = {"parrafos": [{"i": i, "texto": t} for i, t in pending]}
        try:
            raw = vertex.generate(
                json.dumps(payload, ensure_ascii=False),
                system=SYSTEM,
                stage=stage,
                json_schema=SCHEMA,
                temperature=0.0,
            )
            report.calls += 1
            returned = {
                int(item["i"]): item["texto"]
                for item in json.loads(raw).get("parrafos", [])
            }
        except Exception:
            # A failed batch leaves its paragraphs uncorrected. Correction is an
            # improvement, not a precondition, so this must not abort the run.
            report.calls += 1
            report.missing += len(pending)
            continue

        for i, original in pending:
            proposed = returned.get(i)
            if proposed is None:
                report.missing += 1
                continue
            ok, reason, detail = verify(original, proposed)
            if not ok:
                report.rejected.append(Rejection(index=i, reason=reason, detail=detail))
                continue
            cache[_key(original)] = proposed.strip()
            dirty = True
            out[i] = proposed.strip()

        # Persist after every batch, not once at the end. Correction is the most
        # expensive step in the pipeline and the slowest; saving only on completion
        # means a killed or crashed run throws away everything it already paid for
        # — observed, after a run was interrupted at batch 14 of 19.
        if dirty:
            _save_cache(cache)
            dirty = False

    for original, final in zip(paragraphs, out):
        if final.strip() == original.strip():
            report.unchanged += 1
        else:
            report.changed += 1

    if dirty:
        _save_cache(cache)
    return out, report


def corrected_path(source: str) -> pathlib.Path:
    """Where the corrected text is persisted.

    It has to exist on disk: after correction the byte offsets differ from the
    extracted text, so this file — not the PDF — is what ``char_span`` refers to,
    and what an audit of invariant #1 must open in binary.
    """
    p = pathlib.Path(source)
    return p.with_suffix(p.suffix + ".corrected.txt")


def write_corrected(source: str, text: bytes) -> pathlib.Path:
    path = corrected_path(source)
    path.write_bytes(text)
    return path


def _batches(paragraphs: list[str]) -> list[list[int]]:
    out: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for i, p in enumerate(paragraphs):
        if cur and size + len(p) > MAX_BATCH_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(p)
    if cur:
        out.append(cur)
    return out


def _key(text: str) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\x00{text}".encode("utf-8")).hexdigest()


def _cache_file() -> pathlib.Path:
    return CACHE_DIR / "paragraphs.json"


def _load_cache() -> dict[str, str]:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    _cache_file().write_text(
        json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8"
    )
