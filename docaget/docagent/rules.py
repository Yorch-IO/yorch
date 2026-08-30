"""Rule learning: propose, then validate adversarially.

This module exists because of a specific mistake. When classifying a book's
end-of-chapter review questions, a rule was written and checked with a script
that reported "11 paragraphs, zero false positives". It was wrong: the checking
script required a dot after the leading number while the implementation made the
dot optional, so the real classifier tagged **nine footnotes as questions**. The
error only surfaced when the classifier's output was printed over the whole
document.

So validation here obeys three rules, and they are not negotiable:

1. A proposed rule is applied to the **whole document**, never only to the sample
   it was derived from.
2. Validation demands an **independent signal**, not a second pass of the same
   logic. A ``preguntas`` rule's matches must also look like questions by a
   different measure; a ``nota`` rule's matches must look bibliographic.
3. Degenerate rules are rejected outright: anything firing on less than
   ``MIN_HIT_RATIO`` or more than ``MAX_HIT_RATIO`` of paragraphs.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .chunk import (
    KIND_BODY,
    KIND_FOOTNOTE,
    KIND_QUESTIONS,
    ChunkRules,
    DocRules,
    heading_level,
    split_paragraphs,
)
from .chunk import IMPERATIVES as _IMPERATIVES
from .vertex import Vertex

# A rule that fires on almost nothing tells us nothing; one that fires on
# everything has stopped discriminating.
MIN_HIT_RATIO = 0.003
MAX_HIT_RATIO = 0.20

# An unnumbered heading pattern matching once is a false positive, not a table
# of contents. Two is the smallest number that can be called a pattern.
_MIN_HEADING_MATCHES = 2

# Independent signals used to check a rule's matches are homogeneous.
# _IMPERATIVES is imported from chunk, not redefined: heading_level needs the same
# list to keep "11. Defina Arrianismo" out of the heading sequence, and two copies
# of one discriminator drifting apart is the original sin this project exists to
# avoid — a checking regex that required a dot the implementation made optional
# pronounced a rule clean while it mislabelled nine footnotes.
_BIBLIOGRAPHIC = re.compile(
    r"(ver\b|véase|vease|cf\.|cfr\.|op\. cit|ibid|pp?\.\s*\d|"
    r"consultar|editorial|traducci[óo]n|\bp[áa]g)",
    re.IGNORECASE,
)

PROPOSE_SYSTEM = """Eres un analista de estructura de documentos. Recibirás evidencia estructural extraída de un documento (líneas que se repiten en muchas páginas, encabezados numerados detectados, párrafos que empiezan con número) y debes proponer reglas para procesarlo.

Devuelve ÚNICAMENTE el JSON pedido.

Sobre header_patterns:
- Son expresiones regulares de Python, ANCLADAS con ^ y $, que deben coincidir con las líneas de encabezado o pie que se repiten en casi todas las páginas.
- Deben ser lo bastante específicas para no coincidir nunca con texto del cuerpo. Si el encabezado es "CULTURA, SOCIEDAD Y CRISTIANISMO", el patrón correcto es `^CULTURA,\\s+SOCIEDAD\\s+Y\\s+CRISTIANISMO$`, no `CULTURA`.
- Si ninguna línea se repite lo suficiente, devuelve una lista vacía. NO inventes patrones.

Sobre heading_l1_max y heading_l2_max:
- Son topes de longitud en caracteres para descartar falsos encabezados. Los títulos de capítulo reales son cortos; una línea numerada larga suele ser una nota al pie o una pregunta de repaso.
- Mira los encabezados numerados de la evidencia y elige topes que los admitan todos con algo de margen, pero que descarten líneas mucho más largas.

