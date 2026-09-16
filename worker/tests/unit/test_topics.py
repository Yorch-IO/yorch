"""Reading a transcript for what it is about, and checking that it said so.

The one test that matters is the last group: a topic whose quotation is not in
the transcript loses the quotation, keeps its existence, and is counted apart.
A reading nobody can check must not look like one that can — the rule the graph's
`claims_verified` already stands on, applied to the pass that runs before the
money.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from brainworker.channel import topics
from brainworker.providers.gemini import Generation, Usage

TRANSCRIPT = (
    "bueno hermanos hoy vamos a hablar de la justicia social\n\n"
    "porque el profeta dice que hagamos justicia y amemos misericordia\n\n"
    "y eso no es un asunto político es un asunto del corazón\n"
)


class FakeProvider:
    def __init__(self, payload: object = None, *, raises=None):
        self.payload = payload
        self.raises = raises
        self.usage = Usage(5000, 400, 0, 1)
        self.calls: list[dict] = []

        class _Settings:
            model = "gemini-3.6-flash"

        self.settings = _Settings()

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None):
        self.calls.append({"prompt": prompt, "stage": stage})
        if self.raises:
            raise self.raises
        return Generation(
            text=json.dumps(self.payload if self.payload is not None else {}),
            usage=self.usage,
            finish_reason=None,
        )


def _read(payload, transcript=TRANSCRIPT, *, raises=None):
    provider = FakeProvider(payload, raises=raises)
    reading, spend = topics.read_topics(
        provider, "justicia social", transcript, video_id="aaaaaaaaaaa"
    )
    return provider, reading, spend


def _payload(temas, responde=True, motivo="habla de ello"):
    return {"temas": temas, "responde_a_la_consulta": responde, "motivo": motivo}


# --- the check ---------------------------------------------------------------


def test_a_quotation_that_is_in_the_transcript_survives():
    _, reading, _ = _read(
        _payload([{"tema": "justicia", "evidencia": "hagamos justicia y amemos misericordia"}])
    )
    assert reading.verified == 1
    assert reading.temas[0].evidencia == "hagamos justicia y amemos misericordia"
    assert reading.temas[0].verified


def test_a_quotation_that_is_not_there_loses_its_evidence_not_its_topic():
    # The claim rule, in the same shape: what it costs is the sentence you could
    # check, not the reading itself.
    _, reading, _ = _read(
        _payload([{"tema": "escatología", "evidencia": "el señor viene pronto"}])
    )
    assert len(reading.temas) == 1
    assert reading.temas[0].evidencia == ""
    assert reading.temas[0].verified is False
    assert reading.verified == 0


def test_whitespace_is_tolerated_and_nothing_else_is():
    # The rule `quoting.find` states, shared with the claim checker so the two
    # cannot drift: a model reproduces the words and not the line breaks.
    _, ok, _ = _read(
        _payload([{"tema": "t", "evidencia": "justicia social\n   porque el profeta"}])
    )
    assert ok.verified == 1

    # A fixed accent is a quote the transcript does not contain.
    _, bad, _ = _read(
        _payload([{"tema": "t", "evidencia": "es un asunto politico"}])
    )
    assert bad.verified == 0


def test_the_stored_quotation_is_the_transcripts_spelling_not_the_models():
    _, reading, _ = _read(
        _payload([{"tema": "t", "evidencia": "justicia social  porque el profeta"}])
    )
    assert reading.temas[0].evidencia == "justicia social\n\nporque el profeta"


def test_a_model_cannot_be_credited_for_quoting_what_it_was_not_shown():
    # The lookup runs against the excerpt the call was given, not the whole
    # file, so a quotation from beyond the ceiling fails rather than passing.
    long = "x" * topics.TRANSCRIPT_CHARS + "\n\nla parte que nadie leyó"
    _, reading, _ = _read(
        _payload([{"tema": "t", "evidencia": "la parte que nadie leyó"}]), long
    )
    assert reading.truncated is True
    assert reading.verified == 0


def test_verified_counts_only_what_survived():
    _, reading, _ = _read(
        _payload(
            [
                {"tema": "a", "evidencia": "hagamos justicia"},
                {"tema": "b", "evidencia": "esto no está en el texto"},
                {"tema": "c", "evidencia": "es un asunto del corazón"},
            ]
        )
    )
    assert (len(reading.temas), reading.verified) == (3, 2)


# --- shape and failure -------------------------------------------------------


def test_a_topic_with_no_name_is_dropped():
    _, reading, _ = _read(_payload([{"tema": "  ", "evidencia": "hagamos justicia"}]))
    assert reading.temas == []


@pytest.mark.parametrize("value,expected", [("alta", "alta"), ("", "media"), ("x", "media")])
def test_an_unknown_confidence_falls_back_rather_than_travelling(value, expected):
    _, reading, _ = _read(
        _payload([{"tema": "t", "evidencia": "hagamos justicia", "confianza": value}])
    )
    assert reading.temas[0].confianza == expected


def test_a_failed_call_is_one_unread_video_and_still_reports_its_spend():
    _, reading, spend = _read(None, raises=RuntimeError("provider said no"))
    assert reading.failed is True
    assert reading.temas == []
    assert reading.responde is False
    assert spend.stage == topics.STAGE


def test_a_failed_reading_is_not_a_video_read_and_found_irrelevant():
    # Two different facts: one was read and does not discuss the topic, the
    # other was never read. `failed` is what keeps them apart on screen.
    _, failed, _ = _read(None, raises=RuntimeError("no"))
    _, read, _ = _read(_payload([], responde=False, motivo="habla de finanzas"))
    assert failed.failed and not read.failed
    assert read.motivo == "habla de finanzas"


def test_the_ceiling_is_reported_rather_than_silent():
    long = "y" * (topics.TRANSCRIPT_CHARS + 10)
    _, reading, _ = _read(_payload([]), long)
    assert reading.truncated is True
    assert reading.characters == topics.TRANSCRIPT_CHARS

    _, short, _ = _read(_payload([]), TRANSCRIPT)
    assert short.truncated is False
    assert short.characters == len(TRANSCRIPT)


def test_the_reading_survives_serialisation():
    _, reading, _ = _read(
        _payload([{"tema": "justicia", "evidencia": "hagamos justicia"}])
    )
    dumped = asdict(reading)
    assert dumped["video_id"] == "aaaaaaaaaaa"
    assert dumped["prompt_version"] == topics.PROMPT_VERSION
    assert dumped["temas"][0]["evidencia"] == "hagamos justicia"
    assert set(dumped) >= {
        "video_id", "topic", "model", "prompt_version", "temas", "responde",
        "motivo", "characters", "truncated", "verified", "failed",
    }
