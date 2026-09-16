"""What a video actually says about a topic, read from its uncorrected transcript.

This is the pass that separates a hypothesis from evidence, and it exists
because of a property of the transcript pipeline that is easy to miss: **on a
video with captions, the transcript is free.** `probe_video` downloads the
caption track and `group_transcript` builds the paragraph stream without a
single provider call, so by the time a video reaches its gate the words are
already on disk and nobody has been billed for them. Reading them before
deciding whether to pay for correction, embedding and semantics costs one cheap
call and replaces a guess about a title with a quotation from the talk.

Two things about the input that are deliberate:

* **It is the uncorrected stream** — `transcript.txt`, the `transcript_text`
  artifact, before `correct_text` has run. That is what exists at this point in
  the workflow, and it is also the honest thing to read: correction is one of
  the stages being decided on, so a decision that depended on it would have to
  pay for it first. The cost is that an auto-caption transcript spells names the
  way the captioner heard them — this corpus holds *Cuyama* for Fukuyama — so a
  topic keyed on a proper noun can be missed here and found after correction.
  Recorded rather than worked around, because the alternative is paying for
  correction before the gate that decides whether to pay for correction.
* **A ceiling, not a sample.** `TRANSCRIPT_CHARS` exists to bound a pathological
  input, not to read a third of a sermon and guess at the rest. A 76-minute talk
  measures 62,016 characters and fits whole, at about $0.026 — reading only the
  opening would save two cents and answer a different question, since a preacher
  states the subject in the middle as often as at the start. When the ceiling
  does bite, `truncated` says so rather than leaving a partial reading looking
  complete.

And the check that makes it worth anything: **every topic carries a quotation,
and the code looks the quotation up in the transcript.** A topic whose evidence
is not there loses the evidence, not its existence — the same rule, through the
same matcher, as a claim's quote — and `verified` reports how many survived.
A reading nobody can check must not look like one that can.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .. import quoting
from ..pipeline import Spend
from ..providers import Provider, VertexAdapter

log = logging.getLogger(__name__)

STAGE = "channel-topics"

#: Bumped when `SYSTEM` or `SCHEMA` changes in a way that could move a reading.
PROMPT_VERSION = "topics/1"

#: The ceiling on what one call reads. About 3.8 hours of speech at the measured
#: 14.5 characters a second — a bound on the worst case, not a sampling policy.
#: The estimate prices `min(projected, this)`, so the two must not drift.
TRANSCRIPT_CHARS = 200_000

#: System prompt, schema and envelope. Per call, and there is one call a video.
CALL_OVERHEAD = 520

#: Output per video: a handful of topics, each with a quoted sentence, plus the
#: verdict and its reason. **Unmeasured** — reasoned from the schema rather than
#: from a run, and stated as such wherever it reaches a figure.
OUTPUT_PER_VIDEO = 700

CONFIDENCE = ("baja", "media", "alta")

SYSTEM = """Lees la transcripción automática de un vídeo y dices de qué trata en \
relación con un tema que se te indica.

La transcripción la produjo una máquina a partir del audio: no tiene puntuación \
fiable y escribe mal los nombres propios. Léela como habla, no como un texto \
editado.

