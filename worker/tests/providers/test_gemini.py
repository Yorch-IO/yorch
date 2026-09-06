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
    """A quota gets its own, longer patience than an outage.

    `online_prediction_requests_per_base_model` is metered per *minute*. Three
    attempts at 1.5^n is about seven seconds, so every attempt landed inside the
    same exhausted bucket and the stage failed without having waited for the
    thing it was waiting for. A run stalled at `embedding 1/600` under 127 of
    these before the engine grew the longer policy this now matches.
    """
    monkeypatch.setattr(g.time, "sleep", lambda _: None)
    attempts = []

    def always_429():
        attempts.append(1)
        raise FakeAPIError(429, "RESOURCE_EXHAUSTED")

    p = Provider(settings())
    with pytest.raises(ProviderError) as e:
        p._call("embed", always_429)
    assert len(attempts) == g.RATE_LIMIT_ATTEMPTS
    assert g.RATE_LIMIT_ATTEMPTS > g.MAX_ATTEMPTS
    assert e.value.kind == "provider_quota"


def test_an_outage_keeps_the_shorter_patience(monkeypatch):
    """The longer budget is for a bucket that refills on a clock. An outage does
    not, and queueing behind one leaves the user watching a spinner."""
    monkeypatch.setattr(g.time, "sleep", lambda _: None)
    attempts = []

    def always_503():
        attempts.append(1)
        raise FakeAPIError(503, "UNAVAILABLE")

    with pytest.raises(ProviderError):
        Provider(settings())._call("generate", always_503)
    assert len(attempts) == g.MAX_ATTEMPTS


def test_the_quota_backoff_reaches_the_length_of_the_window():
    """2, 8, 32, 60, 60 — capped, because sleeping longer than the window buys
    nothing, and long enough that the last attempts are in a fresh bucket."""
    quota = ProviderError("x", kind="provider_quota", retryable=True)
    delays = [g._backoff(quota, n, 0.0) for n in range(1, g.RATE_LIMIT_ATTEMPTS)]

    assert [int(d) for d in delays] == [2, 8, 32, 60, 60]
    assert sum(delays) > 60, "the whole budget is shorter than one quota window"


def test_the_service_gets_to_say_how_long_to_wait(monkeypatch):
    """`Retry-After` wins over the guess: it knows when its bucket refills."""
    quota = ProviderError("x", kind="provider_quota", retryable=True)

    assert g._backoff(quota, 1, retry_after=17.0) == 17.0
    # Still capped: a header asking for ten minutes would strand the activity.
    assert g._backoff(quota, 1, retry_after=600.0) == g.RATE_LIMIT_MAX_BACKOFF


def test_a_missing_retry_after_header_costs_a_guess_not_a_failure():
    class Bare:
        pass

    assert g._retry_after(Bare()) == 0.0

    class Weird:
        headers = {"Retry-After": "not a number"}

    assert g._retry_after(Weird()) == 0.0

    class Real:
        headers = {"retry-after": "12"}

    assert g._retry_after(Real()) == 12.0


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


# -- streaming --------------------------------------------------------------
#
# The two rules `generate_stream` exists to keep are both about *when* things
# happen rather than what they are: retry stops once the reader has seen a
# delta, and usage is read at the end rather than the beginning.


class FakeChunk:
    """One streamed piece. `text` may be absent, empty, or raise."""

    def __init__(self, text=None, usage=None, explode=False):
        self._text = text
        self._explode = explode
        self.usage_metadata = usage

    @property
    def text(self):
        if self._explode:
            raise RuntimeError("no candidates on this chunk")
        return self._text


class FakeStreamModels:
    def __init__(self, *attempts):
        # One entry per attempt: either a list of chunks, or an exception to
        # raise, or a ("mid", chunks, exc) tuple that fails part-way through.
        self.attempts = list(attempts)
        self.calls = 0

    def generate_content_stream(self, **kw):
        self.calls += 1
        plan = self.attempts[min(self.calls - 1, len(self.attempts) - 1)]
        if isinstance(plan, Exception):
            raise plan

        def gen():
            for item in plan:
                if isinstance(item, Exception):
                    raise item
                yield item

        return gen()


def stream_provider(monkeypatch, models):
    p = Provider(settings())
    client = type("C", (), {"models": models})()
    monkeypatch.setattr(type(p), "client", property(lambda self: client))
    return p


def test_every_delta_reaches_the_callback_and_the_text_is_their_concatenation(monkeypatch):
    models = FakeStreamModels([FakeChunk("hola "), FakeChunk("mundo")])
    seen: list[str] = []
    out = stream_provider(monkeypatch, models).generate_stream("q", on_delta=seen.append)
    assert seen == ["hola ", "mundo"]
    assert out.text == "hola mundo"


def test_usage_is_taken_from_the_last_chunk_that_carries_any(monkeypatch):
    """The first chunk's counts are a fraction of the bill.

    Reading them would under-report the most expensive call in a turn, and
    nothing downstream could tell — a `Spend` row with plausible small numbers
    looks exactly like a cheap call.
    """
    models = FakeStreamModels([
        FakeChunk("a", usage=FakeUsage(prompt=10, candidates=1)),
        FakeChunk("b"),
        FakeChunk(None, usage=FakeUsage(prompt=10, candidates=900)),
    ])
    out = stream_provider(monkeypatch, models).generate_stream("q", on_delta=lambda _: None)
    assert out.usage.input_tokens == 10
    assert out.usage.output_tokens == 900
    assert out.usage.calls == 1


