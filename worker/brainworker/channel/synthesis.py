"""A comparative, neutral reading of what a channel's sermons say about a topic.

The same retrieval as a question — `answering.retrieve.search`, unchanged, so
the tenant and library scoping, the dense floor, `diversify`, the concept
expansion and the locator attachment all come for free, and every figure
measured against the one-shot path keeps holding. What is different is the
*shape* of the answer and what the code checks about it.

**Five sections, and the split between two of them is the whole point.**
`hallazgos` are descriptive statements the fragments support; every one of them
must name a citation the code verified, or it is **moved to `limitaciones`** and
counted. `interpretacion_teologica` is inference — the thing a reader asked for
and the thing most easily mistaken for evidence — so it is a different field,
carries its own `alcance` and `limites`, and is rendered apart. Merging the two
into a single prose answer would put the one text a reader must treat sceptically
in the same paragraph as the one they may rely on.

**A finding with no surviving citation is demoted, not deleted.** Deleting it
would leave an answer that looks complete and is quietly shorter; the reader
would never know a claim had been made and dropped. `demoted` reports how many,
because a synthesis where half the findings were demoted is one to distrust.

**Neutrality is a section of the prompt, not a claim about the output.** The six
answering rules come first, unchanged, and `effort.compose_system` puts the
neutrality section below them with the sentence that says the rules win — the
same arrangement a per-organisation answer style gets, and for the same reason:
how a thing is written is a house matter, and that it may not use general
knowledge is not.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..answering import answer as answer_mod
from ..answering.effort import compose_system
from ..answering.types import Citation, Evidence, Question
from ..pipeline import Spend
from ..providers import Provider, VertexAdapter
from ..providers.adapter import TruncatedResponse

log = logging.getLogger(__name__)

STAGE = "channel-synthesis"

#: Bumped when `NEUTRALITY` or `SCHEMA` changes in a way that could move an
#: answer. Recorded beside the model id, for the reason a preselection records
#: its own: a reading whose instrument is unrecorded cannot be compared.
PROMPT_VERSION = "synthesis/1"

#: The same ceiling `answer.MAX_OUTPUT_TOKENS` is, imported rather than repeated.
#: It bounds the *bill*, not the reasoning: a call with dynamic thinking can
#: spend the model's whole output allowance reasoning and return no text at all,
#: measured once at $0.497373 for nothing.
MAX_OUTPUT_TOKENS = answer_mod.MAX_OUTPUT_TOKENS

NEUTRALITY = """Este trabajo es una lectura comparativa de varias prédicas. \
Dos cosas más, que no sustituyen a ninguna regla de arriba:

A. Separa lo que los fragmentos dicen de lo que tú concluyes. Un `hallazgo` es \
una afirmación descriptiva sobre lo que se predicó, y va con sus `chunk_ids`. \
Una `interpretacion_teologica` es una inferencia tuya: dice su `alcance` (sobre \
qué se sostiene) y sus `limites` (qué no permite concluir).
B. No arbitres entre tradiciones. No digas cuál postura es la correcta, la más \
bíblica o la más fiel. Describe lo que cada prédica sostiene y en qué se \
diferencian; si el corpus solo representa una postura, eso es una `limitacion`, \
no un consenso.
C. No le atribuyas a un predicador una conclusión que no aparece en sus \
fragmentos, ni siquiera cuando se siga de lo que sí dice.
D. `comparacion` compara prédicas entre sí. Si solo hay una, o si las que hay \
no se tocan, dilo en `limitaciones` en vez de inventar un contraste."""

SCHEMA = {
    "type": "object",
    "properties": {
        "hallazgos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "afirmacion": {"type": "string"},
                    "chunk_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["afirmacion", "chunk_ids"],
            },
        },
        "comparacion": {
            "type": "object",
            "properties": {
                "convergencias": {"type": "array", "items": {"type": "string"}},
                "diferencias": {"type": "array", "items": {"type": "string"}},
                "matices": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["convergencias", "diferencias", "matices"],
        },
        "interpretacion_teologica": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "inferencia": {"type": "string"},
                    "alcance": {"type": "string"},
                    "limites": {"type": "string"},
                },
                "required": ["inferencia", "alcance", "limites"],
            },
        },
        # Last, deliberately: the same ordering `answer.SCHEMA` uses, so a
        # streamed envelope cannot be verified before it closes and nothing is
        # tempted to verify half of one.
        "citas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string"},
                    "afirmacion": {"type": "string"},
                },
                "required": ["chunk_id", "afirmacion"],
            },
        },
        "limitaciones": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["hallazgos", "comparacion", "citas", "limitaciones"],
}


@dataclass
class Finding:
    """One descriptive statement, and the citations that survived checking."""

    afirmacion: str
    chunk_ids: list[str] = field(default_factory=list)


@dataclass
class Inference:
    """One reading, labelled as a reading. Never merged with a finding."""

    inferencia: str
    alcance: str = ""
    limites: str = ""


@dataclass
class Comparison:
    convergencias: list[str] = field(default_factory=list)
    diferencias: list[str] = field(default_factory=list)
    matices: list[str] = field(default_factory=list)


@dataclass
class Synthesis:
    """What the corpus was read to say, and how much of it checked out.

    Every field declared, and the tests assert through `asdict`: a field set as
    a loose attribute is one `asdict` never writes, which is how
    `Answer.style_effort` was missing from every response with no error
    anywhere.
    """

    state: str = "answered"
    topic: str = ""
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    hallazgos: list[Finding] = field(default_factory=list)
    comparacion: Comparison = field(default_factory=Comparison)
    interpretacion_teologica: list[Inference] = field(default_factory=list)
    citas: list[Citation] = field(default_factory=list)
    limitaciones: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    #: Findings moved to `limitaciones` for naming no citation that survived.
    #: A synthesis where half the findings were demoted is one to distrust, and
    #: a count is the only thing that says so.
    demoted: int = 0
    #: Chunk ids the model cited that it was never shown.
    invented: int = 0
    #: Why, when `state` is not `answered`. The only thing that says *which*
    #: refusal this is, which is the defect `_settle` already had once.
    reason: str = ""
    spend: list[Spend] = field(default_factory=list)


def compose(
    provider: Provider, question: Question, evidence: list[Evidence]
) -> Synthesis:
    """Read the evidence into five sections, keeping only what checks out."""
    base = Synthesis(topic=question.text, model=provider.settings.model)
    if not evidence:
        base.state = "insufficient_evidence"
        base.reason = "la búsqueda no devolvió ningún fragmento de esta biblioteca"
        return base

    adapter = VertexAdapter(provider)
    try:
        raw = adapter.generate_json(
            answer_mod._prompt(question, evidence),
            system=compose_system(answer_mod.SYSTEM, NEUTRALITY),
            schema=SCHEMA,
            stage=STAGE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    except TruncatedResponse as e:
        # Told apart from the corpus coming up short, because the remedy is
        # different and the wrong one is actively misleading: this is the model
        # spending its whole output allowance and never writing the answer.
        base.state = "insufficient_evidence"
        base.reason = (
            "el modelo agotó su límite de salida y no llegó a escribir la "
            f"síntesis ({e.output_tokens} tokens de salida, ningún texto): "
            "vuelve a preguntar, o acota más el tema"
        )
        base.evidence = evidence
        base.spend = [_spend(provider, adapter)]
        return base
    except Exception as e:  # noqa: BLE001 — a refusal is a first-class outcome
        log.warning("could not compose the synthesis: %s", e)
        base.state = "insufficient_evidence"
        base.reason = f"el modelo no devolvió una síntesis utilizable: {e}"
        base.evidence = evidence
        base.spend = [_spend(provider, adapter)]
        return base

    citations, invented = answer_mod._verify(raw.get("citas") or [], evidence)
    kept = {c.chunk_id for c in citations}

    findings: list[Finding] = []
    limits = [str(x).strip() for x in (raw.get("limitaciones") or []) if str(x).strip()]
    demoted = 0
    for row in raw.get("hallazgos") or []:
        if not isinstance(row, dict):
            continue
        text = answer_mod._clean(str(row.get("afirmacion") or ""))
        if not text:
            continue
        ids = [str(i) for i in (row.get("chunk_ids") or []) if str(i) in kept]
        if ids:
            findings.append(Finding(afirmacion=text, chunk_ids=ids))
        else:
            # Demoted, not deleted. Deleting would leave an answer that looks
            # complete and is quietly shorter, and the reader would never know
            # a claim had been made and dropped.
            demoted += 1
            limits.append(f"Sin cita comprobable: {text}")

    comparison = raw.get("comparacion") or {}
    result = Synthesis(
        state="answered" if findings else "insufficient_evidence",
        topic=question.text,
        model=provider.settings.model,
        hallazgos=findings,
        comparacion=Comparison(
            convergencias=_strings(comparison.get("convergencias")),
            diferencias=_strings(comparison.get("diferencias")),
            matices=_strings(comparison.get("matices")),
        ),
        interpretacion_teologica=[
            Inference(
                inferencia=answer_mod._clean(str(row.get("inferencia") or "")),
                alcance=str(row.get("alcance") or "").strip(),
                limites=str(row.get("limites") or "").strip(),
            )
            for row in (raw.get("interpretacion_teologica") or [])
            if isinstance(row, dict) and str(row.get("inferencia") or "").strip()
        ],
        citas=citations,
        limitaciones=limits,
        evidence=evidence,
        demoted=demoted,
        invented=len(invented),
        spend=[_spend(provider, adapter)],
    )
    if not findings:
        result.reason = (
            "ninguna afirmación quedó respaldada por una cita comprobable en "
            "los fragmentos recuperados"
        )
        # An interpretation resting on nothing checkable is the one thing this
        # must not print: without a finding to hang off, it is the model's own
        # reading of a corpus it may not have read.
        result.interpretacion_teologica = []
    return result


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def _spend(provider: Provider, adapter: VertexAdapter) -> Spend:
    from ..activities.ingest import price_for

    usage = adapter.usage
    return Spend(
        stage=STAGE,
        model=provider.settings.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        usd=price_for(provider.settings.model, usage.input_tokens, usage.output_tokens),
    )
