"""Which of a channel's videos might be about a topic, judged from metadata alone.

Modelled on :mod:`brainworker.bookmeta`, the smallest bounded call in this
codebase, and it keeps that module's rule about `None` being an answer in a
different shape: **`dudoso` is an answer**, and the prompt says so. A title that
does not mention a subject is not evidence that the sermon avoided it, and a
model pushed to choose between "yes" and "no" over a two-line description will
invent a reason for whichever it picks.

Three properties this module has that the prompt cannot give it, because a
prompt is a request and these are checks:

* **Every verdict is matched back to the batch it was sent.** An id the model
  returned that was not sent is dropped and counted; an id that was sent and
  came back with no verdict becomes `sin_evaluar` rather than vanishing. The two
  together mean the result covers exactly the videos that were evaluated, which
  is the property the whole feature's honesty rests on — a candidate the reader
  is offered must come from the list they were told was examined.
* **A malformed row loses its verdict, not its video.** A score outside 0-100 or
  a relevance outside the four words is refused, and the video reads
  `sin_evaluar`. Silently clamping it would make a number nobody produced.
* **Nothing here may ever be shown as a quotation from a sermon.** `razon` is
  the model's guess about a title. The verified quotes come from
  :mod:`~brainworker.channel.topics` and from a citation's own chunk, and those
  two are the only text in this feature that a document actually contains.

Spanish on the wire, like the chunk kinds and a claim's `status`, and for the
same reason: these strings go into an artifact and onto a screen, and renaming
them later would make the records already written unreadable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..pipeline import Spend
from ..providers import Provider, VertexAdapter
from ..youtube import ChannelVideo

log = logging.getLogger(__name__)

STAGE = "channel-preselect"

#: Bumped whenever `SYSTEM` or `SCHEMA` changes in a way that could move a
#: verdict. Recorded in the artifact beside the model id, because a preselection
#: is a measurement and a measurement whose instrument is unrecorded cannot be
#: compared with the next one.
PROMPT_VERSION = "preselect/1"

#: How many videos travel in one call.
#:
#: Not one call per video: a title is a few dozen tokens and the system prompt
#: is a few hundred, so per-video calls would pay the overhead twenty-five times
#: over for the same judgement. Not all hundred at once either — the output is
#: one row per video and a long envelope is the shape that meets the model's
#: output ceiling, which returns `MAX_TOKENS` and no text at all.
BATCH_SIZE = 25

#: How much of a title and a description the model reads. A YouTube description
#: routinely runs to five thousand characters of links, service times and
#: hashtags, none of which says what the sermon argues. The estimate prices
#: exactly these numbers, so the two must not drift — the rule
#: `bookmeta.METADATA_CHARS` states.
TITLE_CHARS = 200
DESCRIPTION_CHARS = 600

#: Output tokens per video: a verdict, a score and one sentence of reason.
#: Measured against nothing yet — stated so, and carried in the estimate where
#: over-reporting is the permitted direction.
OUTPUT_PER_VIDEO = 70

#: System prompt, schema and envelope, per call rather than per video.
CALL_OVERHEAD = 620

#: What a verdict may say. `sin_evaluar` is not one the model may return: it is
#: what the code writes for a video whose verdict did not come back or did not
#: survive checking, and keeping it out of the schema is what stops the model
#: using it as a way of declining to answer.
RELEVANCE = ("relevante", "dudoso", "descartado")
UNEVALUATED = "sin_evaluar"
UNCERTAINTY = ("baja", "media", "alta")

SYSTEM = """Lees el título y la descripción de vídeos de un canal de YouTube y \
dices, para cada uno, qué probabilidad hay de que trate el tema que se te \
indica.

REGLAS:
1. Solo tienes metadatos. No has visto el vídeo ni lo has oído. Juzga la \
probabilidad de que trate el tema, nunca afirmes lo que el vídeo dice.
2. No inventes contenido, doctrina, citas ni posturas. Si la descripción no \
dice nada del tema, eso es lo que sabes.
3. Ante evidencia insuficiente responde `dudoso`. Es la respuesta correcta más \
frecuente y no es una evasiva: un título que no menciona el tema no demuestra \
que el vídeo no lo trate.
4. `relevante` es para cuando el título o la descripción hablan del tema de \
forma explícita. `descartado` es para cuando hablan claramente de otra cosa.
5. `razon` es una frase corta que dice en qué parte del título o de la \
descripción te apoyas. No la adornes y no cites el vídeo: no lo has leído.
6. `puntaje` va de 0 a 100 y acompaña al veredicto, no lo sustituye.
7. Devuelve exactamente un resultado por cada `video_id` que recibas, con el \
mismo identificador. No añadas vídeos que no estén en la lista."""

SCHEMA = {
    "type": "object",
    "properties": {
        "resultados": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "relevancia": {"type": "string", "enum": list(RELEVANCE)},
                    "puntaje": {"type": "integer"},
                    "razon": {"type": "string"},
                    "incertidumbre": {"type": "string", "enum": list(UNCERTAINTY)},
                },
                "required": ["video_id", "relevancia", "puntaje", "razon"],
            },
        }
    },
    "required": ["resultados"],
}


@dataclass(frozen=True)
class Candidate:
    """One video's verdict. `sin_evaluar` means the model did not usably answer."""

    video_id: str
    relevancia: str
    puntaje: int
    razon: str
    incertidumbre: str = "alta"

    @property
    def considered(self) -> bool:
        """Whether this is a video the gate should offer. `dudoso` is included:
        that is the whole point of the topic pass that comes after it."""
        return self.relevancia in ("relevante", "dudoso")