Sobre heading_l1_pattern y heading_l2_pattern (LEE ESTO CON ATENCIÓN):
- El detector por defecto SOLO reconoce encabezados NUMERADOS ("2.1 Título"). No ve ningún otro.
- **Si `numbered_headings_detected` viene vacío, el documento no numera sus encabezados.** Ese es justamente el caso en el que DEBES proponer un heading_l1_pattern; devolver null ahí condena al documento a indexarse sin ningún índice de contenidos. No es un motivo para omitirlo: es el motivo para darlo.
- Saca el patrón de `short_lines`: son párrafos cortos de una sola línea. Ahí suelen estar los títulos ("PREDICACIÓN A TRAVÉS DE LA HISTORIA BÍBLICA", "Introducción", "La Predicación en los Profetas"). Busca qué los distingue de la prosa: mayúsculas, longitud, ausencia de punto final.
- La señal que mejor funciona es la AUSENCIA de puntuación de oración: un título no termina en punto y no contiene ni «.» ni «?» ni «¿». Una frase de prosa sí. Usa una clase de caracteres NEGADA, no una lista de los permitidos.
- Ejemplos de patrones válidos: `^[^.?¿!]{3,70}$` para títulos sin puntuación de oración; `^[A-ZÁÉÍÓÚÑ][^.?¿!]{3,60}$` si además empiezan en mayúscula; `^(LIBRO|CAPÍTULO|PARTE)\\s+\\w+$` para partes nombradas con palabras.
- NUNCA incluyas «.», «?» ni «¿» entre los caracteres permitidos. Un intento anterior propuso `^[A-ZÁÉÍÓÚÑ][a-zA-Z0-9\\s¿?().,:;-]{3,70}$` y se rechazó: al admitir «¿?» se quedó con las preguntas de repaso, que tienen exactamente la misma forma que un título.
- heading_l1_pattern es el nivel superior; heading_l2_pattern el inmediatamente inferior. Usa null en el que no apliques.
- Debe coincidir con AL MENOS DOS líneas y no con prosa. Si los encabezados YA están numerados, devuelve null en ambos: el detector numérico ya los cubre.

