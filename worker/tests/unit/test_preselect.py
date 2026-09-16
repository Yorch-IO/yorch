"""Judging a channel's metadata, and the three checks the prompt cannot make.

The prompt asks for one verdict per video with the id it was given. These assert
what happens when it does not: an id from outside the batch, a score outside the
range, and a video that comes back with nothing at all. All three have to be
visible, because the honesty of the whole feature rests on one property — a
candidate the reader is offered came from the list they were told was examined.
"""

from __future__ import annotations

import json

import pytest

from brainworker.channel import preselect
from brainworker.providers.gemini import Generation, Usage
from brainworker.youtube import ChannelVideo


class FakeProvider:
    """The double `tests/unit/test_bookmeta.py` uses, answering from a script."""

    def __init__(self, payloads: list[object] | None = None, *, raises=None):
        self.payloads = list(payloads or [])
        self.raises = raises
        self.usage = Usage(1000, 200, 0, 1)
        self.calls: list[dict] = []

        class _Settings:
            model = "gemini-3.6-flash"

        self.settings = _Settings()

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None):
        self.calls.append({"prompt": prompt, "system": system, "stage": stage})
        if self.raises:
            raise self.raises
        payload = self.payloads.pop(0) if self.payloads else {"resultados": []}
        return Generation(text=json.dumps(payload), usage=self.usage, finish_reason=None)


def _videos(n: int) -> list[ChannelVideo]:
    return [
        ChannelVideo(
            video_id=f"v{i:010d}"[:11],
            title=f"Prédica {i}",
            description="d" * 50,
            published_at="2026-01-01T00:00:00Z",
            duration_s=2700,
        )
        for i in range(n)
    ]


def _row(vid: str, relevancia="relevante", puntaje=80, razon="el título lo dice",
         incertidumbre="baja") -> dict:
    return {
        "video_id": vid,
        "relevancia": relevancia,
        "puntaje": puntaje,
        "razon": razon,
        "incertidumbre": incertidumbre,
    }


# --- the property the whole feature rests on ---------------------------------


def test_every_candidate_comes_from_the_batch_that_was_sent():
    videos = _videos(3)
    provider = FakeProvider([{"resultados": [_row(v.video_id) for v in videos]}])
    result, _ = preselect.select(provider, "justicia social", videos)
    assert [c.video_id for c in result.candidates] == [v.video_id for v in videos]
    assert result.evaluated == [v.video_id for v in videos]


def test_an_invented_id_is_dropped_and_counted():
    # Never silently kept: a candidate the reader is offered has to come from
    # the list they were told was examined.
    videos = _videos(2)
    provider = FakeProvider(
        [{"resultados": [_row(videos[0].video_id), _row("zzzzzzzzzzz")]}]
    )
    result, _ = preselect.select(provider, "tema", videos)
    assert result.invented == 1
    assert "zzzzzzzzzzz" not in [c.video_id for c in result.candidates]
    assert len(result.candidates) == 2


@pytest.mark.parametrize("puntaje", [-1, 101, 1000, "ochenta", None])
def test_a_score_outside_the_range_loses_the_verdict_not_the_video(puntaje):
    # Refused rather than clamped: clamping writes down a number nobody
    # produced, and `sin_evaluar` says the true thing instead.
    videos = _videos(1)
    provider = FakeProvider(
        [{"resultados": [_row(videos[0].video_id, puntaje=puntaje)]}]
    )
    result, _ = preselect.select(provider, "tema", videos)
    assert result.malformed == 1
    assert result.candidates[0].relevancia == preselect.UNEVALUATED
    assert result.unevaluated == 1


def test_a_relevance_outside_the_enum_is_refused():
    videos = _videos(1)
    provider = FakeProvider(
        [{"resultados": [_row(videos[0].video_id, relevancia="quizás")]}]
    )
    result, _ = preselect.select(provider, "tema", videos)
    assert result.malformed == 1
    assert result.candidates[0].relevancia == preselect.UNEVALUATED


def test_a_video_the_model_skipped_is_unevaluated_and_not_discarded():
    # Treating silence as "descartado" would hide a model quietly answering
    # about fewer videos than it was asked about.
    videos = _videos(3)
    provider = FakeProvider([{"resultados": [_row(videos[0].video_id)]}])
    result, _ = preselect.select(provider, "tema", videos)
    assert result.unevaluated == 2
    assert [c.relevancia for c in result.candidates[1:]] == [
        preselect.UNEVALUATED,
        preselect.UNEVALUATED,
    ]


