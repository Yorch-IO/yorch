"""Turning evidence into a cited answer, or refusing to.

Two rules make this module worth having rather than being a prompt:

**No answer is returned without at least one citation the code verified.** The
model is asked to cite by chunk id, and every id it returns is checked against
the evidence it was actually given. A citation naming a chunk that was not in
the prompt is a fabrication — the most dangerous failure this product can have,
because it looks exactly like a grounded answer — so it is dropped, and an
answer left with no surviving citations is downgraded to insufficient evidence.

**Refusing is a first-class outcome, not an error path.** `insufficient_evidence`
comes back with the retrieved chunks and an explanation, because "no answer"
with nothing to look at is indistinguishable from a broken index.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from ..pipeline import Spend
from ..providers import Provider, VertexAdapter
from .effort import budget_for, compose_system
from .jsonstream import FieldStreamer
from .types import Answer, Citation, Evidence, Plan, Question

log = logging.getLogger(__name__)

SYSTEM = """Respondes preguntas usando EXCLUSIVAMENTE los fragmentos que se te dan.

Reglas, en orden de importancia:

1. No usas conocimiento general. Si los fragmentos no contienen la respuesta,
   `suficiente` es falso. Esa es una respuesta correcta y frecuente, no un fallo.
2. Cada afirmación de tu respuesta va acompañada de la cita del fragmento del que
   sale, por su `chunk_id`. Solo puedes citar los identificadores que aparecen en
   los fragmentos recibidos.
3. Las citas van SOLO en el campo `citas`. El texto de `respuesta` es prosa que
   va a leer una persona: no escribas identificadores dentro de él, ni entre
   corchetes ni de ninguna otra forma. La interfaz enlaza las citas por su
   cuenta.
4. No completas huecos, no infieres más allá de lo que el texto dice y no
   suavizas una contradicción entre fragmentos: si dicen cosas distintas, lo
   señalas.
5. Algunos fragmentos traen `lecturas`: afirmaciones que otro modelo extrajo de
   ese mismo fragmento. NO son el documento. Te sirven para orientarte dentro de
   un fragmento largo, y ante cualquier discrepancia manda el `texto` del
   fragmento. No citas una lectura, citas el fragmento. Una lectura cuyo
   `estado` no es `afirma` describe algo que el documento rechaza (`niega`) o
   atribuye a otro (`atribuido`); presentarla como lo que el documento sostiene
   es el peor error que puedes cometer aquí. `sin_estado` significa que no se
   sabe, no que lo afirme.