Sobre question_pattern y footnote_pattern:
- Los documentos académicos suelen tener DOS clases de párrafo que empiezan con número: preguntas de repaso al final de capítulo, y notas al pie.
- Mira la evidencia y determina qué los distingue tipográficamente. Fíjate especialmente en si hay un punto tras el número.
- Devuelve regex ancladas al inicio (^). Si no distingues las dos clases con seguridad, devuelve null en ambas."""

PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "header_patterns": {"type": "array", "items": {"type": "string"}},
        "heading_l1_max": {"type": "integer"},
        "heading_l2_max": {"type": "integer"},
        "heading_l1_pattern": {"type": "string", "nullable": True},
        "heading_l2_pattern": {"type": "string", "nullable": True},
        "question_pattern": {"type": "string", "nullable": True},
        "footnote_pattern": {"type": "string", "nullable": True},
        "reasoning": {"type": "string"},
    },
    "required": ["header_patterns", "heading_l1_max", "heading_l2_max", "reasoning"],
}


@dataclass
class Proposal:
    header_patterns: tuple[str, ...] = ()
    heading_l1_max: int = 40
    heading_l2_max: int = 120
    # Unnumbered heading patterns. None means "this family numbers its headings",
    # which the built-in HEADING_RE already handles.
    heading_l1_pattern: str | None = None
    heading_l2_pattern: str | None = None
    question_pattern: str | None = None
    footnote_pattern: str | None = None
    reasoning: str = ""


@dataclass
class Finding:
    rule: str
    ok: bool
    detail: str


# Rules whose failure genuinely blocks adoption, because getting them wrong
# changes what text comes out of the extractor at all.
ESSENTIAL_RULES = frozenset({"header_patterns", "heading_guards"})
# Rules where failure just means "keep the built-in", and the built-ins were
# themselves arrived at by measurement — so an unlearnable one is not a defect.
OPTIONAL_RULES = frozenset(
    {"question_pattern", "footnote_pattern", "heading_patterns"}
)


@dataclass
class Validation:
    findings: list[Finding] = field(default_factory=list)
    # Feedback handed back to the model on a refine round.
    feedback: list[str] = field(default_factory=list)
    #: Which heading levels the learned patterns validated for.
    #:
    #: Level 1 and level 2 are checked separately but report under one rule
    #: name, so `failed_rules()` cannot tell them apart — and a rejected level-2
    #: pattern would then discard a level-1 pattern that passed. That is the
    #: same mistake `passed` records for whole proposals, one level down.
    heading_levels_ok: set[int] = field(default_factory=set)
    #: Which rules block a refine round **for this document**.
    #:
    #: Not simply ``ESSENTIAL_RULES``, because whether ``heading_patterns``
    #: matters at all depends on the document: on one that numbers its headings
    #: the built-in detector already produces an outline and a rejected pattern
    #: costs nothing, while on one that numbers nothing the pattern is the *only*
    #: thing that can. ``validate`` promotes it in the second case. See there.
    essential: frozenset[str] = ESSENTIAL_RULES

    def failed_rules(self) -> set[str]:
        return {f.rule for f in self.findings if not f.ok}

    @property
    def passed(self) -> bool:
        """Whether the proposal can be adopted, in whole or in part.

        Only the essential rules block. An earlier version required every rule to
        pass, and it threw away real work: on the reference book the header pattern
        validated cleanly three times in a row, but a degenerate ``footnote_pattern``
        failed alongside it and the whole proposal was discarded. The run then fell
        back to defaults with no header pattern, so 175 running-header lines stayed
        in the text — 552 paragraphs instead of 377, 135 chunks carrying the header
        as noise, and a paid correction pass over 175 copies of the same line.
        """
        return not (self.failed_rules() & self.essential)

    @property
    def fully_passed(self) -> bool:
        return all(f.ok for f in self.findings)

    def notes(self) -> list[str]:
        return [f"{'OK ' if f.ok else 'FAIL'} {f.rule}: {f.detail}" for f in self.findings]


# --- propose -----------------------------------------------------------------


def propose(vertex: Vertex, evidence, feedback: list[str] | None = None) -> Proposal:
    """Ask the model for structural rules, given the evidence and any prior
    validation feedback."""
    payload = {
        "repeated_lines": [
            {"text": t, "pages": n} for t, n in list(evidence.repeated_lines.items())[:20]
        ],
        "pages": evidence.pages,
        "numbered_headings_detected": evidence.numbered_lines[:25],
        "paragraphs_starting_with_a_number": evidence.numbered_paragraphs[:25],
        "short_lines": list(getattr(evidence, "short_lines", []))[:40],
        "first_lines_sample": evidence.first_lines[:8],
        "last_lines_sample": evidence.last_lines[:8],
    }
    prompt = json.dumps(payload, ensure_ascii=False, indent=2)
    if feedback:
        prompt += (
            "\n\nUn intento anterior falló la validación por estas razones. "
            "Corrígelas:\n- " + "\n- ".join(feedback)
        )

    raw = vertex.generate(
        prompt, system=PROPOSE_SYSTEM, stage="propose", json_schema=PROPOSE_SCHEMA
    )
    d = json.loads(raw)
    return Proposal(
        header_patterns=tuple(d.get("header_patterns") or ()),
        heading_l1_max=int(d.get("heading_l1_max") or 40),
        heading_l2_max=int(d.get("heading_l2_max") or 120),
        heading_l1_pattern=d.get("heading_l1_pattern") or None,
        heading_l2_pattern=d.get("heading_l2_pattern") or None,
        question_pattern=d.get("question_pattern") or None,
        footnote_pattern=d.get("footnote_pattern") or None,
        reasoning=d.get("reasoning", ""),
    )


# --- validate ----------------------------------------------------------------


def validate(proposal: Proposal, text: bytes, evidence) -> Validation:
    """Check a proposal against the *whole* document.

    Every check below is deterministic. The model proposed; it does not get to
    judge its own work.
    """
    v = Validation()
    paras = split_paragraphs(text)
    lines = [p.text for p in paras]
    total = max(1, len(paras))

    _check_regexes_compile(proposal, v)
    _check_header_patterns(proposal, v, lines, evidence)
    # The pattern check runs *before* the guards, because the guards consult its
    # verdict: they are allowed to pass unexercised on a document that numbers
    # nothing only when a learned pattern actually supplies the structure. A
    # pattern that was itself rejected supplies none, and the free pass would
    # then rest on nothing.
    _check_heading_patterns(proposal, v, lines, total)
    _check_heading_guards(proposal, v, lines)
    _check_kind_patterns(proposal, v, lines, total)

    # `heading_patterns` is optional on a document that numbers its headings —
    # the built-in detector already produces an outline there, so a rejected
    # pattern costs nothing and is not worth another paid call. On a document
    # that numbers *nothing* it is the only rule that can produce an outline at
    # all, and treating it as optional there was a real defect: the model
    # proposed a pattern matching prose, the checker refused it and wrote precise
    # feedback ("it is taking review questions; exclude them"), and because
    # nothing essential had failed that feedback was never sent back. One
    # attempt, pattern dropped, document indexed with no table of contents — the
    # exact outcome the rule exists to prevent.
    if not _numbers_its_headings(proposal, lines):
        v.essential = ESSENTIAL_RULES | {"heading_patterns"}
    return v


def _numbers_its_headings(p: Proposal, lines: list[str]) -> bool:
    """Whether the built-in numeric detector finds any level-1 heading here.

    Uses the proposal's own guards, so this cannot disagree with
    `_check_heading_guards` about what the document contains.
    """
    rules = ChunkRules(heading_l1_max=p.heading_l1_max, heading_l2_max=p.heading_l2_max)
    return any(heading_level(ln, rules) == 1 for ln in lines)


def _check_regexes_compile(p: Proposal, v: Validation) -> None:
    for name, pattern in (
        *[("header_patterns", h) for h in p.header_patterns],
        ("heading_patterns", p.heading_l1_pattern),
        ("heading_patterns", p.heading_l2_pattern),
        ("question_pattern", p.question_pattern),
        ("footnote_pattern", p.footnote_pattern),
    ):
        if not pattern:
            continue
        try:
            re.compile(pattern)
        except re.error as e:
            v.findings.append(Finding(name, False, f"{pattern!r} does not compile: {e}"))
            v.feedback.append(f"La regex {pattern!r} no compila en Python: {e}")


def _check_header_patterns(p: Proposal, v: Validation, lines: list[str], evidence) -> None:
    """A header pattern must match a line that genuinely repeats across pages, and
    must not match body prose."""
    if not p.header_patterns:
        v.findings.append(
            Finding("header_patterns", True, "none proposed; nothing to strip")
        )
        return

    repeated = {t: n for t, n in getattr(evidence, "repeated_lines", {}).items()}
    for pattern in p.header_patterns:
        try:
            rx = re.compile(pattern)
        except re.error:
            continue  # already reported

        if not (pattern.startswith("^") and pattern.endswith("$")):
            v.findings.append(
                Finding(
                    "header_patterns",
                    False,
                    f"{pattern!r} is not anchored; an unanchored pattern strips "
                    "in-body mentions of the same phrase",
                )
            )
            v.feedback.append(f"El patrón {pattern!r} debe estar anclado con ^ y $.")
            continue

        matched_repeats = [t for t in repeated if rx.match(t)]
        if not matched_repeats:
            v.findings.append(
                Finding(
                    "header_patterns",
                    False,
                    f"{pattern!r} matches none of the lines that actually repeat "
                    f"across pages (candidates: {list(repeated)[:3]})",
                )
            )
            v.feedback.append(
                f"El patrón {pattern!r} no coincide con ninguna línea repetida real. "
                f"Las candidatas son: {list(repeated)[:5]}"
            )
            continue

        # Independent signal: how much *body* does it hit? A header pattern should
        # only ever match short standalone lines, never paragraphs of prose.
        prose_hits = [ln for ln in lines if len(ln) > 200 and rx.search(ln)]
        if prose_hits:
            v.findings.append(
                Finding(
                    "header_patterns",
                    False,
                    f"{pattern!r} also matches {len(prose_hits)} body paragraphs",
                )
            )
            v.feedback.append(
                f"El patrón {pattern!r} también coincide con párrafos de cuerpo; "
                "hazlo más específico."
            )
            continue

        v.findings.append(
            Finding(
                "header_patterns",
                True,
                f"{pattern!r} matches {matched_repeats[0]!r} "
                f"on {repeated[matched_repeats[0]]} pages, no body hits",
            )
        )


def _check_heading_guards(p: Proposal, v: Validation, lines: list[str]) -> None:
    """The guards must admit the real headings and reject the long numbered lines.

    The independent signal is the **numbering sequence**, which knows nothing
    about the length guard being tested: real chapters run 1, 2, 3, … with no
    repeats, because a document has exactly one chapter 3. A guard loose enough to
    swallow review questions or footnotes — both of which restart at 1 — produces
    duplicate level-1 numbers, and that is arithmetic rather than opinion.

    An earlier version of this check compared the level-1 count against the
    level-2 count. It was too weak: at ``l1_max=400`` this book yields 9 chapters
    and 59 subsections, which passes that test while being obviously wrong.
    """
    # Built from the length guards *only*, deliberately: this check measures the
    # numbered path, and folding a learned unnumbered pattern in here would both
    # blur what is being validated and hand `int()` a line with no number in it.
    # `_check_heading_patterns` is what judges that rule.
    rules = ChunkRules(heading_l1_max=p.heading_l1_max, heading_l2_max=p.heading_l2_max)

    l1_numbers: list[int] = []
    l2plus = 0
    for ln in lines:
        level = heading_level(ln, rules)
        if level == 1:
            l1_numbers.append(int(ln.split()[0].rstrip(".").split(".")[0]))
        elif level >= 2:
            l2plus += 1

    if not l1_numbers:
        proposed = bool(p.heading_l1_pattern or p.heading_l2_pattern)
        if proposed and v.heading_levels_ok:
            # A document that numbers its parts in words ("LIBRO PRIMERO") has no
            # numbered heading for these guards to admit, and never will. Failing
            # here would block adoption of the very rule that gives such a
            # document a table of contents — `heading_guards` is essential, so
            # one FAIL discards the whole proposal — and the feedback would ask
            # the model to raise a cap that is not the problem.
            #
            # Passing is honest but weaker than it looks: the caps are recorded
            # unexercised, and a *sibling* document of this family that does
            # number its headings would inherit them unvalidated. The alternative
            # is that this family can never learn a profile at all.
            v.findings.append(
                Finding(
                    "heading_guards",
                    True,
                    "no numbered headings in this document; the guards are "
                    "unexercised and structure comes from the learned pattern",
                )
            )
            return
        if proposed:
            # A pattern was offered and rejected, so nothing supplies this
            # document's structure. Say that rather than the misleading "raise
            # heading_l1_max", which would not help: there is no number to cap.
            v.findings.append(
                Finding(
                    "heading_guards",
                    False,
                    "no numbered headings, and the proposed heading pattern was "
                    "rejected — nothing would give this document an outline",
                )
            )
            v.feedback.append(
                "El documento no numera sus encabezados y el patrón propuesto "
                "para los no numerados se ha rechazado, así que quedaría sin "
                "índice de contenidos. Propón un heading_l1_pattern válido a "
                "partir de short_lines."
            )
            return
        v.findings.append(
            Finding(
                "heading_guards",
                False,
                f"l1_max={p.heading_l1_max} admits no level-1 headings at all",
            )
        )
        v.feedback.append(
            f"Con heading_l1_max={p.heading_l1_max} no se detecta ningún capítulo. "
            "Súbelo, o propón un heading_l1_pattern si los encabezados no llevan número."
        )
        return

    # A learned pattern that validated and explains strictly more level-1
    # headings than the numeric path is what supplies this document's outline;
    # the numbered path is then measuring something else. Observed on
    # `01_RetoDeDios_INT-S.pdf`: 30 "Capítulo N" headings against 16 numbered
    # lines that are this publisher's footnotes ("2. Ibídem."), which restart at
    # 1 in every chapter and so produce duplicates [2, 3, 4]. `heading_guards`
    # is essential, so that FAIL discarded a pattern the checker had just
    # confirmed against the document, and a 304-page book indexed with no
    # outline at all.
    #
    # This is the same reasoning as the "no numbered headings" branch above,
    # applied to a document that has a few spurious ones rather than none. The
    # comparison is strict and by count, so it cannot fire on a document whose
    # real chapters are numbered — there the numeric path explains at least as
    # much, and the arithmetic check stands.
    if p.heading_l1_pattern and 1 in v.heading_levels_ok:
        try:
            rx = re.compile(p.heading_l1_pattern)
        except re.error:
            rx = None
        if rx is not None:
            by_pattern = sum(
                1 for ln in lines if rx.match(ln) and len(ln) <= p.heading_l1_max
            )
            if by_pattern > len(l1_numbers):
                v.findings.append(
                    Finding(
                        "heading_guards",
                        True,
                        f"the learned pattern explains {by_pattern} level-1 "
                        f"headings against {len(l1_numbers)} numbered lines, so "
                        "the numbered path is footnote noise here and its "
                        "sequence is not the signal",
                    )
                )
                return

    duplicates = sorted({n for n in l1_numbers if l1_numbers.count(n) > 1})
    if duplicates:
        v.findings.append(
            Finding(
                "heading_guards",
                False,
                f"l1_max={p.heading_l1_max} yields {len(l1_numbers)} level-1 headings "
                f"with duplicate numbers {duplicates}: a document has one chapter "
                f"{duplicates[0]}, so the guard is admitting review questions or "
                "footnotes as chapters",
            )
        )
        v.feedback.append(
            f"heading_l1_max={p.heading_l1_max} produce números de capítulo repetidos "
            f"{duplicates}, lo que es imposible. Las preguntas de repaso y las notas al "
            "pie reinician la numeración en 1; baja el tope para excluirlas."
        )
        return

    is_monotonic = all(l1_numbers[i] < l1_numbers[i+1] for i in range(len(l1_numbers) - 1))
    if not is_monotonic:
        v.findings.append(
            Finding(
                "heading_guards",
                False,
                f"level-1 numbering is {l1_numbers}, not monotonic",
            )
        )
        v.feedback.append(
            f"La numeración de capítulos detectada es {l1_numbers}, que no es monotónica. Ajusta los topes."
        )
        return

    is_contiguous = (max(l1_numbers) - min(l1_numbers) + 1) == len(l1_numbers)
    if not is_contiguous:
        v.findings.append(
            Finding(
                "heading_guards",
                False,
                f"level-1 numbering is {l1_numbers}, which is not contiguous",
            )
        )
        v.feedback.append(
            f"La numeración de capítulos detectada es {l1_numbers}, que no es contigua. Ajusta los topes."
        )
        return

    v.findings.append(
        Finding(
            "heading_guards",
            True,
            f"{len(l1_numbers)} chapters numbered {l1_numbers}, {l2plus} subsections",
        )
    )


def _check_heading_patterns(
    p: Proposal, v: Validation, lines: list[str], total: int
) -> None:
    """Check a learned pattern for headings that carry no number.

    **This check is weaker than ``_check_heading_guards`` and the difference
    matters.** That one has the numbering sequence — an independent, arithmetic
    signal that caught a proposal reading a year as a chapter (`[1,2,4,…,1991,13,
    15]`). An unnumbered heading has no sequence, so there is nothing here that
    can catch a pattern matching the *wrong* short lines. What is checked is only
    that the matches are plausible as a set:

    * at least ``_MIN_HEADING_MATCHES`` of them — one match is a false positive,
      not a table of contents;
    * every match inside the level's own length guard, so the rule cannot smuggle
      prose past a cap the numbered path obeys;
    * a hit ratio inside the same degeneracy bounds every other rule obeys;
    * no match that the questions or footnotes rule also claims, since a review
      question promoted to a chapter is the exact failure invariant #11 exists
      for.

    A proposal that declines to name a pattern passes trivially: the numbered
    detector is a known-good default, and "this family numbers its headings" is
    the common case rather than a gap.
    """
    for pattern, cap, level in (
        (p.heading_l1_pattern, p.heading_l1_max, 1),
        (p.heading_l2_pattern, p.heading_l2_max, 2),
    ):
        if not pattern:
            continue
        before = sum(1 for f in v.findings if f.rule == "heading_patterns" and not f.ok)
        try:
            rx = re.compile(pattern)
        except re.error:
            continue  # already reported by _check_regexes_compile
        hits = [ln for ln in lines if rx.match(ln)]
        ratio = len(hits) / total

        if len(hits) < _MIN_HEADING_MATCHES:
            v.findings.append(
                Finding(
                    "heading_patterns",
                    False,
                    f"nivel {level}: {pattern!r} coincide con {len(hits)} línea(s); "
                    f"un índice necesita al menos {_MIN_HEADING_MATCHES}",
                )
            )
            v.feedback.append(
                f"El patrón de encabezado {pattern!r} solo coincide con "
                f"{len(hits)} línea(s) del documento. Un encabezado que aparece "
                "una sola vez no es un patrón: propón uno más general o null."
            )
            continue

        if ratio > MAX_HIT_RATIO:
            v.findings.append(
                Finding(
                    "heading_patterns",
                    False,
                    f"nivel {level}: {pattern!r} coincide con {ratio:.1%} de los "
                    f"párrafos — ha dejado de discriminar",
                )
            )
            v.feedback.append(
                f"El patrón de encabezado {pattern!r} coincide con {ratio:.1%} de "
                "los párrafos. Un encabezado es raro: haz el patrón más específico."
            )
            continue

        if long := [ln for ln in hits if len(ln) > cap]:
            v.findings.append(
                Finding(
                    "heading_patterns",
                    False,
                    f"nivel {level}: {len(long)} coincidencia(s) superan el tope de "
                    f"{cap} caracteres, p. ej. {long[0][:80]!r}",
                )
            )
            v.feedback.append(
                f"El patrón {pattern!r} coincide con líneas más largas que "
                f"{cap} caracteres, que son prosa y no títulos. Ánclalo mejor."
            )
            continue

        if stolen := [ln for ln in hits if _looks_like_question(ln)]:
            v.findings.append(
                Finding(
                    "heading_patterns",
                    False,
                    f"nivel {level}: {len(stolen)} coincidencia(s) son preguntas de "
                    f"repaso, p. ej. {stolen[0][:80]!r}",
                )
            )
            v.feedback.append(
                f"El patrón {pattern!r} se está quedando con preguntas de repaso. "
                "Un encabezado y una pregunta numerada tienen la misma forma; "
                "excluye las preguntas."
            )
            continue

        v.findings.append(
            Finding(
                "heading_patterns",
                True,
                f"nivel {level}: {pattern!r} -> {len(hits)} encabezados sin numerar "
                f"({ratio:.1%} de los párrafos)",
            )
        )
        assert before == sum(
            1 for f in v.findings if f.rule == "heading_patterns" and not f.ok
        ), "a level that reached here added no failure of its own"
        v.heading_levels_ok.add(level)


def _check_kind_patterns(
    p: Proposal, v: Validation, lines: list[str], total: int
) -> None:
    """The check that the original bug would have failed.

    Applies each proposed pattern to every paragraph, then demands an
    **independent** signal of homogeneity: question matches must actually look
    like questions (¿ marks or an imperative verb), footnote matches must actually
    look bibliographic. A rule whose matches are a mix of both is rejected even
    though the regex "works".
    """
    for name, pattern, signal, label in (
        ("question_pattern", p.question_pattern, _looks_like_question, "question"),
        ("footnote_pattern", p.footnote_pattern, _looks_like_footnote, "footnote"),
    ):
        if not pattern:
            v.findings.append(Finding(name, True, "none proposed; defaults apply"))
            continue
        try:
            rx = re.compile(pattern)
        except re.error:
            continue

        hits = [ln for ln in lines if rx.match(ln)]
        ratio = len(hits) / total

        if ratio < MIN_HIT_RATIO:
            v.findings.append(
                Finding(name, False, f"degenerate: fires on {len(hits)}/{total} paragraphs")
            )
            v.feedback.append(
                f"{name}={pattern!r} solo coincide con {len(hits)} de {total} párrafos; "
                "es demasiado restrictivo."
            )
            continue
        if ratio > MAX_HIT_RATIO:
            v.findings.append(
                Finding(
                    name,
                    False,
                    f"degenerate: fires on {len(hits)}/{total} paragraphs "
                    f"({ratio:.0%}), it has stopped discriminating",
                )
            )
            v.feedback.append(
                f"{name}={pattern!r} coincide con el {ratio:.0%} de los párrafos; "
                "es demasiado laxo."
            )
            continue

        agreeing = [ln for ln in hits if signal(ln)]
        purity = len(agreeing) / len(hits)
        if purity < 0.7:
            impostors = [ln[:90] for ln in hits if not signal(ln)][:3]
            v.findings.append(
                Finding(
                    name,
                    False,
                    f"only {purity:.0%} of {len(hits)} matches look like a {label} "
                    f"by an independent signal; e.g. {impostors}",
                )
            )
            v.feedback.append(
                f"{name}={pattern!r} captura elementos que NO son {label}: {impostors}. "
                "El discriminador tipográfico que elegiste no separa las dos clases."
            )
            continue

        v.findings.append(
            Finding(
                name,
                True,
                f"{len(hits)}/{total} paragraphs, {purity:.0%} confirmed by an "
                "independent signal",
            )
        )


def _looks_like_question(text: str) -> bool:
    """Independent of any numbering: does it read as a question or an exercise?"""
    return "¿" in text or text.rstrip().endswith("?") or bool(_IMPERATIVES.search(text))


def _looks_like_footnote(text: str) -> bool:
    """Independent of any numbering: does it read as a bibliographic aside?"""
    if _looks_like_question(text):
        return False
    return bool(_BIBLIOGRAPHIC.search(text)) or len(text) > 120


# --- apply -------------------------------------------------------------------


def to_rules(p: Proposal) -> tuple[DocRules, ChunkRules]:
    return (
        DocRules(header_patterns=p.header_patterns),
        ChunkRules(
            heading_l1_max=p.heading_l1_max,
            heading_l2_max=p.heading_l2_max,
            heading_l1_pattern=p.heading_l1_pattern,
            heading_l2_pattern=p.heading_l2_pattern,
        ),
    )


def adopt(
    proposal: Proposal,
    validation: Validation,
    *,
    fingerprint: str,
    slug: str,
    extractor: str,
    learned_from: str,
) -> "profiles.Profile":
    """Turn a validated proposal into a profile, rule by rule.

    Adoption is **partial**: a rule that failed validation is dropped and its
    built-in default applies, while the rules that passed are kept. Discarding a
    whole proposal because one optional rule was degenerate throws away real work
    — and did: a header pattern that validated cleanly three times was lost
    alongside a bad footnote pattern, leaving 175 running-header lines in the
    text.

    Lives here rather than in the LangGraph node that used to hold it because the
    Temporal worker adopts profiles too, and two implementations of "drop only
    what failed" is exactly how that measured rule gets quietly lost.
    """
    from . import profiles

    failed = validation.failed_rules()
    doc_rules, chunk_rules = to_rules(proposal)
    if "header_patterns" in failed:
        doc_rules = DocRules()
    if "heading_guards" in failed:
        chunk_rules = ChunkRules()
    # Per level, not per rule name: the two heading levels report under one name,
    # so dropping both because one failed discards work the checker confirmed.
    # Same reasoning as partial adoption above, one granularity down — observed
    # when a level-2 pattern matching 32% of the paragraphs took a level-1
    # pattern that explained the book's 30 chapters down with it.
    if "heading_patterns" in failed:
        chunk_rules = replace(
            chunk_rules,
            heading_l1_pattern=(
                chunk_rules.heading_l1_pattern if 1 in validation.heading_levels_ok else None
            ),
            heading_l2_pattern=(
                chunk_rules.heading_l2_pattern if 2 in validation.heading_levels_ok else None
            ),
        )

    return profiles.Profile(
        fingerprint=fingerprint,
        slug=slug,
        extractor=extractor,
        doc_rules=doc_rules,
        chunk_rules=chunk_rules,
        learned_from=learned_from,
        learned_at=time.time(),
        question_pattern=(None if "question_pattern" in failed else proposal.question_pattern),
        footnote_pattern=(None if "footnote_pattern" in failed else proposal.footnote_pattern),
        validation_notes=validation.notes(),
    )


def adopted_rules(validation: Validation) -> list[str]:
    """The rule names that survived, for a log line or a UI label."""
    return sorted((ESSENTIAL_RULES | OPTIONAL_RULES) - validation.failed_rules())


def classifier_for(profile) -> "Callable[[str, ChunkRules], str] | None":
    """A `build_chunks` classifier for a profile's learned kind patterns.

    Returns None when the profile learned neither, so the caller passes nothing
    and the chunker uses its own measured defaults — an unproposed pattern is a
    known-good fallback, not a gap.
    """
    q, f = profile.question_pattern, profile.footnote_pattern
    if not (q or f):
        return None
    return lambda text, rules: classify_with(text, rules, q, f)


def classify_with(
    text: str, chunk_rules: ChunkRules, question_re: str | None, footnote_re: str | None
) -> str:
    """``chunk.classify_kind`` with learned patterns substituted in.

    Falls back to the built-in rules when the learner declined to propose one —
    those defaults were themselves measured, so an unproposed pattern is not a
    gap.
    """
    from .chunk import classify_kind

    if heading_level(text, chunk_rules) > 0:
        return KIND_BODY
    if question_re and re.match(question_re, text):
        return KIND_QUESTIONS
    if footnote_re and re.match(footnote_re, text):
        return KIND_FOOTNOTE
    return classify_kind(text, chunk_rules)