REGLAS:
1. Solo puedes usar lo que está en la transcripción. No añadas doctrina, \
contexto ni lo que suele decirse sobre el asunto.
2. Cada tema tiene que ir con una `evidencia`: un fragmento **literal** de la \
transcripción, copiado tal cual, que sea donde se dice. No lo arregles, no lo \
puntúes y no lo resumas: se comprueba palabra por palabra contra el texto.
3. Si no encuentras un fragmento literal que sostenga un tema, no pongas ese \
tema. Una lista corta y comprobable vale más que una larga que no lo es.
4. `responde_a_la_consulta` dice si el vídeo trata el tema indicado, no si lo \
menciona de pasada. Una mención suelta es `false` con el motivo escrito.
5. `motivo` es una frase que explica el veredicto. Si el vídeo trata de otra \
cosa, di de qué trata.
6. Responde en el idioma de la transcripción."""

SCHEMA = {
    "type": "object",
    "properties": {
        "temas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tema": {"type": "string"},
                    "evidencia": {"type": "string"},
                    "confianza": {"type": "string", "enum": list(CONFIDENCE)},
                },
                "required": ["tema", "evidencia"],
            },
        },
        "responde_a_la_consulta": {"type": "boolean"},
        "motivo": {"type": "string"},
    },
    "required": ["temas", "responde_a_la_consulta", "motivo"],
}


@dataclass(frozen=True)
class Topic:
    """One thing the video is about, with the sentence that says so.

    `evidencia` is empty when the quotation the model gave is not in the
    transcript. That is a topic a reader must treat as the model's impression
    rather than as something the talk says, and the two have to look different.
    """

    tema: str
    evidencia: str = ""
    confianza: str = "media"

    @property
    def verified(self) -> bool:
        return bool(self.evidencia)


@dataclass
class VideoTopics:
    """What one video's transcript was read to say.

    Every field declared, for the reason `Preselection` gives: this is
    serialised with `asdict` into the `topics` artifact, and `asdict` writes
    declared fields and nothing else.
    """

    video_id: str
    topic: str
    model: str
    prompt_version: str = PROMPT_VERSION
    temas: list[Topic] = field(default_factory=list)
    responde: bool = False
    motivo: str = ""
    #: Characters of transcript the call actually read.
    characters: int = 0
    #: Whether `TRANSCRIPT_CHARS` cut it short. A partial reading must not look
    #: like a complete one.
    truncated: bool = False
    #: Topics whose quotation was found in the transcript, of `len(temas)`.
    #: Reported rather than assumed, because a reading nobody can check must not
    #: look like one that can.
    verified: int = 0
    #: A call that failed. The video keeps its place in the list with nothing
    #: read, which is different from a video that was read and found irrelevant.
    failed: bool = False


def read_topics(
    provider: Provider, topic: str, transcript: str, *, video_id: str
) -> tuple[VideoTopics, Spend]:
    """Read one transcript. One call, and the spend is reported either way."""
    adapter = VertexAdapter(provider)
    excerpt = transcript[:TRANSCRIPT_CHARS]
    out = VideoTopics(
        video_id=video_id,
        topic=topic,
        model=provider.settings.model,
        characters=len(excerpt),
        truncated=len(transcript) > TRANSCRIPT_CHARS,
    )
    try:
        raw = adapter.generate_json(
            json.dumps(
                {"tema": topic, "transcripcion": excerpt}, ensure_ascii=False
            ),
            system=SYSTEM,
            schema=SCHEMA,
            stage=STAGE,
        )
    except Exception as e:  # noqa: BLE001 — one unreadable video is not a bad batch
        log.warning("could not read the topics of %s: %s", video_id, e)
        out.failed = True
        out.motivo = "no se pudo leer la transcripción"
        return out, _spend(provider, adapter)

    if not isinstance(raw, dict):
        out.failed = True
        return out, _spend(provider, adapter)

    out.responde = bool(raw.get("responde_a_la_consulta"))
    out.motivo = " ".join(str(raw.get("motivo") or "").split())
    for row in raw.get("temas") or []:
        if not isinstance(row, dict):
            continue
        tema = " ".join(str(row.get("tema") or "").split())
        if not tema:
            continue
        # The quotation is looked up in the transcript the call was given, not
        # in the whole file: a model cannot have quoted what it was not shown,
        # and checking against the untruncated text would credit it for one.
        found = quoting.verbatim(str(row.get("evidencia") or ""), excerpt)
        confianza = str(row.get("confianza") or "")
        out.temas.append(
            Topic(
                tema=tema,
                evidencia=found or "",
                confianza=confianza if confianza in CONFIDENCE else "media",
            )
        )
    out.verified = sum(1 for t in out.temas if t.verified)
    return out, _spend(provider, adapter)


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