def test_sin_evaluar_is_not_a_word_the_model_may_return():
    # Keeping it out of the schema is what stops the model using it to decline.
    enum = preselect.SCHEMA["properties"]["resultados"]["items"]["properties"][
        "relevancia"
    ]["enum"]
    assert preselect.UNEVALUATED not in enum
    assert set(enum) == set(preselect.RELEVANCE)


# --- batching ----------------------------------------------------------------


def test_videos_are_judged_in_batches_and_every_one_is_judged():
    videos = _videos(60)
    payloads = [
        {"resultados": [_row(v.video_id) for v in batch]}
        for batch in preselect.batches(videos)
    ]
    provider = FakeProvider(payloads)
    result, _ = preselect.select(provider, "tema", videos)
    assert len(provider.calls) == 3  # 25 + 25 + 10
    assert result.unevaluated == 0
    assert len(result.candidates) == 60


def test_one_failed_batch_does_not_cost_the_channel_its_other_verdicts():
    videos = _videos(30)

    class Flaky(FakeProvider):
        def generate(self, prompt, **kw):
            self.calls.append({"prompt": prompt})
            if len(self.calls) == 1:
                raise RuntimeError("provider said no")
            return Generation(
                text=json.dumps(
                    {"resultados": [_row(v.video_id) for v in videos[25:]]}
                ),
                usage=self.usage,
                finish_reason=None,
            )

    result, spend = preselect.select(Flaky(), "tema", videos)
    assert result.unevaluated == 25
    assert sum(1 for c in result.candidates if c.relevancia == "relevante") == 5
    # And the spend is still reported: the tokens were billed either way.
    assert spend.stage == preselect.STAGE


# --- the shortlist -----------------------------------------------------------


def test_the_shortlist_keeps_dudoso_and_drops_descartado():
    # `dudoso` is exactly what the transcript pass exists to settle — dropping
    # it here would make the cheap guess final.
    videos = _videos(3)
    provider = FakeProvider(
        [
            {
                "resultados": [
                    _row(videos[0].video_id, "descartado", 5),
                    _row(videos[1].video_id, "dudoso", 50),
                    _row(videos[2].video_id, "relevante", 90),
                ]
            }
        ]
    )
    result, _ = preselect.select(provider, "tema", videos)
    assert [c.video_id for c in result.shortlist] == [
        videos[2].video_id,
        videos[1].video_id,
    ]


# --- what the estimate prices ------------------------------------------------


def test_the_payload_the_estimate_prices_is_the_payload_that_is_sent():
    # The rule `bookmeta.METADATA_CHARS` states: the estimate prices exactly
    # this string, so the two must not drift.
    videos = _videos(2)
    provider = FakeProvider([{"resultados": []}])
    preselect.select(provider, "justicia social", videos)
    assert provider.calls[0]["prompt"] == preselect.payload_for(
        "justicia social", videos
    )


def test_a_long_description_is_cut_before_it_is_sent():
    videos = [
        ChannelVideo(
            video_id="aaaaaaaaaaa",
            title="T" * 400,
            description="D" * 5000,
            published_at="",
        )
    ]
    sent = json.loads(preselect.payload_for("tema", videos))["videos"][0]
    assert len(sent["titulo"]) == preselect.TITLE_CHARS
    assert len(sent["descripcion"]) == preselect.DESCRIPTION_CHARS


def test_the_record_survives_serialisation():
    # Asserted through `asdict` rather than on the object, because reading the
    # attribute directly is exactly what did not catch `Answer.style_effort`.
    from dataclasses import asdict

    videos = _videos(1)
    provider = FakeProvider([{"resultados": [_row(videos[0].video_id)]}])
    result, _ = preselect.select(provider, "tema", videos)
    dumped = asdict(result)
    assert dumped["topic"] == "tema"
    assert dumped["prompt_version"] == preselect.PROMPT_VERSION
    assert dumped["model"] == "gemini-3.6-flash"
    assert dumped["evaluated"] == [videos[0].video_id]
    assert dumped["candidates"][0]["razon"] == "el título lo dice"
    assert set(dumped) >= {
        "topic", "model", "prompt_version", "evaluated", "candidates",
        "invented", "malformed", "unevaluated",
    }
