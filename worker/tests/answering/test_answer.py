"""Composing an answer, and refusing to.

These are the tests that matter most in the product. A wrong ingest costs money;
a fabricated citation costs the user's trust in every answer they were given
before it, because there is no way to tell the two apart by looking.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from brainworker.answering import answer as mod
from brainworker.answering.effort import BUDGETS
from brainworker.answering.types import Evidence, Question
from brainworker.providers.gemini import Generation, Usage


class FakeProvider:
    """Returns a canned JSON answer and records the prompt it was given."""

    def __init__(self, payload: dict | str) -> None:
        self.payload = payload
        self.prompts: list[str] = []
        self.budgets: list[int | None] = []
        self.ceilings: list[int | None] = []
        #: What the model would report as its reason for stopping.
        self.finish_reason: str | None = None

        # Typed like the real `Gemini`, not trimmed to what today's assertions
        # touch. `compose` reads both of these to decide whether a per-question
        # effort level may override the configured reasoning budget, and a
        # double that omitted them would fail with an AttributeError rather than
        # exercise the policy.
        class _S:
            model = "gemini-3.6-flash"
            stage_thinking: dict[str, int | None] = {}
            thinking_budget: int | None = None

        self.settings = _S()
        self.usage = Usage(500, 120, 40, 1)

    def generate(self, prompt, *, system=None, temperature=0.0,
                 max_output_tokens=None, response_schema=None, stage=None,
                 thinking_budget=None):
        self.prompts.append(prompt)
        self.ceilings.append(max_output_tokens)
        # Recorded as "what the provider was actually handed", including the
        # absence of a budget — `None` here means the adapter forwarded nothing
        # and `thinking_for(stage)` decides, which is a different outcome from
        # being handed a number that happens to match.
        self.budgets.append(thinking_budget)
        text = (
            self.payload if isinstance(self.payload, str)
            else json.dumps(self.payload, ensure_ascii=False)
        )
        return Generation(
            text=text, usage=self.usage, finish_reason=self.finish_reason
        )


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


# --- the effort level's reasoning budget -----------------------------------
#
# `compose` is where the policy lives: an effort level may raise the answering
# reasoning budget only when the operator has expressed no opinion. Both ways
# of expressing one are checked, because the second is the easy one to miss.


def _answered(chunk: int = 0) -> dict:
    return {
        "suficiente": True, "respuesta": "Sí.",
        "citas": [{"chunk_id": f"chk_{chunk:024d}", "afirmacion": "Sí."}],
        "motivo": "",
    }


def _asked_at(effort: str) -> Question:
    return Question(library_id="lib_1", text="¿Qué hace feliz?", effort=effort)


def test_the_default_level_forwards_no_budget_at_all():
    """`standard` must leave `thinking_for("answering")` reaching the provider
    untouched — that resolution is the product's recorded position, and a level
    that merely re-sent the same number would have taken ownership of it."""
    provider = FakeProvider(_answered())
    mod.compose(provider, _asked_at("standard"), evidence(2))
    assert provider.budgets == [None]


def test_no_level_names_a_reasoning_budget_today():
    """And that is measured, not merely unset.

    `thorough` shipped at 8192 on the reasoning that the widest level should
    think hardest. Measured on the real corpus 2026-09-03, four questions with
    its evidence held constant: leaving it unset won 3 and tied 1, never losing,
    and spent *more* output on 3 of 4. `None` sends no `ThinkingConfig`, so the
    model picks per question — a literal caps that rather than raising it. See
    the note in `BUDGETS`.
    """
    assert all(BUDGETS[n].thinking_override is None for n in BUDGETS)


def _with_override(monkeypatch, level, value):
    """Give one level a reasoning budget, so the plumbing can be exercised.

    Every level names None today, so a test reading the real table could not
    tell a threaded override from one silently dropped. These monkeypatch a
    number in rather than asserting against the table, which keeps the
    mechanism pinned while the table stays measured.
    """
    monkeypatch.setitem(
        BUDGETS, level, dataclasses.replace(BUDGETS[level], thinking_override=value)
    )


def test_a_level_that_names_a_budget_has_it_forwarded(monkeypatch):
    _with_override(monkeypatch, "thorough", 4096)
    provider = FakeProvider(_answered())
    mod.compose(provider, _asked_at("thorough"), evidence(2))
    assert provider.budgets == [4096]


def test_naming_the_answering_stage_wins_over_the_level(monkeypatch):
    """`BRAIN_THINKING_ANSWERING` lands in `stage_thinking`. Somebody naming
    this stage explicitly means this stage, and outranks a per-question dial."""
    _with_override(monkeypatch, "thorough", 4096)
    provider = FakeProvider(_answered())
    provider.settings.stage_thinking = {"answering": 0}
    mod.compose(provider, _asked_at("thorough"), evidence(2))
    assert provider.budgets == [None], "the level must not override the operator"


def test_a_global_budget_of_zero_still_reaches_answering_at_every_level(monkeypatch):
    """The guard that is easy to get wrong, and the reason it is written twice.

    `answering` is deliberately *absent* from the stage map so that a global
    `BRAIN_THINKING_BUDGET` reaches it — the config's own words are "someone
    turning off every reasoning cost means it". A guard that checked only the
    per-stage map would be satisfied here and let `thorough` spend against an
    operator who had globally set zero.
    """
    _with_override(monkeypatch, "thorough", 4096)
    provider = FakeProvider(_answered())
    provider.settings.thinking_budget = 0
    mod.compose(provider, _asked_at("thorough"), evidence(2))
    assert provider.budgets == [None]


# --- the level is recorded on the answer ------------------------------------


def test_every_way_out_of_compose_records_the_level_it_answered_at():
    """Five returns, and the four that are not the happy path are the ones a
    user reaches when they are about to blame the effort setting."""
    asked = _asked_at("thorough")

    # No evidence at all.
    assert mod.compose(FakeProvider(_answered()), asked, []).effort == "thorough"

    # The generation call itself failed.
    assert mod.compose(FakeProvider("not json"), asked, evidence(2)).effort == "thorough"

    # The model said the fragments do not contain the answer.
    thin = FakeProvider({"suficiente": False, "motivo": "no está", "citas": []})
    assert mod.compose(thin, asked, evidence(2)).effort == "thorough"

    # It claimed an answer and cited nothing checkable.
    unbacked = FakeProvider({
        "suficiente": True, "respuesta": "Sí.", "motivo": "",
        "citas": [{"chunk_id": "chk_" + "f" * 24, "afirmacion": "inventado"}],
    })
    assert mod.compose(unbacked, asked, evidence(2)).effort == "thorough"

    # And the answered path.
    assert mod.compose(FakeProvider(_answered()), asked, evidence(2)).effort == "thorough"


# -- streaming --------------------------------------------------------------
#
# `citas` is the last field in SCHEMA, so verification cannot run until the
# envelope closes. Everything below is about that gap: what a reader is shown
# while it is open, and what is true once it shuts.


class StreamingProvider(FakeProvider):
    """Hands the canned payload over in fixed-size pieces, like a real stream."""

    def __init__(self, payload, *, piece: int = 7) -> None:
        super().__init__(payload)
        self.piece = piece

    def generate_stream(self, prompt, *, on_delta, **kw):
        result = self.generate(prompt, **kw)
        for i in range(0, len(result.text), self.piece):
            on_delta(result.text[i:i + self.piece])
        return result


def stream(payload, ev=None, **kw):
    """Compose with streaming on; return (answer, what the reader was shown)."""
    shown: list[str] = []
    provider = StreamingProvider(payload, **kw)
    result = mod.compose(
        provider, question(), evidence(2) if ev is None else ev,
        on_delta=shown.append,
    )
    return result, "".join(shown)


def test_the_prose_is_streamed_and_the_envelope_is_not():
    answer, shown = stream({
        "suficiente": True,
        "respuesta": "La gente es feliz cuando tiene propósito.",
        "citas": [{"chunk_id": f"chk_{0:024d}", "afirmacion": "propósito"}],
        "motivo": "",
    })
    assert answer.state == "answered"
    # No braces, no field names, no escapes: the reader saw prose.
    assert shown == "La gente es feliz cuando tiene propósito."
    assert "suficiente" not in shown and "{" not in shown


def test_streaming_produces_the_same_answer_as_not_streaming():
    payload = {
        "suficiente": True,
        "respuesta": "Una respuesta cualquiera.",
        "citas": [{"chunk_id": f"chk_{1:024d}", "afirmacion": "algo"}],
        "motivo": "",
    }
    whole = mod.compose(FakeProvider(payload), question(), evidence(2))
    streamed, _ = stream(payload)
    assert dataclasses.asdict(streamed) == dataclasses.asdict(whole)


def test_a_streamed_turn_that_loses_its_citations_is_still_refused():
    """The gap this whole design has to survive.

    The model wrote a confident, fluent paragraph and cited a chunk it was never
    shown. The reader watched that paragraph arrive. It is still not an answer,
    and `state` has to say so — otherwise streaming would have created a way to
    ship an unverified answer that the non-streaming path refuses.
    """
    answer, shown = stream({
        "suficiente": True,
        "respuesta": "La felicidad procede de la virtud, según el texto.",
        "citas": [{"chunk_id": "chk_" + "f" * 24, "afirmacion": "inventada"}],
        "motivo": "",
    })
    assert shown == "La felicidad procede de la virtud, según el texto."
    assert answer.state == "insufficient_evidence"
    assert answer.text == ""
    assert answer.citations == []
    assert "inexistente" in answer.reason


def test_a_streamed_turn_the_model_itself_refuses_streams_nothing():
    """`suficiente: false` comes first in the envelope and `respuesta` is empty,
    so there is no draft to withdraw — the refusal is all there ever was."""
    answer, shown = stream({
        "suficiente": False,
        "respuesta": "",
        "citas": [],
        "motivo": "los fragmentos no hablan de eso",
    })
    assert shown == ""
    assert answer.state == "insufficient_evidence"
    assert answer.reason == "los fragmentos no hablan de eso"


def test_a_truncated_envelope_still_hands_over_the_draft_it_had():
    """`MAX_TOKENS` cuts the object mid-string.

    `json.loads` then fails and the turn is refused — but the characters already
    decoded were paid for, and the caller is owed them so it can explain what
    happened rather than showing a blank.
    """
    answer, shown = stream('{"suficiente": true, "respuesta": "empezó a responder y')
    assert shown == "empezó a responder y"
    assert answer.state == "insufficient_evidence"
    assert answer.spend, "a truncated call still billed for its tokens"


def test_the_draft_may_contain_a_chunk_id_the_finished_answer_does_not():
    """`_clean` runs on the parsed text, never on the deltas.

    So the two can differ, and this pins the direction: what is returned is
    clean. A caller that appended the deltas instead of replacing them would
    leave an id on screen that the answer does not contain.
    """
    cited = f"chk_{0:024d}"
    answer, shown = stream({
        "suficiente": True,
        "respuesta": f"La virtud basta [{cited}].",
        "citas": [{"chunk_id": cited, "afirmacion": "virtud"}],
        "motivo": "",
    })
    assert cited in shown
    assert cited not in answer.text
    assert answer.state == "answered"


# -- a call that thought until it ran out of room ----------------------------
#
# Measured on the real corpus 2026-09-06: one `standard` chat turn's answering
# call billed **65,521 output tokens and $0.497373** over 6m42s and produced not
# one character of prose. 65,521 is `gemini-3.6-flash`'s own 65,536-token
# ceiling. Nothing failed anywhere: the run is `succeeded`, and the turn read
# "Evidencia insuficiente" — a claim about the corpus this run had no basis for,
# which sends the reader to look at their library instead of at the bill.


def _truncated() -> FakeProvider:
    p = FakeProvider('{"suficiente": true, "respuesta": "El corpus indica q')
    p.finish_reason = "MAX_TOKENS"
    p.usage = Usage(3977, 65521, 65500, 1)
    return p


def test_a_truncated_answer_says_the_model_ran_out_of_room():
    got = mod.compose(_truncated(), question(), evidence())
    assert got.state == "insufficient_evidence"
    # The number is what makes it legible as a bill rather than as a corpus.
    assert "65521" in got.reason
    assert "agotó su límite de salida" in got.reason


def test_a_truncation_does_not_read_as_a_gap_in_the_library():
    """The wrong sentence is the whole harm here. Both refusals ask for the same
    action — ask again — so this stayed `insufficient_evidence` rather than
    earning a state of its own; what had to change is what it says."""
    got = mod.compose(_truncated(), question(), evidence())
    assert "fragmentos no contienen" not in got.reason
    # Nor the generic branch's wording, which names an exception class and
    # nothing a reader can act on. Asserted because dropping the truncation
    # branch leaves that sentence behind, and it passes the two negatives above
    # while saying nothing true.
    assert "no pudo componer" not in got.reason


def test_a_truncated_call_still_reports_what_it_spent():
    """It billed 65,521 output tokens. A refusal that reported no spend would
    hide the one number that explains the incident."""
    got = mod.compose(_truncated(), question(), evidence())
    assert got.spend and got.spend[0].output_tokens == 65521


def test_an_envelope_that_is_merely_malformed_keeps_its_own_wording():
    """The generic branch still exists and still names the exception, because a
    schema violation is a different thing to chase."""
    p = FakeProvider("no soy json en absoluto")
    got = mod.compose(p, question(), evidence())
    assert got.state == "insufficient_evidence"
    assert "no pudo componer" in got.reason


def test_every_answering_call_carries_the_output_ceiling():
    """Unbounded is what let one call bill $0.497 for nothing. The number is
    4.5x the widest output ever measured here (`thorough` at 3,582 tokens), so
    it bounds the runaway and no answer this product has produced comes near
    it — and it caps the *bill*, not the reasoning: every effort level still
    names no `thinking_budget`, which a measured A/B chose over 8192.
    """
    p = FakeProvider({"suficiente": False, "motivo": "nada"})
    mod.compose(p, question(), evidence())
    assert p.ceilings == [mod.MAX_OUTPUT_TOKENS]
    assert p.budgets == [None], "a level named a reasoning budget"