@dataclass
class Preselection:
    """What one preselection asked, and what came back — the whole record.

    Every field is declared rather than set as a loose attribute, because this
    is serialised with `asdict` into the `preselection` artifact and `asdict`
    writes declared fields and nothing else. A field that existed only on the
    instance would be invisible in every record ever written, with no error
    anywhere — which is exactly how `Answer.style_effort` disappeared.
    """

    topic: str
    model: str
    prompt_version: str = PROMPT_VERSION
    #: The ids sent, in the order they were sent. A candidate that is not in
    #: here did not come from this preselection.
    evaluated: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    #: Verdicts naming a video that was never sent. Counted rather than ignored:
    #: a non-zero figure here is the model inventing identifiers, which is worth
    #: knowing before its verdicts are trusted.
    invented: int = 0
    #: Rows refused for a score out of range or a word outside the enum.
    malformed: int = 0
    #: Videos that were sent and came back with nothing usable.
    unevaluated: int = 0

    @property
    def shortlist(self) -> list[Candidate]:
        """The candidates worth reading a transcript for, best first."""
        return sorted(
            (c for c in self.candidates if c.considered),
            key=lambda c: (c.puntaje, c.video_id),
            reverse=True,
        )


def payload_for(topic: str, batch: list[ChannelVideo]) -> str:
    """Exactly what one call sends, so the estimate can price the real string.

    A separate function rather than an expression inside :func:`select` for the
    reason `bookmeta.METADATA_CHARS` is a constant: the estimator calls this too,
    and a quote computed from a different string than the one that is sent is a
    quote for a different call.
    """
    return json.dumps(
        {
            "tema": topic,
            "videos": [
                {
                    "video_id": v.video_id,
                    "titulo": v.title[:TITLE_CHARS],
                    "descripcion": v.description[:DESCRIPTION_CHARS],
                }
                for v in batch
            ],
        },
        ensure_ascii=False,
    )


def batches(videos: list[ChannelVideo], size: int = BATCH_SIZE) -> list[list[ChannelVideo]]:
    return [videos[i : i + size] for i in range(0, len(videos), size)]


def select(
    provider: Provider,
    topic: str,
    videos: list[ChannelVideo],
    *,
    size: int = BATCH_SIZE,
) -> tuple[Preselection, Spend]:
    """Judge every video from its metadata. One call per `size` of them.

    A batch that fails is not a run that fails: its videos come back
    `sin_evaluar` and the rest of the channel is still judged. The spend is
    reported either way, because the tokens were billed whether or not the
    envelope parsed — the rule `bookmeta.read_metadata` records.
    """
    adapter = VertexAdapter(provider)
    result = Preselection(
        topic=topic,
        model=provider.settings.model,
        evaluated=[v.video_id for v in videos],
    )
    verdicts: dict[str, Candidate] = {}

    for batch in batches(videos, size):
        sent = {v.video_id for v in batch}
        try:
            raw = adapter.generate_json(
                payload_for(topic, batch),
                system=SYSTEM,
                schema=SCHEMA,
                stage=STAGE,
            )
        except Exception as e:  # noqa: BLE001 — one bad batch is not a bad channel
            log.warning("preselection batch failed (%d videos): %s", len(batch), e)
            continue
        rows = raw.get("resultados") if isinstance(raw, dict) else None
        for row in rows or []:
            if not isinstance(row, dict):
                result.malformed += 1
                continue
            vid = str(row.get("video_id") or "")
            if vid not in sent:
                # An id from outside the batch. Never trusted, never silently
                # kept: a candidate the reader is offered has to come from the
                # list they were told was examined.
                result.invented += 1
                continue
            candidate = _candidate(vid, row)
            if candidate is None:
                result.malformed += 1
                continue
            verdicts[vid] = candidate

    for vid in result.evaluated:
        result.candidates.append(
            verdicts.get(vid)
            or Candidate(
                video_id=vid,
                relevancia=UNEVALUATED,
                puntaje=0,
                razon="",
                incertidumbre="alta",
            )
        )
    result.unevaluated = sum(
        1 for c in result.candidates if c.relevancia == UNEVALUATED
    )
    return result, _spend(provider, adapter)


def _candidate(vid: str, row: dict) -> Candidate | None:
    """One checked verdict, or None for a row that cannot be believed.

    A score outside the range is refused rather than clamped: clamping would
    write down a number nobody produced, and the video's own `sin_evaluar` says
    the true thing instead.
    """
    relevancia = str(row.get("relevancia") or "")
    if relevancia not in RELEVANCE:
        return None
    try:
        puntaje = int(row.get("puntaje"))
    except (TypeError, ValueError):
        return None
    if not 0 <= puntaje <= 100:
        return None
    incertidumbre = str(row.get("incertidumbre") or "")
    return Candidate(
        video_id=vid,
        relevancia=relevancia,
        puntaje=puntaje,
        razon=" ".join(str(row.get("razon") or "").split()),
        incertidumbre=incertidumbre if incertidumbre in UNCERTAINTY else "media",
    )


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
