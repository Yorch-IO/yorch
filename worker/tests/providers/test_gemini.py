"""Provider behaviour, against a mocked client.

Nothing here reaches Vertex. The point of these tests is the parts that are ours
— credential handling, error classification, retry, dimension checking — and a
real call would test Google's availability instead.
"""

from __future__ import annotations

import pytest
from google.genai import errors

from brainworker.config import Gemini
from brainworker.providers import Provider, ProviderError, Usage
from brainworker.providers import gemini as g


class FakeAPIError(errors.APIError):
    """`errors.APIError` needs a response object; this is the smallest stand-in."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        self.status = message
        Exception.__init__(self, f"{code} {message}")


def settings(**kw) -> Gemini:
    return Gemini(project_id="proj-1", **kw)


def provider(monkeypatch, client) -> Provider:
    p = Provider(settings())
    monkeypatch.setattr(type(p), "client", property(lambda self: client))
    return p


# -- credentials ------------------------------------------------------------


def test_a_provider_without_a_project_refuses_to_be_built():
    with pytest.raises(ProviderError) as e:
        Provider(Gemini())
    assert e.value.kind == "provider_unconfigured"


def test_no_credential_ever_appears_in_the_settings_object():
    """The whole reason for ADC: there is no secret here to leak into a payload,
    a log line, the catalog, or Temporal history."""
    text = repr(settings())
    for word in ("key", "token", "secret", "credential", "password"):
        assert word not in text.lower()


def test_missing_adc_is_reported_with_the_command_that_fixes_it(monkeypatch):
    def explode(**_):
        raise RuntimeError("could not automatically determine credentials")

    monkeypatch.setattr(g.genai, "Client", explode)
    with pytest.raises(ProviderError) as e:
        _ = Provider(settings()).client
    assert e.value.kind == "provider_no_credentials"
    assert "gcloud auth application-default login" in str(e.value)


def test_the_client_is_built_lazily(monkeypatch):
    """A worker must start and report itself unhealthy on a machine with no ADC,
    not crash before it can say so."""
    built = []
    monkeypatch.setattr(g.genai, "Client", lambda **kw: built.append(kw) or object())
    p = Provider(settings())
    assert built == []
    _ = p.client
    _ = p.client
    assert len(built) == 1
    assert built[0]["vertexai"] is True
    assert built[0]["location"] == "global"


# -- error classification ---------------------------------------------------


@pytest.mark.parametrize(
    "code,message,kind,retryable",
    [
        (429, "RESOURCE_EXHAUSTED", "provider_quota", True),
        (403, "PERMISSION_DENIED", "provider_forbidden", False),
        (401, "unauthenticated", "provider_forbidden", False),
        (404, "model not found", "provider_model_missing", False),
        (503, "backend unavailable", "provider_unavailable", True),
        (400, "invalid argument", "provider_refused", False),
    ],
)
def test_each_failure_is_classified_by_the_fix_it_needs(code, message, kind, retryable):
    err = g._classify(FakeAPIError(code, message))
    assert err.kind == kind
    assert err.retryable is retryable


def test_a_permission_error_names_the_role_to_grant():
    err = g._classify(FakeAPIError(403, "PERMISSION_DENIED"))
    assert "roles/aiplatform.user" in str(err)


# -- retry ------------------------------------------------------------------


def test_a_quota_error_is_retried_then_surfaced(monkeypatch):
    monkeypatch.setattr(g.time, "sleep", lambda _: None)
    attempts = []

    def always_429():
        attempts.append(1)
        raise FakeAPIError(429, "RESOURCE_EXHAUSTED")

    p = Provider(settings())
    with pytest.raises(ProviderError) as e:
        p._call("embed", always_429)
    assert len(attempts) == g.MAX_ATTEMPTS
    assert e.value.kind == "provider_quota"


def test_a_permission_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(g.time, "sleep", lambda _: None)
    attempts = []

    def always_403():
        attempts.append(1)
        raise FakeAPIError(403, "PERMISSION_DENIED")

    with pytest.raises(ProviderError):
        Provider(settings())._call("generate", always_403)
    assert len(attempts) == 1, "retrying a missing role only wastes the user's time"


def test_a_transient_failure_that_then_succeeds_returns_the_result(monkeypatch):
    monkeypatch.setattr(g.time, "sleep", lambda _: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise FakeAPIError(503, "backend unavailable")
        return "recovered"

    assert Provider(settings())._call("generate", flaky) == "recovered"
    assert len(calls) == 2


# -- embeddings -------------------------------------------------------------


class FakeEmbedding:
    def __init__(self, values):
        self.values = values


class FakeResponse:
    def __init__(self, embeddings=None, text=None, usage=None):
        self.embeddings = embeddings
        self.text = text
        self.usage_metadata = usage


class FakeUsage:
    def __init__(self, prompt=0, candidates=0, total=0):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.total_token_count = total


class FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def embed_content(self, **kw):
        self.calls.append(kw)
        return self.response

    def generate_content(self, **kw):
        self.calls.append(kw)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.models = FakeModels(response)


def test_embedding_dimensions_are_checked_against_the_collection_contract(monkeypatch):
    """A Qdrant collection's vector size is fixed at creation, so a short vector
    has to fail here rather than at write time."""
    client = FakeClient(FakeResponse(embeddings=[FakeEmbedding([0.1] * 768)]))
    with pytest.raises(ProviderError, match="dimensions"):
        provider(monkeypatch, client).embed(["hola"])


def test_one_text_per_request_because_batching_fails_silently(monkeypatch):
    """Measured against `gemini-embedding-2` on 2026-08-19: a request carrying
    four texts returns *one* embedding and no error.

    That is the engine's inherited invariant #6, established for
    `gemini-embedding-001` and still true for its replacement. Batching would
    have left every chunk after the first unembedded.
    """
    client = FakeClient(FakeResponse(embeddings=[FakeEmbedding([0.0] * 3072)]))
    p = provider(monkeypatch, client)
    result = p.embed(["uno", "dos", "tres"])

    assert len(result) == 3
    assert len(client.models.calls) == 3, "one request per text"
    for call in client.models.calls:
        assert isinstance(call["contents"], str), "never a list of texts"


def test_a_response_carrying_more_than_one_embedding_is_refused(monkeypatch):
    """Nothing observed does this, but pairing vector N with chunk N is the one
    thing that must not be assumed."""
    client = FakeClient(
        FakeResponse(embeddings=[FakeEmbedding([0.0] * 3072)] * 2)
    )
    with pytest.raises(ProviderError, match="asked for one embedding"):
        provider(monkeypatch, client).embed(["uno"])


def test_the_indexing_and_query_tasks_are_kept_distinct(monkeypatch):
    client = FakeClient(FakeResponse(embeddings=[FakeEmbedding([0.0] * 3072)]))
    p = provider(monkeypatch, client)

    p.embed(["a"], task=g.RETRIEVAL_DOCUMENT)
    p.embed(["a"], task=g.RETRIEVAL_QUERY)
    assert [c["config"].task_type for c in client.models.calls] == [
        "RETRIEVAL_DOCUMENT",
        "RETRIEVAL_QUERY",
    ]

    with pytest.raises(ValueError):
        p.embed(["a"], task="SEMANTIC_SIMILARITY")


def test_an_empty_batch_costs_nothing(monkeypatch):
    client = FakeClient(FakeResponse(embeddings=[]))
    assert provider(monkeypatch, client).embed([]) == []
    assert client.models.calls == []


def test_embedding_tokens_come_from_statistics_not_usage_metadata(monkeypatch):
    """`usage_metadata` is None for embeddings — measured against
    `gemini-embedding-2` on 2026-08-19. Reading only the response level reports
    every embedding as free, which is the engine's invariant #7 naming two
    places instead of one."""
    class Stats:
        token_count = 8

    class Counted(FakeEmbedding):
        def __init__(self, values):
            super().__init__(values)
            self.statistics = Stats()

    client = FakeClient(FakeResponse(embeddings=[Counted([0.0] * 3072)], usage=None))
    result = provider(monkeypatch, client).embed(["la gracia común"])
    assert result[0].usage.input_tokens == 8
    assert result[0].usage.output_tokens == 0


# -- usage ------------------------------------------------------------------


def test_usage_is_read_from_what_the_api_reported(monkeypatch):
    client = FakeClient(
        FakeResponse(text="hola", usage=FakeUsage(prompt=120, candidates=45))
    )
    result = provider(monkeypatch, client).generate("¿qué es la gracia?")
    assert result.text == "hola"
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 45
    assert result.usage.calls == 1


def test_a_response_without_usage_metadata_does_not_fail_the_run(monkeypatch):
    """Reporting zero tokens is wrong but recoverable; raising would fail a run
    that actually succeeded."""
    client = FakeClient(FakeResponse(text="hola", usage=None))
    result = provider(monkeypatch, client).generate("x")
    assert result.usage == Usage(input_tokens=0, output_tokens=0, calls=1)


def test_usage_accumulates_across_calls():
    total = Usage()
    total.add(Usage(input_tokens=10, output_tokens=2, calls=1))
    total.add(Usage(input_tokens=5, output_tokens=1, calls=1))
    assert (total.input_tokens, total.output_tokens, total.calls) == (15, 3, 2)


def test_json_mode_is_requested_by_schema_not_by_prompt_wording(monkeypatch):
    """A schema violation then surfaces on the call that caused it, rather than
    as a parse failure three stages later."""
    client = FakeClient(FakeResponse(text="{}", usage=FakeUsage()))
    p = provider(monkeypatch, client)
    p.generate("extract", response_schema={"type": "object"})
    config = client.models.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema == {"type": "object"}