6. Respondes en el idioma de la pregunta, con la terminología del documento."""

SCHEMA = {
    "type": "object",
    "properties": {
        "suficiente": {"type": "boolean"},
        "respuesta": {"type": "string"},
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
        "motivo": {"type": "string"},
    },
    "required": ["suficiente", "motivo"],
}


def _thinking_override(provider: Provider, question: Question) -> int | None:
    """The effort level's reasoning budget, unless an operator has spoken.

    Configuration wins over a per-question level, in both of the two ways it can
    be expressed, and the second is the one that is easy to miss:

    * ``BRAIN_THINKING_ANSWERING`` puts ``"answering"`` in ``stage_thinking``.
      Somebody naming this stage explicitly means this stage.
    * ``BRAIN_THINKING_BUDGET`` sets the global fallback. ``answering`` is
      deliberately *absent* from the stage map so that a global reaches it —
      `config.py` says so in as many words: "someone turning off every reasoning
      cost means it." A guard that checked only the per-stage map would let
      ``thorough`` spend against an operator who had globally set zero, which is
      exactly the promise that comment makes.

    So the level's override applies only when neither is set — that is, only when
    the resolved budget would have been the model's own default anyway.
    """
    gemini = provider.settings
    if "answering" in gemini.stage_thinking or gemini.thinking_budget is not None:
        return None
    return budget_for(question.effort).thinking_override


def compose(
    provider: Provider,
    question: Question,
    evidence: list[Evidence],
    plan: Plan | None = None,
    style: str = "",
    on_delta: Callable[[str], None] | None = None,
) -> Answer:
    """Compose an answer, or refuse.

    `on_delta` makes this stream: it receives the answer's prose as the model
    writes it, decoded out of the JSON envelope by `FieldStreamer`. It changes
    nothing else. **Every branch below the call is shared**, which is the reason
    it is a parameter rather than a second function — `suficiente`, `_verify`,
    `_clean` and all four refusals have exactly one implementation, so a streamed
    answer and a whole one carry the same guarantees.

    The consequence a caller has to handle: `citas` is the *last* field in
    `SCHEMA`, so verification cannot run until the envelope closes. A turn can
    therefore stream fluent prose and still come back
    `insufficient_evidence` — because the model cited nothing, or cited chunks it
    was never shown. What streamed is a draft; the returned `Answer` is the
    document, and a caller showing the draft must replace it rather than keep it.
    """
    if not evidence:
        return Answer(
            state="insufficient_evidence",
            reason="la búsqueda no devolvió ningún fragmento de esta biblioteca",
            plan=plan,
            effort=question.effort,
        )

    adapter = VertexAdapter(provider)
    prompt = _prompt(question, evidence)

    call = dict(
        system=compose_system(SYSTEM, style),
        schema=SCHEMA,
        stage="answering",
        thinking_budget=_thinking_override(provider, question),
    )
    try:
        if on_delta is None:
            raw = adapter.generate_json(prompt, **call)
        else:
            raw = _streamed(adapter, prompt, call, on_delta)
    except Exception as e:
        log.warning("answer generation failed: %s", e)
        return Answer(
            state="insufficient_evidence",
            reason=f"el modelo no pudo componer una respuesta ({type(e).__name__})",
            evidence=evidence,
            plan=plan,
            spend=[_spend(provider, adapter)],
            effort=question.effort,
        )

    spend = [_spend(provider, adapter)]

    if not raw.get("suficiente"):
        return Answer(
            state="insufficient_evidence",
            reason=str(raw.get("motivo") or "los fragmentos no contienen la respuesta"),
            evidence=evidence,
            plan=plan,
            spend=spend,
            effort=question.effort,
        )

    citations, invented = _verify(raw.get("citas") or [], evidence)
    if invented:
        # Not a warning to be logged and moved past. A model citing chunks it was
        # never shown is fabricating provenance, and the rest of its answer has
        # no better standing than those citations did.
        log.warning(
            "answer cited %d chunk id(s) that were never retrieved: %s",
            len(invented), invented,
        )

    if not citations:
        return Answer(
            state="insufficient_evidence",
            reason=(
                "el modelo afirmó tener respuesta pero no la respaldó con ninguna "
                "cita verificable"
                + (f" (citó {len(invented)} fragmento(s) inexistente(s))" if invented else "")
            ),
            evidence=evidence,
            plan=plan,
            spend=spend,
            effort=question.effort,
        )

    return Answer(
        state="answered",
        text=_clean(str(raw.get("respuesta") or "")),
        citations=citations,
        evidence=evidence,
        plan=plan,
        spend=spend,
        effort=question.effort,
    )


def _streamed(
    adapter: VertexAdapter,
    prompt: str,
    call: dict,
    on_delta: Callable[[str], None],
) -> dict:
    """The same call, with `respuesta` relayed as it is decoded.

    The envelope arrives as JSON, so what reaches `on_delta` here is the prose
    of one field and nothing else — no braces, no field names, no escapes.

    `finish()` matters on the failure path as much as the happy one: a stream cut
    short by `MAX_TOKENS` never closes the string, and the characters already
    decoded are the best draft there is. Withholding them because a closing quote
    never arrived would lose the visible answer to a truncation that
    `json.loads` is about to complain about anyway.
    """
    streamer = FieldStreamer("respuesta")

    def relay(piece: str) -> None:
        shown = streamer.feed(piece)
        if shown:
            on_delta(shown)

    try:
        return adapter.generate_json_stream(prompt, on_delta=relay, **call)
    finally:
        tail = streamer.finish()
        if tail:
            on_delta(tail)


_INLINE_ID = re.compile(
    r"\s*[\[\(]\s*(?:chk_[0-9a-f]{24})(?:\s*,\s*chk_[0-9a-f]{24})*\s*[\]\)]"
)
_BARE_ID = re.compile(r"\s*\bchk_[0-9a-f]{24}\b")


def _clean(text: str) -> str:
    """Strip chunk ids the model wrote into the prose.

    The prompt asks it not to, and it mostly complies — but "mostly" is not a
    property a user-facing string can rest on, and the citations are already
    carried structurally. Observed on a real answer: "…entregarse a Dios
    [chk_2bf13bd98fd6091aeb6e9ce2, chk_affd611f226cb471abc8eb1f]."

    Punctuation is repaired after the removal, because the ids usually sit
    between the last word and the full stop.
    """
    cleaned = _INLINE_ID.sub("", text)
    cleaned = _BARE_ID.sub("", cleaned)
    cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def _prompt(question: Question, evidence: list[Evidence]) -> str:
    import json

    return json.dumps(
        {
            "pregunta": question.text,
            "fragmentos": [
                {
                    "chunk_id": e.chunk_id,
                    "documento": e.title,
                    "seccion": e.breadcrumb,
                    "texto": e.text,
                    # Omitted entirely when there are none, rather than sent as an
                    # empty list: a key that is sometimes absent is cheaper to
                    # read than one that is usually empty, and this prompt is
                    # already competing for the attention that keeps citations
                    # accurate.
                    **({"lecturas": [
                        {
                            "afirmacion": c.text,
                            "estado": c.status,
                            "confianza": round(c.confidence, 2),
                            **({"cita": c.quote} if c.quote else {}),
                            **({"concepto": c.concept} if c.concept else {}),
                        }
                        for c in e.claims
                    ]} if e.claims else {}),
                }
                for e in evidence
            ],
        },
        ensure_ascii=False,
    )


def _verify(
    claimed: list[dict], evidence: list[Evidence]
) -> tuple[list[Citation], list[str]]:
    """Keep only citations naming a chunk that was actually retrieved.

    Also drops any chunk with no locator: a citation the user cannot open is not
    a citation, and the promise this product makes is that every assertion links
    to somewhere checkable in the original source.
    """
    by_id = {e.chunk_id: e for e in evidence}
    kept: list[Citation] = []
    invented: list[str] = []
    seen: set[tuple[str, str]] = set()

    for item in claimed:
        chunk_id = str(item.get("chunk_id") or "")
        claim = str(item.get("afirmacion") or "").strip()
        source = by_id.get(chunk_id)
        if source is None:
            invented.append(chunk_id)
            continue
        if not source.locator:
            continue
        key = (chunk_id, claim)
        if key in seen:
            continue
        seen.add(key)
        kept.append(
            Citation(
                chunk_id=chunk_id,
                locator=source.locator,
                claim=claim,
                page=source.page,
                section_title=source.breadcrumb or None,
            )
        )
    return kept, invented


def _spend(provider: Provider, adapter: VertexAdapter) -> Spend:
    from ..activities.ingest import price_for

    usage = adapter.usage
    return Spend(
        stage="answering",
        model=provider.settings.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        usd=price_for(provider.settings.model, usage.input_tokens, usage.output_tokens),
    )
