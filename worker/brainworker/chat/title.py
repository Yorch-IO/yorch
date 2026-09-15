"""Naming a conversation from its first exchange.

A list of conversations all called "Conversación" is a list nobody can use, and
the first question is a poor title on its own — "¿Quién fue Jesucristo?" is
fine, but "¿y eso dónde aparece?" is not, and neither is a question three lines
long.

Deliberately once per conversation, after the first turn, and never again. A
title that drifted as a conversation went on would move rows around in a list
somebody is reading, and the first exchange is what a person remembers a
conversation by.
"""

from __future__ import annotations

import json
import logging

from ..pipeline import Spend
from ..providers import Provider, VertexAdapter
from .types import FALLBACK_TITLE_CHARS

log = logging.getLogger(__name__)

STAGE = "chat-title"

SYSTEM = """Pones título a una conversación a partir de su primer intercambio.

REGLAS:
1. Entre tres y seis palabras. Sin comillas, sin punto final.
2. Nombra el tema, no la acción: "La divinidad de Cristo", no "Pregunta sobre \
la divinidad de Cristo".
3. Usa el idioma de la conversación.
4. Si el intercambio no tiene un tema claro, usa las palabras del propio \
usuario en vez de inventar uno."""

SCHEMA = {
    "type": "object",
    "properties": {"titulo": {"type": "string"}},
    "required": ["titulo"],
}


def fallback(question: str) -> str:
    """What a conversation is called until — and if — the model names it.

    Written the moment the conversation is created, so a row in the list is
    never blank and never says "Sin título": the first question truncated is a
    worse title than a generated one and a much better one than nothing.
    """
    text = " ".join(question.split())
    if len(text) <= FALLBACK_TITLE_CHARS:
        return text
    return text[: FALLBACK_TITLE_CHARS - 1].rstrip() + "…"


def title_for(
    provider: Provider,
    question: str,
    answer: str,
) -> tuple[str | None, Spend | None]:
    """A name for the conversation, or `None` to keep the one it has.

    `None` rather than the fallback, so a caller can tell "the model declined or
    failed" from "the model produced this" — the first must leave the existing
    title alone rather than overwrite a good one with a truncation.
    """
    adapter = VertexAdapter(provider)
    prompt = json.dumps(
        {"usuario": question, "asistente": answer}, ensure_ascii=False
    )
    try:
        raw = adapter.generate_json(
            prompt, system=SYSTEM, schema=SCHEMA, stage=STAGE
        )
    except Exception as e:
        log.warning("chat title generation failed, keeping the fallback: %s", e)
        return None, _spend(provider, adapter)

    title = " ".join(str(raw.get("titulo") or "").split()).strip(' "\'.')
    if not title:
        return None, _spend(provider, adapter)
    return fallback(title), _spend(provider, adapter)


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
