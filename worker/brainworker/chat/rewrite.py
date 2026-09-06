"""Turning a follow-up into a question the index can be searched with.

"¿Y qué dice ese libro sobre su muerte?" is a perfectly clear thing to say to a
person and a useless thing to embed. Its referents — *ese libro*, *su* — are in
the previous turn, and the retrieval path has no memory: `retrieve.search`
embeds the text it is given and scores chunks against it. So a conversation that
handed follow-ups straight to it would retrieve on the pronouns and answer from
whatever the corpus happens to say about the word "muerte".

This is the whole of the multi-turn machinery. Everything below it — the
topicality gate, the hybrid search, the graph expansion, the effort ladder, the
citation verification — is reached with a **standalone question** and is
therefore exactly the path a one-shot question takes, which is what lets every
measurement recorded against that path keep holding.

Two rules the prompt is built around, both of which are failure modes rather
than preferences:

- **It restates; it never answers.** A model given a conversation and asked for
  "the question" will happily supply the answer instead, and the answer would
  then be embedded and searched for — retrieving whatever chunks resemble a
  claim nobody has verified. `SCHEMA` returning a single field named `pregunta`
  is part of that; so is the instruction not to add facts.
- **It carries referents across, and nothing else.** A rewrite that helpfully
  broadens "¿y su muerte?" into "la muerte y resurrección de Jesucristo en la
  teología cristiana" retrieves a different, larger question than the one that
  was asked.
"""

from __future__ import annotations

import json
import logging

from ..pipeline import Spend
from ..providers import Provider, VertexAdapter
from .types import TurnRecord

log = logging.getLogger(__name__)

STAGE = "chat-rewrite"

SYSTEM = """Reescribes el último mensaje de una conversación como una pregunta \
independiente, que pueda entenderse sin haber leído los turnos anteriores.

REGLAS:
1. NO respondas. Tu salida es una pregunta, nunca una afirmación ni un dato.
2. Sustituye los pronombres y las referencias implícitas ("ese libro", "su \
muerte", "lo anterior") por aquello a lo que se refieren en la conversación.
3. No añadas temas, matices ni términos que el usuario no haya usado. Si el \
mensaje ya es independiente, devuélvelo tal cual.
4. Conserva el idioma, el registro y el alcance del mensaje original. Una \
pregunta concreta debe seguir siendo concreta.
5. Si el mensaje es una instrucción sobre la respuesta anterior ("resume eso", \
"dame tres ejemplos"), conviértelo en una pregunta que nombre el tema del que \
se hablaba.
6. Si no hay nada que sustituir porque la conversación no lo aclara, devuelve \
el mensaje sin cambios. Inventar el referente es peor que no resolverlo."""

SCHEMA = {
    "type": "object",
    "properties": {"pregunta": {"type": "string"}},
    "required": ["pregunta"],
}


def standalone(
    provider: Provider,
    history: list[TurnRecord],
    text: str,
) -> tuple[str, Spend | None]:
    """The message as a question that stands on its own, and what that cost.

    Returns the text unchanged and **no spend at all** when there is no history:
    the first turn of a conversation is already standalone by construction, and
    a call to establish that would be a charge on every new conversation for
    nothing. Only follow-ups pay for this.

    A failure degrades to the original text rather than propagating. The
    consequence of degrading is one badly-retrieved answer; the consequence of
    raising would be a conversation that stops working because a cheap
    preprocessing call was rate-limited. Same reasoning as `service._style`,
    which falls back to the built-in wording when the catalog is unreachable.
    """
    if not history:
        return text, None

    adapter = VertexAdapter(provider)
    prompt = json.dumps(
        {
            "conversacion": [
                {"usuario": t.question, "asistente": t.answer} for t in history
            ],
            "mensaje": text,
        },
        ensure_ascii=False,
    )

    try:
        raw = adapter.generate_json(
            prompt, system=SYSTEM, schema=SCHEMA, stage=STAGE
        )
    except Exception as e:
        log.warning("chat rewrite failed, searching on the raw message: %s", e)
        # Still reported: a failed call can have burned input tokens before it
        # failed, and a stage that spends and reports nothing is how the ledger
        # came to hold $0 for every question ever asked.
        return text, _spend(provider, adapter)

    rewritten = str(raw.get("pregunta") or "").strip()
    if not rewritten:
        log.warning("chat rewrite returned nothing, searching on the raw message")
        rewritten = text
    return rewritten, _spend(provider, adapter)


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
