"""The adapter that lets the engine's correction pass run on ADC."""

from __future__ import annotations

import json

import pytest

from brainworker.providers import VertexAdapter
from brainworker.providers.gemini import Generation, Usage


class FakeProvider:
    def __init__(self, text: str = "{}") -> None:
        self.text = text
        self.calls: list[dict] = []

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None):
        self.calls.append(
            {"prompt": prompt, "system": system, "temperature": temperature,
             "schema": response_schema}
        )
        return Generation(text=self.text, usage=Usage(10, 5, 0, 1))


def test_it_presents_the_signature_the_engine_calls():
    """`correct_paragraphs` calls exactly one method with these keywords. If the
    engine's signature moves, this fails here rather than mid-correction."""
    import inspect

    from docagent.vertex import Vertex

    engine = inspect.signature(Vertex.generate).parameters
    adapter = inspect.signature(VertexAdapter.generate).parameters
    assert set(engine) <= set(adapter)


def test_usage_accumulates_across_every_batch():
    """Correction is many calls; the charge recorded must be their sum.

    `Usage` is positional as (input, output, thinking, calls) — thinking sits in
    the middle because it is a *subset* of output, not an addition to it.
    """
    adapter = VertexAdapter(FakeProvider())
    for _ in range(3):
        adapter.generate("x", stage="correct")
    assert adapter.usage.input_tokens == 30
    assert adapter.usage.output_tokens == 15
    assert adapter.usage.calls == 3


def test_a_json_schema_is_passed_through_as_a_response_schema():
    provider = FakeProvider(text='{"parrafos": []}')
    adapter = VertexAdapter(provider)
    adapter.generate("x", json_schema={"type": "object"}, temperature=0.0)
    assert provider.calls[0]["schema"] == {"type": "object"}
    assert provider.calls[0]["temperature"] == 0.0


def test_invalid_json_is_reported_on_the_call_that_produced_it():
    """Rather than as a parse failure in a later stage with no indication of
    which input caused it."""
    adapter = VertexAdapter(FakeProvider(text="no soy json"))
    with pytest.raises(ValueError, match="invalid JSON"):
        adapter.generate_json("x", system="s", schema={"type": "object"})


def test_image_input_is_refused_rather_than_silently_dropped():
    """OCR is a paid stage the plan gates behind its own confirmation. Dropping
    the image would correct a blank page and report success."""
    adapter = VertexAdapter(FakeProvider())
    with pytest.raises(NotImplementedError, match="OCR"):
        adapter.generate("x", image_png=b"\x89PNG")


def test_embedding_is_not_reachable_through_the_adapter():
    """`Provider.embed` checks batch length and vector width. A second path to
    embedding here would let both checks be bypassed."""
    adapter = VertexAdapter(FakeProvider())
    assert not hasattr(adapter, "embed")
    assert not hasattr(adapter, "embed_many")


def test_it_drives_the_engines_correction_end_to_end():
    """The integration that matters: the engine's own function, its verification
    gate, and this adapter, with no network."""
    from docagent import correct

    original = "El fin principal del hombre es glorificar a Dios, segun Gén. 1:26."
    fixed = "El fin principal del hombre es glorificar a Dios, según Gén. 1:26."

    class Correcting(FakeProvider):
        def generate(self, prompt, **kw):
            payload = json.loads(prompt)
            out = [{"i": p["i"], "texto": fixed} for p in payload["parrafos"]]
            return Generation(
                text=json.dumps({"parrafos": out}, ensure_ascii=False),
                usage=Usage(10, 5, 0, 1),
            )

    adapter = VertexAdapter(Correcting())
    result, report = correct.correct_paragraphs(adapter, [original])
    assert result == [fixed]
    assert report.changed == 1 and not report.rejected
