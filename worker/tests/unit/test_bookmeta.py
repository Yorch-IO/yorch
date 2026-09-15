"""Reading a title and an author off the opening pages.

The rule under test is that **`None` is an answer**. The value goes into the
catalog and the catalog is what a person then edits, so a plausible invention is
harder to notice and correct than a blank — which is why every one of these
asserts what the model is *not* allowed to talk this code into.
"""

from __future__ import annotations

import json

import pytest

from brainworker import bookmeta
from brainworker.providers.gemini import Generation, Usage


class FakeProvider:
    """The double `tests/providers/test_adapter.py` uses, with the settings the
    spend is built from."""

    def __init__(self, payload: object = None, *, raises: Exception | None = None):
        self.text = json.dumps(payload) if payload is not None else "{}"
        self.raises = raises
        self.usage = Usage(120, 18, 0, 1)
        self.calls: list[dict] = []

        class _Settings:
            model = "gemini-3.6-flash"

        self.settings = _Settings()

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None):
        self.calls.append({"prompt": prompt, "system": system, "stage": stage})
        if self.raises:
            raise self.raises
        return Generation(text=self.text, usage=self.usage, finish_reason=None)


def read(payload=None, *, raises=None, text="Comienzo del documento."):
    provider = FakeProvider(payload, raises=raises)
    return provider, bookmeta.read_metadata(provider, text)


def test_a_title_and_an_author_come_back_cleaned():
    _, (title, author, _) = read({"titulo": "  El reto de Dios ", "autor": "Darío Silva-Silva"})
    assert (title, author) == ("El reto de Dios", "Darío Silva-Silva")


def test_a_missing_author_stays_missing():
    """A title page that names no author must come back as no author rather than
    a guess. The prompt says so and this is what holds it to it."""
    _, (title, author, _) = read({"titulo": "Apuntes", "autor": None})
    assert title == "Apuntes" and author is None


@pytest.mark.parametrize("value", ["null", "None", "n/a", "desconocido", "sin autor", "", "  "])
def test_the_words_a_model_writes_instead_of_nothing_are_nothing(value):
    """A model asked for `null` inside a JSON envelope sometimes writes the
    *string* — and a book whose author is "null" would be written into the
    catalog and printed on a cover."""
    _, (_, author, _) = read({"titulo": "Apuntes", "autor": value})
    assert author is None


def test_an_answer_that_does_not_parse_still_reports_what_it_billed():
    """The tokens were spent whether or not the envelope parsed. A stage that
    spends and reports nothing is how the ledger came to hold $0 for every
    question ever asked — and this is the shape that reaches it, because
    `VertexAdapter` books the usage before it tries to decode."""
    provider = FakeProvider()
    provider.text = "lo siento, no puedo"
    title, author, spend = bookmeta.read_metadata(provider, "Comienzo.")
    assert title is None and author is None
    assert spend is not None and spend.input_tokens == 120


def test_a_provider_that_never_answered_reports_no_tokens():
    """The other half, and the distinction is real money: a call that raised
    before the model saw it was not billed, and reporting 120 tokens for it
    would put a charge in the ledger that nobody incurred."""
    _, (title, author, spend) = read(raises=RuntimeError("provider is down"))
    assert title is None and author is None
    assert spend is not None and spend.input_tokens == 0


def test_the_spend_is_filed_under_the_stage_the_ledger_knows():
    from brainworker import stages

    _, (_, _, spend) = read({"titulo": "X", "autor": None})
    assert spend.stage == bookmeta.STAGE
    assert stages.stage_of_cost(spend.stage) == "epub"


def test_only_the_opening_of_the_document_is_sent():
    """The estimate prices exactly `METADATA_CHARS`, so a reader that sent the
    whole book would spend more than the gate quoted — and would answer the same
    question no better, because a title page is at the front."""
    provider, _ = read({"titulo": "X", "autor": None}, text="y" * 50_000)
    sent = provider.calls[0]["prompt"]
    assert len(json.loads(sent)["comienzo"]) == bookmeta.METADATA_CHARS


def test_the_call_is_made_once_and_carries_its_stage():
    provider, _ = read({"titulo": "X", "autor": "Y"})
    assert len(provider.calls) == 1
    assert provider.calls[0]["stage"] == bookmeta.STAGE


def test_an_envelope_missing_its_fields_is_not_a_crash():
    _, (title, author, spend) = read({})
    assert (title, author) == (None, None)
    assert spend is not None
