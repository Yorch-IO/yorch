"""The comparative reading, and the two things that keep it honest.

A finding with no surviving citation is **moved to `limitaciones`**, never
deleted — deleting would leave an answer that looks complete and is quietly
shorter, and the reader would never know a claim had been made and dropped. And
interpretation is a field of its own, so what a reader must treat sceptically
never shares a paragraph with what they may rely on.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from brainworker.answering.types import Evidence, Question
from brainworker.channel import synthesis
from brainworker.providers.gemini import Generation, Usage


class FakeProvider:
    def __init__(self, payload=None, *, raises=None):
        self.payload = payload
        self.raises = raises
        self.usage = Usage(9000, 1200, 0, 1)
        self.calls: list[dict] = []

        class _Settings:
            model = "gemini-3.6-flash"

        self.settings = _Settings()

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None):
        self.calls.append(
            {"prompt": prompt, "system": system, "stage": stage,
             "max_output_tokens": max_output_tokens}
        )
        if self.raises:
            raise self.raises
        return Generation(
            text=json.dumps(self.payload or {}), usage=self.usage, finish_reason=None
        )


def _evidence(chunk_id: str, *, locator: str = "1:16:13 · https://youtu.be/x?t=4573"):
    return Evidence(
        chunk_id=chunk_id,
        document_id="doc",
        version_id="ver",
        title="Prédica",
        breadcrumb="",
        text="hagamos justicia y amemos misericordia",
        kind="transcripcion",
        score=0.71,
        source="vector",
        locator=locator,
    )


def _question(text: str = "¿qué se predica sobre la justicia social?") -> Question:
    return Question(library_id="lib_yt_UC", text=text, effort="thorough")


def _payload(**over):
    body = {
        "hallazgos": [{"afirmacion": "Se predica la justicia", "chunk_ids": ["chk_1"]}],
        "comparacion": {"convergencias": ["a"], "diferencias": [], "matices": []},
        "interpretacion_teologica": [
            {"inferencia": "Hay un énfasis profético", "alcance": "estas dos prédicas",
             "limites": "no permite hablar del canal entero"}
        ],
        "citas": [{"chunk_id": "chk_1", "afirmacion": "Se predica la justicia"}],
        "limitaciones": ["solo dos prédicas"],
    }
    body.update(over)
    return body


def _compose(payload, evidence=None, *, raises=None):
    provider = FakeProvider(payload, raises=raises)
    return provider, synthesis.compose(
        provider, _question(), evidence if evidence is not None else [_evidence("chk_1")]
    )


# --- the answer ---------------------------------------------------------------


def test_a_supported_finding_survives_with_its_citation():
    _, out = _compose(_payload())
    assert out.state == "answered"
    assert out.hallazgos[0].afirmacion == "Se predica la justicia"
    assert out.hallazgos[0].chunk_ids == ["chk_1"]
    assert out.citas[0].locator.startswith("1:16:13")
    assert out.demoted == 0


def test_interpretation_is_a_field_of_its_own():
    # The thing a reader must treat sceptically must never share a paragraph
    # with the thing they may rely on.
    _, out = _compose(_payload())
    assert out.interpretacion_teologica[0].inferencia == "Hay un énfasis profético"
    assert out.interpretacion_teologica[0].alcance
    assert out.interpretacion_teologica[0].limites
    assert "énfasis profético" not in " ".join(h.afirmacion for h in out.hallazgos)


# --- the checks the prompt cannot make ----------------------------------------


def test_a_finding_with_no_surviving_citation_is_demoted_not_deleted():
    _, out = _compose(
        _payload(
            hallazgos=[
                {"afirmacion": "Se predica la justicia", "chunk_ids": ["chk_1"]},
                {"afirmacion": "Se condena la usura", "chunk_ids": ["chk_ausente"]},
            ]
        )
    )
    assert [h.afirmacion for h in out.hallazgos] == ["Se predica la justicia"]
    assert out.demoted == 1
    assert any("Se condena la usura" in x for x in out.limitaciones)


def test_an_invented_chunk_id_is_dropped_and_counted():
    _, out = _compose(
        _payload(
            citas=[
                {"chunk_id": "chk_1", "afirmacion": "a"},
                {"chunk_id": "chk_inventado", "afirmacion": "b"},
            ]
        )
    )
    assert [c.chunk_id for c in out.citas] == ["chk_1"]
    assert out.invented == 1


def test_a_citation_with_no_locator_is_not_a_citation():
    # A citation the reader cannot open is not a citation, and the promise this
    # product makes is that every assertion links somewhere checkable.
    _, out = _compose(_payload(), [_evidence("chk_1", locator="")])
    assert out.citas == []
    assert out.state == "insufficient_evidence"


def test_nothing_supported_is_a_refusal_with_a_reason():
    # "No answer" with nothing to look at is indistinguishable from a broken
    # index, so the reason is the only thing that says which refusal this was.
    _, out = _compose(_payload(hallazgos=[], citas=[]))
    assert out.state == "insufficient_evidence"
    assert "cita comprobable" in out.reason


def test_a_refusal_prints_no_interpretation():
    # An interpretation resting on nothing checkable is the model's own reading
    # of a corpus it may not have read.
    _, out = _compose(_payload(hallazgos=[{"afirmacion": "x", "chunk_ids": ["nope"]}]))
    assert out.state == "insufficient_evidence"
    assert out.interpretacion_teologica == []


def test_no_evidence_at_all_refuses_before_spending():
    provider = FakeProvider(_payload())
    out = synthesis.compose(provider, _question(), [])
    assert out.state == "insufficient_evidence"
    assert provider.calls == []


def test_a_truncated_answer_is_told_apart_from_a_thin_corpus():
    from brainworker.providers.adapter import TruncatedResponse

    _, out = _compose(None, raises=TruncatedResponse("out of room", output_tokens=65521))
    assert out.state == "insufficient_evidence"
    assert "65521" in out.reason
    assert out.spend[0].stage == synthesis.STAGE


def test_a_failed_call_still_reports_what_it_billed():
    _, out = _compose(None, raises=RuntimeError("provider said no"))
    assert out.state == "insufficient_evidence"
    assert out.spend and out.spend[0].stage == synthesis.STAGE


# --- the prompt ---------------------------------------------------------------


def test_the_six_answering_rules_come_first_and_neutrality_below_them():
    # `compose_system` writes the sentence that says the rules win, which is the
    # same arrangement a per-organisation answer style gets.
    provider, _ = _compose(_payload())
    system = provider.calls[0]["system"]
    from brainworker.answering import answer as answer_mod

    assert system.startswith(answer_mod.SYSTEM)
    assert synthesis.NEUTRALITY in system
    assert system.index(answer_mod.SYSTEM) < system.index(synthesis.NEUTRALITY)
    assert "mandan las reglas" in system


def test_the_call_carries_an_output_ceiling():
    # It bounds the bill, not the reasoning: a call with dynamic thinking can
    # spend the whole output allowance reasoning and return no text, measured
    # once at $0.497373 for nothing.
    provider, _ = _compose(_payload())
    assert provider.calls[0]["max_output_tokens"] == synthesis.MAX_OUTPUT_TOKENS


def test_the_prompt_is_the_answering_prompt_unchanged():
    # Reusing it is what keeps a fragment's `lecturas` carrying their `estado`,
    # which is the rule that stops a claim the document *rejects* being read as
    # one it holds.
    from brainworker.answering import answer as answer_mod

    provider, _ = _compose(_payload())
    assert provider.calls[0]["prompt"] == answer_mod._prompt(
        _question(), [_evidence("chk_1")]
    )


# --- serialisation ------------------------------------------------------------


def test_the_synthesis_survives_serialisation():
    # Through `asdict`, not on the object: reading the attribute directly is
    # exactly what did not catch `Answer.style_effort` going missing from every
    # response with no error anywhere.
    _, out = _compose(_payload())
    dumped = asdict(out)
    assert set(dumped) >= {
        "state", "topic", "model", "prompt_version", "hallazgos", "comparacion",
        "interpretacion_teologica", "citas", "limitaciones", "evidence",
        "demoted", "invented", "reason", "spend",
    }
    assert dumped["hallazgos"][0]["chunk_ids"] == ["chk_1"]
    assert dumped["comparacion"]["convergencias"] == ["a"]
    assert dumped["interpretacion_teologica"][0]["limites"]
    assert dumped["citas"][0]["locator"].startswith("1:16:13")
    assert dumped["prompt_version"] == synthesis.PROMPT_VERSION


def test_a_timed_locator_is_what_reaches_the_reader():
    # The whole promise: every finding opens at the minute it was said.
    _, out = _compose(_payload())
    assert "?t=4573" in out.citas[0].locator
