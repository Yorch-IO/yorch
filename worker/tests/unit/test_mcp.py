"""The protocol surface, and the decisions it must not quietly lose.

`scripts/mcp_server.py` holds the pump and the HTTP and no decisions; all of
them are here, which is the same split `probing.py`/`probe_retrieval.py` uses.
What is worth asserting is the JSON an agent receives and the wording it reads,
because both are contracts with something that is not this codebase.
"""

from __future__ import annotations

import pytest

from brainworker import mcp

LIBS = [
    {"id": "lib_teologia", "name": "Teología", "documents": 80,
     "indexed_versions": 76, "language": "es"},
    {"id": "lib_pruebas", "name": "Pruebas", "documents": 5,
     "indexed_versions": 3, "language": "es"},
]


def _envelope(answer_state: str, **answer: object) -> dict:
    """The envelope `GET /ask/{id}` returns, top-level state and all.

    Captured from a real call rather than imagined: `{"state": "done",
    "answer": {"state": "answered", ...}}`. The outer one is the *run's*
    outcome and is `done` for a refusal too, because `asking._record` maps
    "the corpus does not cover this" onto a success on purpose.
    """
    return {"question_id": "ask-1", "state": "done",
            "answer": {"state": answer_state, **answer}, "error": None}


def _dispatch(message, *, libraries=lambda: LIBS, ask=None, collect=None):
    return mcp.dispatch(
        message,
        libraries=libraries,
        # `done` outside, the answer's own state inside — the shape
        # `GET /ask/{id}` really returns, not the one it is easy to assume.
        ask=ask or (lambda q, l, e: ("q1", _envelope("answered"), 1.0)),
        collect=collect or (lambda qid: _envelope("answered")),
    )


# --- the handshake -----------------------------------------------------------


def test_a_notification_is_answered_with_nothing():
    """JSON-RPC forbids a response to a message with no `id`, and a server that
    replied to `notifications/initialized` has every strict client close the
    connection at the handshake — before a single tool is ever listed."""
    assert _dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_initialize_answers_in_the_clients_own_revision_when_it_can():
    out = _dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05"}})
    assert out["result"]["protocolVersion"] == "2024-11-05"


def test_an_unknown_revision_is_answered_in_ours_rather_than_echoed():
    """Echoing a revision we do not implement would promise shapes we do not
    send; the specification asks a server to state what it supports and let the
    client decide."""
    out = _dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "1999-01-01"}})
    assert out["result"]["protocolVersion"] == mcp.PROTOCOL


# --- what the agent is offered -----------------------------------------------


def test_the_tools_are_ask_and_collect_and_nothing_that_returns_passages():
    """The decision this whole module exists for, pinned.

    An MCP tool that hands raw chunks to another model exports the retrieval and
    leaves behind the one property that makes this a product rather than a
    vector database: every citation here has already survived `answer._verify`.
    A `retrieve` or `search` tool added later must fail this test and send
    whoever added it to the module docstring."""
    out = _dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [t["name"] for t in out["result"]["tools"]] == ["ask", "collect"]


def test_the_library_ids_reach_the_schema_and_not_only_the_prose():
    """RAGFlow builds the description from the live dataset list so a model can
    name one without a second round trip. Putting the ids in the `enum` is the
    same idea one step further and costs nothing: a model then *cannot* name a
    library that does not exist, where a description can only ask it not to."""
    schema = mcp.tool_schema(LIBS)
    assert schema["inputSchema"]["properties"]["library_id"]["enum"] == [
        "lib_teologia", "lib_pruebas"]
    assert "lib_teologia" in schema["description"]


def test_an_installation_with_no_libraries_omits_the_enum_rather_than_emitting_one():
    """An empty `enum` is a schema nothing can satisfy, so a fresh installation
    would answer every call with a validation failure instead of saying it has
    nothing indexed yet."""
    schema = mcp.tool_schema([])
    assert "enum" not in schema["inputSchema"]["properties"]["library_id"]
    assert "no libraries" in schema["inputSchema"]["properties"]["library_id"]["description"]


def test_the_effort_levels_travel_by_name_and_never_as_numbers():
    schema = mcp.tool_schema(LIBS)
    assert schema["inputSchema"]["properties"]["effort"]["enum"] == [
        "brief", "standard", "thorough"]
    assert "top_k" not in str(schema)


# --- what comes back ---------------------------------------------------------


