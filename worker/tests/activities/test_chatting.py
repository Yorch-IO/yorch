"""What a settled turn writes down, which is not what the caller handed back.

`run_chat_turn` persists the answer itself rather than returning it, so the
transcript a reader comes back to is written here — and the one thing that was
being lost on the way is the reason a refusal gives.
"""

from __future__ import annotations

import pytest

from brainworker.activities import chatting
from brainworker.answering.types import Answer, Citation, Evidence
from brainworker.chat.types import ChatTurn
from brainworker.graph.schema import LEGACY_TENANT_ID


class _Settings:
    """Only what `_settle` reads: it opens one catalog and writes."""

    database_url = "postgresql:///unused"


class _Catalog:
    """Only `_settle`'s two calls."""

    def __init__(self) -> None:
        self.settled: dict | None = None
        self.cleared: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settle_turn(self, cid, seq, **kw):
        self.settled = {"conversation_id": cid, "seq": seq, **kw}

    def clear_deltas(self, cid, seq):
        self.cleared.append((cid, seq))


@pytest.fixture
def catalog(monkeypatch):
    fake = _Catalog()
    monkeypatch.setattr(chatting, "Catalog", lambda *_, **__: fake)
    return fake


def _turn() -> ChatTurn:
    return ChatTurn(
        conversation_id="cnv_1",
        turn_seq=1,
        tenant_id=LEGACY_TENANT_ID,
        library_id="lib_teologia",
        text="¿y sobre la esperanza?",
        effort="standard",
    )


def _settle(catalog, answer: Answer) -> dict:
    chatting._settle(
        _Settings(), _turn(), answer, "¿Qué dicen sobre la esperanza?", answer.state
    )
    assert catalog.settled is not None
    return catalog.settled


def test_a_refusal_keeps_the_reason_that_says_which_refusal_it_was(catalog):
    """Four states arrive as `insufficient_evidence` and they have four
    remedies: nothing cleared the dense floor, the model cited nothing
    verifiable, the search returned no fragment, or the envelope never closed.
    Dropping the reason flattens all four into "not enough evidence", which is a
    claim about the corpus the run may have no basis for.

    Found on the real corpus 2026-09-06: an answering call spent its whole
    65,521-token output ceiling on reasoning, returned no text, billed $0.497 —
    twenty times a normal `standard` turn — and rendered as "Evidencia
    insuficiente" with nothing to look at.
    """
    row = _settle(catalog, Answer(
        state="insufficient_evidence",
        reason="el modelo no pudo componer una respuesta (JSONDecodeError)",
    ))
    assert row["error"] == {
        "kind": "insufficient_evidence",
        "message": "el modelo no pudo componer una respuesta (JSONDecodeError)",
    }


def test_an_off_corpus_turn_says_so_rather_than_looking_like_a_gap(catalog):
    """`off_corpus` and `insufficient_evidence` are different states with
    different fixes, and the second is what a reader assumes when told nothing."""
    row = _settle(catalog, Answer(
        state="off_corpus",
        reason="ningún fragmento supera el umbral de similitud",
    ))
    assert row["error"]["kind"] == "off_corpus"


def test_an_answered_turn_carries_no_error_at_all(catalog):
    """A refusal's reason rides in `error` because both clients already render
    it. An answer has nothing to explain, and a non-null `error` beside real
    prose would read as an answer that half failed."""
    row = _settle(catalog, Answer(
        state="answered", text="La esperanza es…", reason="",
    ))
    assert row["error"] is None


def test_only_the_evidence_a_verified_citation_names_is_stored(catalog):
    """A `thorough` turn retrieves up to 48 chunks and the model cites a
    fraction; the citations panel needs the text of those, and the rest is
    evidence for an answer that did not use it."""
    def ev(chunk_id: str, text: str) -> Evidence:
        return Evidence(
            chunk_id=chunk_id, version_id="ver_1", document_id="doc_1",
            text=text, title="T", breadcrumb="", kind="cuerpo", score=0.9,
            locator="L",
        )

    cited, other = ev("chk_1", "uno"), ev("chk_2", "dos")
    row = _settle(catalog, Answer(
        state="answered",
        text="…",
        citations=[Citation(chunk_id="chk_1", locator="L", claim="c")],
        evidence=[cited, other],
    ))
    assert [e["chunk_id"] for e in row["cited_evidence"]] == ["chk_1"]


def test_the_relay_is_dropped_only_after_the_outcome_is_written(catalog):
    """Never the reverse: a reader still following the stream has to be able to
    reach the end of it, and the deltas are what they are reading."""
    _settle(catalog, Answer(state="answered", text="…"))
    assert catalog.cleared == [("cnv_1", 1)]
