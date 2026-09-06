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


# -- the query embedding's cache --------------------------------------------
#
# `search` fronts the query embedding with `CachedEmbedder` for latency, not for
# money: measured 2026-09-06, everything else in that function totals 9 ms while
# this one call ranged 0.4 s to 18.8 s against a per-minute quota. What the cache
# must not do is make the ledger claim money nobody spent.


class _CountingProvider:
    """Records how many texts it was actually asked to embed."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

        class _S:
            embedding_model = "gemini-embedding-2"
            embedding_dimensions = 4

        self.settings = _S()

    def embed(self, texts, *, task, workers=6):
        from brainworker.providers.gemini import Embedding, Usage

        self.calls.append(list(texts))
        return [
            Embedding(values=[0.1, 0.2, 0.3, 0.4], usage=Usage(input_tokens=7))
            for _ in texts
        ]


def _embedder(provider, cache):
    from brainworker.providers import CachedEmbedder

    return CachedEmbedder(
        provider, cache, model="gemini-embedding-2", dimensions=4, workers=1
    )


def test_a_repeated_question_is_not_embedded_again(tmp_path):
    from brainworker.providers.gemini import RETRIEVAL_QUERY

    p = _CountingProvider()
    first = _embedder(p, tmp_path).embed_many(["¿y las obras?"], task_type=RETRIEVAL_QUERY)
    second = _embedder(p, tmp_path).embed_many(["¿y las obras?"], task_type=RETRIEVAL_QUERY)

    assert p.calls == [["¿y las obras?"]], "the second ask reached the provider"
    # `approx`, not equality: the cache stores float32, so a cached vector comes
    # back rounded where a fresh one is whatever the API returned. That is
    # inconsequential — Qdrant stores and compares float32 either way, so the
    # document side has always been rounded — but it does mean a repeat question
    # is not bit-identical to its first ask, and asserting equality here would
    # have been asserting something false.
    assert first[0].values == pytest.approx(second[0].values, rel=1e-6)


def test_a_cache_hit_bills_nothing(tmp_path):
    """The rule `search` depends on, and the one that would fail quietly.

    A hit hands back an `Embedding` carrying the token count the *original* call
    cost — right for saying what a vector was worth, wrong for billing. `search`
    reads `embedder.usage`, which accumulates misses only, so a repeat question
    books a zero row rather than charging again for tokens nobody spent.
    """
    from brainworker.providers.gemini import RETRIEVAL_QUERY

    p = _CountingProvider()

    miss = _embedder(p, tmp_path)
    miss.embed_many(["¿la fe?"], task_type=RETRIEVAL_QUERY)
    assert miss.usage.input_tokens == 7
    assert miss.cache_hits == 0

    hit = _embedder(p, tmp_path)
    embedded = hit.embed_many(["¿la fe?"], task_type=RETRIEVAL_QUERY)
    assert hit.usage.input_tokens == 0, "a cache hit was billed"
    assert hit.cache_hits == 1
    # The vector still reports what it was worth, which is why reading *this*
    # for the charge would have been the silent mistake.
    assert embedded[0].usage.input_tokens == 7


def test_a_query_and_a_passage_do_not_share_a_cache_entry(tmp_path):
    """The two tasks embed asymmetrically on purpose (invariant #5), so one
    text under two tasks is two vectors and must be two entries — sharing them
    would serve a document vector to a question and measurably degrade
    retrieval, with nothing to see."""
    from brainworker.providers.gemini import RETRIEVAL_DOCUMENT, RETRIEVAL_QUERY

    p = _CountingProvider()
    _embedder(p, tmp_path).embed_many(["texto"], task_type=RETRIEVAL_QUERY)
    _embedder(p, tmp_path).embed_many(["texto"], task_type=RETRIEVAL_DOCUMENT)
    assert len(p.calls) == 2, "one task's vector was served for the other"
