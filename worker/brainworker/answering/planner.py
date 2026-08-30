"""Choosing how to look, without letting a model write the query.

The planner sees the *catalogue* of approved templates — id, summary, parameter
names and types — and returns one id plus typed parameters. It never sees the
Cypher and never emits any. That is the whole security model for question
answering, because Memgraph does not enforce read-only (see
`brainworker.graph.client`), and it is why this module validates the planner's
output through `queries.bind` before anything reaches the database.

A planner that returns nonsense is an ordinary event, not an attack: models omit
fields and pass strings where integers belong. Every rejection here degrades to
vector-only retrieval rather than failing the question.
"""

from __future__ import annotations

import json
import logging

from ..graph import queries
from ..pipeline import Spend
from ..providers import Provider, VertexAdapter
from .types import Plan, Question

log = logging.getLogger(__name__)

SYSTEM = """Eres un planificador de consultas sobre una biblioteca documental.

Recibes una pregunta y un catálogo de consultas de grafo permitidas. Tu tarea es
elegir COMO MUCHO una de ellas y aportar sus parámetros, o decidir que ninguna
ayuda y que basta con la búsqueda por similitud.

Reglas:
- Devuelves un identificador del catálogo, nunca una consulta escrita por ti.
- Si la pregunta no encaja con ninguna plantilla, devuelves template_id nulo.
  Es una respuesta correcta y frecuente, no un fallo.
- `conceptos` son los términos temáticos que la pregunta menciona, en su forma
  canónica y en español. Sirven para localizar nodos, no para responder.
- No respondes a la pregunta. Solo decides cómo buscarla."""

SCHEMA = {
    "type": "object",
    "properties": {
        "intencion": {
            "type": "string",
            "enum": ["busqueda", "estructura", "relacion", "comparacion", "fuera_de_alcance"],
        },
        "template_id": {"type": "string"},
        "parametros": {"type": "object"},
        "conceptos": {"type": "array", "items": {"type": "string"}},
        "motivo": {"type": "string"},
    },
    "required": ["intencion", "conceptos", "motivo"],
}


def plan(provider: Provider, question: Question) -> Plan:
    """Ask the model how to look. Falls back to vector-only on any doubt."""
    adapter = VertexAdapter(provider)
    prompt = json.dumps(
        {
            "pregunta": question.text,
            "catalogo": queries.catalogue(),
        },
        ensure_ascii=False,
    )

    try:
        raw = adapter.generate_json(
            prompt, system=SYSTEM, schema=SCHEMA, stage="planning"
        )
    except Exception as e:
        # A failed plan must not fail the question: vector search alone is a
        # complete retrieval strategy, just a less targeted one.
        log.warning("planner failed, falling back to vector-only: %s", e)
        return Plan(
            intent="busqueda",
            template_id=None,
            rationale=f"el planificador no respondió ({type(e).__name__})",
            spend=_spend(provider, adapter),
        )

    template_id = (raw.get("template_id") or "").strip() or None
    params = raw.get("parametros") or {}
    concepts = [c.strip() for c in (raw.get("conceptos") or []) if str(c).strip()]

    if template_id is not None:
        template_id, params = _validate(template_id, params, question)

    return Plan(
        intent=str(raw.get("intencion") or "busqueda"),
        template_id=template_id,
        params=params,
        concepts=concepts,
        rationale=str(raw.get("motivo") or ""),
        spend=_spend(provider, adapter),
    )


def _validate(
    template_id: str, params: dict, question: Question
) -> tuple[str | None, dict]:
    """Bind the planner's arguments, or drop the traversal.

    Dropping rather than raising: a question answered from vector search alone
    is worse than one answered with graph context, and far better than an error.
    """
    try:
        template = queries.get(template_id)
    except queries.TemplateError as e:
        log.info("planner chose an unknown template: %s", e)
        return None, {}

    # These are the caller's policy, not the planner's, and they are overwritten
    # rather than validated: a model that asked for `confidence_floor` 0.0 would
    # be asking to answer from relations nobody vetted, and one that named a
    # different `library_id` would be asking to answer this question out of
    # somebody else's corpus. Neither is a request worth honouring, so neither is
    # read.
    for name, value in (
        ("confidence_floor", question.confidence_floor),
        ("library_id", question.library_id),
        # And the sharpest of the three: a library is one shelf inside one
        # organisation; this decides which organisation. A model naming another
        # is asking to answer out of a different customer's corpus.
        ("tenant_id", question.tenant_id),
    ):
        if any(p.name == name for p in template.params):
            params[name] = value

    try:
        return template_id, queries.bind(template, params)
    except queries.TemplateError as e:
        log.info("planner arguments rejected for %s: %s", template_id, e)
        return None, {}


def _spend(provider: Provider, adapter: VertexAdapter) -> Spend:
    from ..activities.ingest import price_for

    usage = adapter.usage
    return Spend(
        stage="planning",
        model=provider.settings.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        usd=price_for(provider.settings.model, usage.input_tokens, usage.output_tokens),
    )
