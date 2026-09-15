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
#: The CWD-relative default. `correct_paragraphs` takes an optional
#: ``cache_dir`` and falls back to this, for the reason `profiles.PROFILE_DIR`
#: gives: `os.chdir` is process-global and the Temporal worker runs activities
#: concurrently.
CACHE_DIR = pathlib.Path("cache/correct")
PROMPT_VERSION = "v1-conservador"

# A correction may not change a paragraph's length by more than this. Fixing
# accents moves it by a fraction of a percent; a 25% swing means content was
# added or dropped.
MAX_LENGTH_DELTA = 0.25
#: Nor may it delete more than this many characters, whatever the ratio says.
#:
#: A ratio cannot express "a sentence was deleted": on a 12-character paragraph
#: ±25% is three characters, and on a 1,600-character one it is four hundred —
#: a whole paragraph of a book can go missing and the ratio still passes.
#: Measured 2026-09-03 over the 2,684 corrections this repository's own cache
#: holds for `libros/`, recovered by re-extracting each PDF and looking its
#: paragraphs up by key: **2,145 gained or kept length, 525 lost 1-5
#: characters, 5 lost 6-11, and 9 lost 12 or more.** Every one of those 9 was
#: read by hand, and five had deleted a whole sentence the author wrote —
#: "Generosidad en vez de avaricia." and "Se negó a recibir culto." (two
#: section titles that extraction had merged into their paragraph, in
#: 07-LlavesDelPoder-INT.pdf), "Según los gnósticos, su doctrina era un
#: conocimiento especial," (LOS APOLOGISTAS.pdf), "no se menciona, en este
#: caso, la reproducción." (06-SexoEnLaBiblia_INT-S.pdf) and one more in
#: 04-TesorosDiosMeDio_int.pdf. None of them lost a proper noun, a two-digit
#: number or a scripture reference, and the largest was -22.2% — so every
#: existing check passed it.
#:
#: 20 rather than 12 because the three remaining cases are partial repairs of
#: interleaved two-column extraction, where the model rearranges as much as it
#: removes; rejecting those keeps the garbled original, which is the honest
#: degradation this gate exists to prefer, and the run reports it either way.
#: **What this cannot see is a deletion offset by an addition**: the measure is
#: net, like `MAX_LENGTH_DELTA`, so a sentence dropped and another lengthened
#: still passes.
MAX_LOST_CHARS = 20

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
    """One correction the gate refused, and **what it refused**.

    `proposed` exists because without it a rejection is unreviewable, and that
    turned out to matter. A refused proposal is never cached — `correct_paragraphs`
    `continue`s before `cache.put` — so the model's text was simply gone, and all
    a reader had was the reason plus the token that went missing.

    Two consequences, both real. The wide audit that set `MAX_LOST_CHARS` measured
    2,684 corrections out of the cache, and **every one of them was a correction
    this gate had accepted** — so `verify`'s false-*positive* rate has never been
    measured at all, only its false-negative one. And on the first auto-caption
    transcript indexed, 22 of 107 paragraphs were refused for `proper_noun` loss
    where the "names" were what the captioner misheard — `Tilich` for Tillich,
    `Mars` for Marx, `Cuyama` for Fukuyama, `FARG` for FARC — and nobody could
    see whether the model had proposed the right repair, because the proposal was
    discarded along with it.

    It is model output the gate judged unsafe, so it goes in the *report* and
    never into the corpus. The corrected stream is unaffected.
    """

    index: int
    reason: str
    detail: str
    #: What the model returned and `verify` refused. Empty when the paragraph
    #: came back absent rather than wrong.
    proposed: str = ""


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

    lost = len(original) - len(corrected)
    if lost > MAX_LOST_CHARS:
        return (
            False,
            "deleted",
            f"{len(original)} → {len(corrected)} chars: {lost} deleted, beyond "
            f"{MAX_LOST_CHARS} — a sentence, not an accent",
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
    cache_dir: "pathlib.Path | None" = None,
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
    cache = _ParagraphCache(cache_dir)
    batches = _batches(paragraphs)

    for n, batch in enumerate(batches, start=1):
        # Cache is per paragraph, not per batch: a re-run with different batching
        # still gets its hits.
        pending: list[tuple[int, str]] = []
        for i in batch:
            hit = cache.get(paragraphs[i])
            if hit is None:
                pending.append((i, paragraphs[i]))
                continue
            report.cache_hits += 1
            # Verified on the way *out* of the cache as well as on the way in.
            # A cache entry is the output of this gate as it stood when the
            # entry was written, and the gate's rules are measured and get
            # tightened — so without this, a correction accepted under an older
            # rule is re-applied for ever and no later run can see it. Not
            # hypothetical: the five sentence deletions `MAX_LOST_CHARS`
            # records are in the cache this repository ships, so every re-index
            # and every `rebuild` of those books would delete them again.
            # `verify` is deterministic and free, so this costs nothing.
            ok, reason, detail = verify(paragraphs[i], hit)
            if ok:
                out[i] = hit
            else:
                report.rejected.append(
                    Rejection(index=i, reason=reason, detail=detail, proposed=hit)
                )

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
                report.rejected.append(
                    Rejection(index=i, reason=reason, detail=detail,
                              proposed=proposed.strip())
                )
                continue
            # Persisted here rather than after the batch. Correction is the
            # most expensive step in the pipeline and the slowest; saving only
            # on completion means a killed run throws away everything it already
            # paid for — observed, after one was interrupted at batch 14 of 19.
            # With a file per entry the write is cheap enough to do at once.
            cache.put(original, proposed.strip())
            out[i] = proposed.strip()

    for original, final in zip(paragraphs, out):
        if final.strip() == original.strip():
            report.unchanged += 1
        else:
            report.changed += 1

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


def _cache_file(root: "pathlib.Path | None" = None) -> pathlib.Path:
    """The pre-2026-09 layout: one JSON holding every entry.

    Still read, never written. See `_ParagraphCache`.
    """
    return (CACHE_DIR if root is None else root) / "paragraphs.json"


class _ParagraphCache:
    """One file per paragraph, replacing a read-modify-write of one JSON.

    The old layout loaded the whole file at the top of a run and rewrote it
    after every batch. That is correct for one process and lossy for two: the
    Temporal worker can be correcting two documents at once, and last-writer-wins
    over a whole dict silently discards the other run's entries. This cache is
    the thing that makes an interrupted correction keep what it paid for — losing
    it quietly is exactly the failure it exists to prevent.

    Per-key files also make the persistence stronger than the comment below the
    batch loop promises: an entry survives from the moment it is verified, not
    from the end of its batch.

    The legacy `paragraphs.json` is read once, lazily, and never written back.
    A repository with 2,379 lines of accumulated corrections in it keeps every
    one of them; nothing has to be migrated.
    """

    def __init__(self, root: "pathlib.Path | None" = None) -> None:
        self.root = CACHE_DIR if root is None else root
        self._legacy: dict[str, str] | None = None

    def _legacy_entries(self) -> dict[str, str]:
        if self._legacy is None:
            try:
                self._legacy = json.loads(
                    _cache_file(self.root).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                self._legacy = {}
        return self._legacy

    def get(self, text: str) -> str | None:
        path = self.root / f"{_key(text)}.txt"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return self._legacy_entries().get(_key(text))

    def put(self, text: str, corrected: str) -> None:
        try:
            os.makedirs(self.root, exist_ok=True)
            path = self.root / f"{_key(text)}.txt"
            # Write-then-rename with the pid in the temporary name, the same
            # pattern `embedcache` uses: a reader must never see half an entry
            # and two processes must not collide on one temporary file.
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(corrected, encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # a cache that cannot be written must not fail the run