def test_an_answer_always_carries_its_citations():
    """Prose without the citations is the artefact this product refuses to
    make, and a calling model that received only the prose would relay only the
    prose."""
    text = mcp.render_answer(_envelope(
        "answered",
        text="La justificación es por la fe.",
        citations=[{"locator": "Reto de Dios · p. 12 · [100:900]",
                    "claim": "por la fe y no por obras", "chunk_id": "chk_x"}],
        spend=[{"usd": 0.0233}], effort="standard"))
    assert "Reto de Dios · p. 12" in text
    assert "por la fe y no por obras" in text
    assert "1 verified" in text


def test_a_refusal_is_a_result_and_says_which_refusal_it_was():
    """`off_corpus` and `insufficient_evidence` have different fixes, and "no
    answer" with nothing to look at is indistinguishable from a broken index —
    the rule `answer.py` already states, arriving at a new reader."""
    out = _dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "ask", "arguments": {
             "question": "¿Qué es la mecánica cuántica?", "library_id": "lib_teologia"}}},
        ask=lambda q, l, e: ("q1", _envelope(
            "off_corpus",
            reason="ningún fragmento de esta biblioteca supera el umbral"), 2.0),
    )
    body = out["result"]["content"][0]["text"]
    assert "isError" not in out["result"], "a refusal is not a tool failure"
    assert "similarity floor" in body
    assert "umbral" in body


def test_the_cost_is_reported_with_the_caveat_every_dollar_figure_carries():
    text = mcp.render_answer(_envelope(
        "answered", text="x", citations=[{"locator": "L", "claim": "c"}],
        spend=[{"usd": 0.01}, {"usd": 0.07}]))
    assert "$0.080000" in text and "third-party multipliers" in text


def test_running_out_of_patience_hands_back_the_id_and_never_says_failed():
    """The recorded `ASK_TIMEOUT` incident in its new shape: an answer computed,
    billed, and reported as lost. The turn keeps going in the worker either
    way, so the only honest thing to return is the way to pick it up."""
    out = _dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "ask", "arguments": {
             "question": "¿Quién fue Jesucristo?", "library_id": "lib_teologia"}}},
        ask=lambda q, l, e: ("ask-123", None, 120.0),
    )
    body = out["result"]["content"][0]["text"]
    assert "isError" not in out["result"]
    assert "ask-123" in body and "collect" in body
    # Not merely "does not say failed": it has to *deny* it. A model reading
    # "still being answered" with no more than that may still decide the call
    # went wrong and tell the user so.
    assert "has not failed" in body
    assert "twice" in body, "it must say re-asking pays for the same question twice"


def test_a_control_api_that_is_down_is_an_error_the_model_can_relay():
    """`ToolFailed` becomes an `isError` result rather than a JSON-RPC error, so
    the model sees what went wrong instead of the call vanishing into the
    transport — and so a fault never renders like a refusal."""
    def boom(q, l, e):
        raise mcp.ToolFailed("The control API at http://127.0.0.1:8787 did not answer")
    out = _dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "ask", "arguments": {
             "question": "x", "library_id": "lib_teologia"}}},
        ask=boom)
    assert out["result"]["isError"] is True
    assert "did not answer" in out["result"]["content"][0]["text"]


def test_a_bad_effort_is_refused_in_a_sentence_rather_than_a_validation_dump():
    out = _dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "ask", "arguments": {
             "question": "x", "library_id": "lib_teologia", "effort": "exhaustivo"}}})
    assert out["result"]["isError"] is True
    assert "brief, standard, thorough" in out["result"]["content"][0]["text"]


@pytest.mark.parametrize("args", [
    {"library_id": "lib_teologia"},
    {"question": "x"},
    {},
])
def test_a_call_missing_either_required_argument_says_so(args):
    out = _dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "ask", "arguments": args}})
    assert out["result"]["isError"] is True


def test_the_runs_outcome_is_not_the_answers_state():
    """The defect the first live call found, pinned with the real envelope.

    `GET /ask/{id}` reports the *run* as `done` for every question that
    finished, refusals included. Reading that instead of the answer's own state
    rendered a real answer — prose and two verified citations — as
    "No answer (done)": the one failure this surface cannot have, a corpus made
    to look silent when it had spoken."""
    text = mcp.render_answer(_envelope(
        "answered",
        text="El texto define la procrastinación como el hábito de posponer.",
        citations=[{"locator": "Procrastinacion · p. 1 · [0:1012]", "claim": "posponer"},
                   {"locator": "Procrastinacion · p. 3 · [6063:7262]", "claim": "hábito"}]))
    assert "No answer" not in text
    assert "2 verified" in text
    assert "Procrastinacion · p. 1" in text