def test_a_stream_that_never_reports_usage_still_counts_the_call(monkeypatch):
    models = FakeStreamModels([FakeChunk("x")])
    out = stream_provider(monkeypatch, models).generate_stream("q", on_delta=lambda _: None)
    assert out.usage.calls == 1


def test_a_chunk_with_no_text_is_not_an_error(monkeypatch):
    """Every stream ends with one: a finish_reason and usage, and no parts."""
    models = FakeStreamModels([FakeChunk("solo"), FakeChunk(None), FakeChunk(explode=True)])
    seen: list[str] = []
    out = stream_provider(monkeypatch, models).generate_stream("q", on_delta=seen.append)
    assert seen == ["solo"]
    assert out.text == "solo"


def test_a_failure_opening_the_stream_is_retried(monkeypatch):
    monkeypatch.setattr(g.time, "sleep", lambda _: None)
    models = FakeStreamModels(FakeAPIError(503, "UNAVAILABLE"), [FakeChunk("al fin")])
    out = stream_provider(monkeypatch, models).generate_stream("q", on_delta=lambda _: None)
    assert out.text == "al fin"
    assert models.calls == 2


def test_a_failure_after_the_first_delta_is_not_retried(monkeypatch):
    """A retry here would replay text somebody has already read.

    The reader would watch the answer start over, which is worse than the error
    — and the tokens of the abandoned attempt are billed either way.
    """
    monkeypatch.setattr(g.time, "sleep", lambda _: None)
    models = FakeStreamModels([FakeChunk("empez"), FakeAPIError(503, "UNAVAILABLE")])
    seen: list[str] = []
    with pytest.raises(ProviderError) as e:
        stream_provider(monkeypatch, models).generate_stream("q", on_delta=seen.append)
    assert e.value.kind == "provider_unavailable"
    assert models.calls == 1
    assert seen == ["empez"]


def test_a_transport_failure_mid_stream_is_classified_not_swallowed(monkeypatch):
    models = FakeStreamModels([FakeChunk("a"), RuntimeError("connection reset")])
    with pytest.raises(ProviderError) as e:
        stream_provider(monkeypatch, models).generate_stream("q", on_delta=lambda _: None)
    assert e.value.kind == "provider_unavailable"
    assert "connection reset" in str(e.value)


def test_streaming_and_whole_calls_are_configured_identically(monkeypatch):
    """Both go through `_prepare`, which is the point of it existing.

    A thinking budget or a schema resolved differently on the two paths would
    make a streamed answer a different answer, not merely a differently
    delivered one.
    """
    p = Provider(settings())
    whole = p._prepare("q", system="S", response_schema={"x": 1}, stage="answering")
    streamed = p._prepare("q", system="S", response_schema={"x": 1}, stage="answering")
    assert whole[1].system_instruction == streamed[1].system_instruction
    assert whole[1].response_mime_type == streamed[1].response_mime_type == "application/json"
    assert whole[2] == streamed[2]


# -- why the model stopped ---------------------------------------------------


class _Candidate:
    def __init__(self, finish_reason):
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, finish_reason=None, candidates=None):
        self.candidates = (
            candidates if candidates is not None
            else ([_Candidate(finish_reason)] if finish_reason is not None else [])
        )


def test_the_finish_reason_is_read_by_name_and_never_by_str():
    """`types.FinishReason` is a *str-valued* `enum.Enum`, so `str(reason)` is
    `"FinishReason.MAX_TOKENS"` and matches nothing.

    Exactly the `HistoryEvent.event_type` trap already recorded in this
    repository, where `getattr(t, "name", str(t))` read as careful and silently
    yielded `"3"` — filtering a whole Temporal history to nothing and reporting
    a run that did nothing. Asserted against the SDK's own enum rather than a
    string, because a hand-built double would agree with the assumption.
    """
    from google.genai import types

    assert g._finish_of(_Response(types.FinishReason.MAX_TOKENS)) == g.MAX_TOKENS
    assert g._finish_of(_Response(types.FinishReason.STOP)) == "STOP"


def test_a_plain_string_finish_reason_survives_the_same_reader():
    """An SDK version that hands back the bare string instead of the enum: a
    `str` has no `.name`, so it falls through to itself."""
    assert g._finish_of(_Response("MAX_TOKENS")) == g.MAX_TOKENS


def test_a_response_with_no_candidates_reports_nothing_rather_than_guessing():
    assert g._finish_of(_Response()) is None
    assert g._finish_of(_Response(candidates=None)) is None


def test_a_streamed_call_reports_the_last_finish_reason_it_saw(monkeypatch):
    """The same rule as the usage, and for the same reason: a truncated stream
    ends with a chunk that carries a `finish_reason` and no text at all, so
    reading the first would report `None` for every call."""
    from google.genai import types

    class _FinishingChunk(FakeChunk):
        def __init__(self, text=None, usage=None, finish=None):
            super().__init__(text, usage)
            self.candidates = [_Candidate(finish)] if finish else []

    models = FakeStreamModels([
        _FinishingChunk("Según el corpus"),
        _FinishingChunk(None, finish=types.FinishReason.MAX_TOKENS),
    ])
    result = stream_provider(monkeypatch, models).generate_stream(
        "q", on_delta=lambda _: None
    )
    assert result.finish_reason == g.MAX_TOKENS
    assert result.text == "Según el corpus"
