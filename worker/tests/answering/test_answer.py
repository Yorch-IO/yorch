"""Composing an answer, and refusing to.

These are the tests that matter most in the product. A wrong ingest costs money;
a fabricated citation costs the user's trust in every answer they were given
before it, because there is no way to tell the two apart by looking.
"""

from __future__ import annotations

import json

import pytest

from brainworker.answering import answer as mod
from brainworker.answering.types import Evidence, Question
from brainworker.providers.gemini import Generation, Usage


class FakeProvider:
    """Returns a canned JSON answer and records the prompt it was given."""

    def __init__(self, payload: dict | str) -> None:
        self.payload = payload
        self.prompts: list[str] = []

        class _S:
            model = "gemini-3.6-flash"

        self.settings = _S()

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None):
        self.prompts.append(prompt)
        text = (
            self.payload if isinstance(self.payload, str)
            else json.dumps(self.payload, ensure_ascii=False)
        )
        return Generation(text=text, usage=Usage(500, 120, 40, 1))


def evidence(n: int = 2, *, locator: bool = True) -> list[Evidence]:
    return [
        Evidence(
            chunk_id=f"chk_{i:024d}",
            version_id="ver_" + "a" * 24,
            document_id="doc_" + "a" * 24,
            title="Semana 1",
            breadcrumb="Libro I",
            text=f"Fragmento {i} sobre la felicidad.",
            kind="cuerpo",
            score=0.8,
            locator=f"Semana 1 · [{i}:100]" if locator else "",
        )
        for i in range(n)
    ]


def question() -> Question:
    return Question(library_id="lib_1", text="¿Qué hace feliz a la gente?")


# -- fabricated citations ---------------------------------------------------


def test_a_citation_naming_a_chunk_that_was_never_retrieved_is_dropped():
    """The most dangerous failure this product can have.

    A fabricated citation looks exactly like a grounded one, so it cannot be
    left to a reviewer to notice. Every id the model returns is checked against
    the evidence it was actually given.
    """
    ev = evidence(2)
    provider = FakeProvider({
        "suficiente": True,
        "respuesta": "La felicidad viene de servir a Dios.",
        "citas": [
            {"chunk_id": ev[0].chunk_id, "afirmacion": "viene de servir a Dios"},
            {"chunk_id": "chk_" + "f" * 24, "afirmacion": "inventado"},
        ],
        "motivo": "",
    })

    result = mod.compose(provider, question(), ev)
    assert result.state == "answered"
    assert [c.chunk_id for c in result.citations] == [ev[0].chunk_id]


def test_an_answer_whose_every_citation_was_invented_is_not_an_answer():
    """Downgraded, not returned with a warning. The rest of the answer has no
    better standing than the citations that were meant to support it."""
    ev = evidence(2)
    provider = FakeProvider({
        "suficiente": True,
        "respuesta": "Una respuesta segura de sí misma.",
        "citas": [{"chunk_id": "chk_" + "f" * 24, "afirmacion": "inventado"}],
        "motivo": "",
    })

    result = mod.compose(provider, question(), ev)
    assert result.state == "insufficient_evidence"
    assert "inexistente" in result.reason
    assert result.evidence == ev, "the retrieved chunks are still shown"


def test_an_answer_with_no_citations_at_all_is_refused():
    provider = FakeProvider({
        "suficiente": True, "respuesta": "Sin respaldo.", "citas": [], "motivo": "",
    })
    result = mod.compose(provider, question(), evidence())
    assert result.state == "insufficient_evidence"
    assert not result.grounded


def test_a_citation_whose_chunk_has_no_locator_is_dropped():
    """A citation the user cannot open is not a citation.

    The promise is that every assertion links to somewhere checkable in the
    original source; a chunk whose projection produced no locator cannot honour
    it.
    """
    ev = evidence(1, locator=False)
    provider = FakeProvider({
        "suficiente": True, "respuesta": "Algo.",
        "citas": [{"chunk_id": ev[0].chunk_id, "afirmacion": "algo"}], "motivo": "",
    })
    result = mod.compose(provider, question(), ev)
    assert result.state == "insufficient_evidence"


# -- refusing ---------------------------------------------------------------


def test_the_model_saying_it_cannot_answer_is_honoured():
    """A correct and frequent outcome, not a failure to route around."""
    provider = FakeProvider({
        "suficiente": False, "motivo": "los fragmentos hablan de otra cosa",
    })
    result = mod.compose(provider, question(), evidence())
    assert result.state == "insufficient_evidence"
    assert result.reason == "los fragmentos hablan de otra cosa"
    assert result.text == ""


def test_no_evidence_at_all_refuses_without_calling_the_model():
    provider = FakeProvider({"suficiente": True, "respuesta": "x", "citas": []})
    result = mod.compose(provider, question(), [])
    assert result.state == "insufficient_evidence"
    assert provider.prompts == [], "a question with no evidence must not be billed"


def test_an_unparseable_model_reply_refuses_rather_than_guessing():
    provider = FakeProvider("no soy json")
    result = mod.compose(provider, question(), evidence())
    assert result.state == "insufficient_evidence"
    assert result.evidence, "the retrieved chunks survive the failure"


# -- what reaches the model -------------------------------------------------


def test_the_model_is_given_the_chunk_ids_it_must_cite_by():
    ev = evidence(2)
    provider = FakeProvider({"suficiente": False, "motivo": ""})
    mod.compose(provider, question(), ev)

    sent = json.loads(provider.prompts[0])
    assert [f["chunk_id"] for f in sent["fragmentos"]] == [e.chunk_id for e in ev]
    assert sent["pregunta"] == "¿Qué hace feliz a la gente?"


def test_duplicate_citations_of_the_same_claim_collapse():
    ev = evidence(1)
    provider = FakeProvider({
        "suficiente": True, "respuesta": "x",
        "citas": [
            {"chunk_id": ev[0].chunk_id, "afirmacion": "misma"},
            {"chunk_id": ev[0].chunk_id, "afirmacion": "misma"},
        ],
        "motivo": "",
    })
    result = mod.compose(provider, question(), ev)
    assert len(result.citations) == 1


def test_spend_is_reported_even_when_the_answer_is_refused():
    """The tokens were spent whether or not the answer was usable."""
    provider = FakeProvider({"suficiente": False, "motivo": "nada"})
    result = mod.compose(provider, question(), evidence())
    assert result.spend and result.spend[0].stage == "answering"
    assert result.spend[0].input_tokens == 500
    # `thinking_tokens` is a subset of `output_tokens`, not an addition to it —
    # the 40 reasoning tokens are already inside the 120 billed as output.
    assert result.spend[0].output_tokens == 120


def test_grounded_requires_both_a_state_and_a_citation():
    from brainworker.answering.types import Answer, Citation

    assert not Answer(state="answered").grounded
    assert not Answer(state="insufficient_evidence",
                      citations=[Citation("c", "l", "x")]).grounded
    assert Answer(state="answered", citations=[Citation("c", "l", "x")]).grounded


# -- the prose a person reads -----------------------------------------------


def test_chunk_ids_the_model_wrote_into_the_prose_are_stripped():
    """Observed on a real answer, which is why this exists.

    The prompt asks the model to keep ids out of `respuesta` and it mostly
    complies — but "mostly" is not a property a user-facing string can rest on,
    and the citations are already carried structurally.
    """
    ev = evidence(2)
    a, b = ev[0].chunk_id, ev[1].chunk_id
    provider = FakeProvider({
        "suficiente": True,
        "respuesta": (
            f"La felicidad procede de servir a Dios [{a}, {b}]. "
            f"Quienes no lo conocen alcanzan una felicidad transitoria [{a}]."
        ),
        "citas": [{"chunk_id": a, "afirmacion": "procede de servir a Dios"}],
        "motivo": "",
    })

    result = mod.compose(provider, question(), ev)
    assert result.state == "answered"
    assert "chk_" not in result.text
    assert result.text.startswith("La felicidad procede de servir a Dios.")
    assert result.text.endswith("una felicidad transitoria.")


def test_stripping_ids_does_not_disturb_ordinary_brackets():
    ev = evidence(1)
    provider = FakeProvider({
        "suficiente": True,
        "respuesta": "El autor (Pat Robertson, 1984) lo llama bienaventuranza.",
        "citas": [{"chunk_id": ev[0].chunk_id, "afirmacion": "x"}],
        "motivo": "",
    })
    result = mod.compose(provider, question(), ev)
    assert result.text == "El autor (Pat Robertson, 1984) lo llama bienaventuranza."


# -- claims as hints, never as sources --------------------------------------


def test_a_chunks_claims_reach_the_prompt_labelled_as_readings():
    """A claim is what a model said the chunk says. It goes in because it points
    into a long chunk, and it goes in *named* — the prompt's own rule is that the
    chunk's text wins any disagreement."""
    from brainworker.answering.types import EvidenceClaim

    ev = evidence(1)
    ev[0].claims = [
        EvidenceClaim(text="La felicidad es un hábito.", confidence=0.82,
                      status="niega", quote="no es un hábito", concept="Felicidad"),
    ]
    provider = FakeProvider({
        "suficiente": True, "respuesta": "Lo rechaza.",
        "citas": [{"chunk_id": ev[0].chunk_id, "afirmacion": "Lo rechaza."}],
        "motivo": "",
    })
    result = mod.compose(provider, question(), ev)

    assert result.state == "answered"
    sent = json.loads(provider.prompts[0])["fragmentos"][0]
    assert sent["lecturas"] == [{
        "afirmacion": "La felicidad es un hábito.",
        "estado": "niega",
        "confianza": 0.82,
        "cita": "no es un hábito",
        "concepto": "Felicidad",
    }]
    assert "lecturas" in mod.SYSTEM and "manda el `texto`" in mod.SYSTEM


def test_a_chunk_with_no_claims_sends_no_key_for_them():
    """This prompt already competes for the attention that keeps citations
    accurate. An always-present, usually-empty key spends it for nothing."""
    provider = FakeProvider({
        "suficiente": True, "respuesta": "Sí.",
        "citas": [{"chunk_id": f"chk_{0:024d}", "afirmacion": "Sí."}],
        "motivo": "",
    })
    mod.compose(provider, question(), evidence(2))
    for fragment in json.loads(provider.prompts[0])["fragmentos"]:
        assert "lecturas" not in fragment
