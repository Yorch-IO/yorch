"""The Model Context Protocol surface: what an agent may ask, and get back.

**It serves answers, never chunks, and that is the whole reason it exists in
this shape.** An MCP tool that hands raw passages to somebody else's model
exports the retrieval and leaves behind the one property that makes this a
product rather than a vector database: `answer._verify` drops any citation
naming a chunk the model was not given, and an answer left with none is
downgraded rather than shipped. A `retrieve` tool would put unverified text into
another model's context under this corpus's name. So the tool is `ask`, it goes
through the same two-call `/ask` contract and the same effort ladder the app
uses, and every citation it returns has already survived verification.

**Localhost only, over the free plane, on stdio — a decision on the record.**
The free plane has no authentication and is safe only because nothing off the
machine can route to it, so a hosted MCP server would be the first externally
reachable surface this product has ever had. The paid-plane alternative needs an
auth story that does not exist here: MCP's remote transport authenticates with
OAuth, and this product's own PKCE sign-in has never run. stdio has no port at
all, which makes it strictly safer than what is already running. And it is a
developer's and an owner's tool, which is the same argument that put the
retrieval probe on the free plane.

**No dependency.** JSON-RPC 2.0 over newline-delimited stdio is a few hundred
lines of stdlib, and `worker/pyproject.toml` keeps a "no compiler in the image"
property that the EPUB writer already honours for the same reason.

Everything here is pure: the framing, the dispatch and the rendering. The half
that talks to a control API lives in `scripts/mcp_server.py`, the same split
`probing.py`/`probe_retrieval.py` and `auditversion.py`/`audit_version.py` use,
and for the same reason — what is worth asserting is the protocol and the
wording, and a test that needed a stack standing up to check a JSON envelope
would run rarely enough to be worth nothing.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

#: The revisions whose shapes this server actually implements. A client asking
#: for one of these is answered in its own version; anything else is answered in
#: `PROTOCOL` and the client decides whether it can proceed, which is what the
#: specification asks a server to do rather than guessing.
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
PROTOCOL = SUPPORTED[0]

SERVER_NAME = "company-brain"
SERVER_VERSION = "0.1.0"

#: The levels, by name and never by number. The wire carrying a level rather
#: than a table of widths is the rule `effort.py` already states: a client that
#: sent figures could ask for two hundred chunks on a paid call.
EFFORTS = ("brief", "standard", "thorough")
DEFAULT_EFFORT = "standard"

# JSON-RPC 2.0
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolFailed(Exception):
    """A tool could not run at all — not a refusal, which is a result.

    The distinction is the same one `off_corpus` draws against a 500: "this
    library does not cover your question" is an answer a calling model should
    relay, and "the control API is not running" is a fault it should report.
    Collapsing them would teach a model to treat an honest refusal as a bug and
    retry it, which is the one thing that costs money for nothing.
    """


def tool_schema(libraries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The `ask` tool, described from the libraries this installation holds.

    Built at `tools/list` time from the live list rather than written down, so a
    calling model can name a library without a second round trip — the one idea
    worth taking outright from RAGFlow's MCP server.

    It goes one step further than theirs, and the step is free: the ids go in
    the schema's `enum`, not only in the prose. A model cannot then name a
    library that does not exist, where a description can only ask it not to.
    An **empty** enum would be a schema nothing can satisfy, so a server with no
    libraries omits it and says so in the description instead — an installation
    with nothing indexed must still be able to say that out loud.
    """
    ids = [str(lib["id"]) for lib in libraries if lib.get("id")]
    library: dict[str, Any] = {
        "type": "string",
        "description": "Which library to ask. " + (
            _catalogue(libraries) if ids
            else "This installation has no libraries yet, so any value is refused."
        ),
    }
    if ids:
        library["enum"] = ids
    return {
        "name": "ask",
        "description": _describe(libraries),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, in the library's own language.",
                },
                "library_id": library,
                "effort": {
                    "type": "string",
                    "enum": list(EFFORTS),
                    "default": DEFAULT_EFFORT,
                    "description": (
                        "How much evidence to read. `brief` is fastest and "
                        "cheapest, `thorough` costs roughly three times a "
                        "`standard` question and returns several times the "
                        "citations."
                    ),
                },
            },
            "required": ["question", "library_id"],
        },
    }


def collect_schema() -> dict[str, Any]:
    """The second half of the two-call contract, as its own tool.

    `ask` waits, but a real question against a large library has outrun a
    180-second client timeout before, and the recorded cost of treating that as
    a failure is an answer that was computed, was billed, and was reported as
    lost. So when the wait runs out the answer is still coming, the id is handed
    back, and this is what fetches it.
    """
    return {
        "name": "collect",
        "description": (
            "Fetch an answer that `ask` started but did not finish waiting for. "
            "Takes the question_id `ask` returned. The answer is still being "
            "computed and has already been paid for — never re-ask instead, "
            "which pays for the same question twice."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question_id": {"type": "string"},
            },
            "required": ["question_id"],
        },
    }


def _catalogue(libraries: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for lib in libraries:
        name = lib.get("name") or lib["id"]
        indexed = lib.get("indexed_versions")
        docs = lib.get("documents")
        bits = [f"{lib['id']} ({name})"]
        if docs is not None:
            bits.append(f"{docs} documents")
        if indexed is not None:
            bits.append(f"{indexed} indexed")
        if lib.get("language"):
            bits.append(str(lib["language"]))
        parts.append(" — ".join([bits[0], ", ".join(bits[1:])]) if len(bits) > 1 else bits[0])
    return "Available: " + "; ".join(parts) + "."


def _describe(libraries: Sequence[Mapping[str, Any]]) -> str:
    return (
        "Ask a question of a Company Brain library and get an answer whose "
        "every citation has been checked against the passage it names — a "
        "citation naming a passage the model was not shown is dropped, and an "
        "answer left with none is refused rather than returned.\n\n"
        + _catalogue(libraries) + "\n\n"
        "Two refusals are results and not errors: `off_corpus` means nothing in "
        "the library cleared the similarity floor, so the question is about "
        "something this corpus does not hold; `insufficient_evidence` means the "
        "library was searched and did not support an answer. Relay either to "
        "the user rather than re-asking, and never present the corpus as silent "
        "when it has said which of the two it is.\n\n"
        "Answering costs money. A `standard` question is a few cents and "
        "`thorough` roughly three times that."
    )


def render_answer(payload: Mapping[str, Any]) -> str:
    """One text block: the answer, its verified citations, and what it cost.

    The citations are not optional and never travel separately. An answer
    printed without them is exactly the artefact this product refuses to make —
    grounded-looking prose with nothing to check — and a calling model that
    received only the prose would relay only the prose.

    **The answer's own state wins, and the envelope's is only a fallback.**
    `GET /ask/{id}` returns the *run's* outcome at the top level, which is
    `done` for every question that finished — `asking._record` maps a refusal
    onto a success deliberately, because "the corpus does not cover this" is an
    answer and not a fault. The three states a reader acts on (`answered`,
    `off_corpus`, `insufficient_evidence`) live on the answer. Reading the outer
    one rendered a real answer, with two verified citations and its prose, as
    "No answer (done)" — found on the first live call, and invisible to the
    tests, which used a hand-built envelope that agreed with the assumption
    rather than the one the plane sends.
    """
    answer = payload.get("answer") or {}
    state = answer.get("state") or payload.get("state") or "unknown"
    if state != "answered":
        reason = (answer.get("reason") or "").strip()
        detail = (payload.get("error") or {})
        if not reason and isinstance(detail, Mapping):
            reason = str(detail.get("message") or "").strip()
        head = {
            "off_corpus": (
                "No answer: nothing in this library cleared the similarity "
                "floor, so the question is about something this corpus does "
                "not hold."
            ),
            "insufficient_evidence": (
                "No answer: the library was searched and did not support one."
            ),
        }.get(state, f"No answer ({state}).")
        return head + (f"\n\n{reason}" if reason else "")

    lines = [(answer.get("text") or "").strip()]
    citations = answer.get("citations") or []
    if citations:
        lines.append(f"\nCitations ({len(citations)} verified):")
        for i, c in enumerate(citations, start=1):
            locator = (c.get("locator") or "").strip() or c.get("chunk_id") or "?"
            claim = (c.get("claim") or "").strip()
            lines.append(f"  [{i}] {locator}" + (f" — {claim}" if claim else ""))
    else:
        # Unreachable through `/ask`, which cannot emit `answered` with none.
        # Said out loud anyway: silence here would be the one failure mode this
        # whole surface exists to prevent.
        lines.append("\n(No citations survived verification.)")

    spend = answer.get("spend") or []
    usd = sum(float(s.get("usd") or 0.0) for s in spend if isinstance(s, Mapping))
    if usd:
        lines.append(
            f"\nCost: ${usd:.6f} (prices are third-party multipliers over "
            f"measured token counts)."
        )
    level = answer.get("style_effort") or answer.get("effort")
    if level:
        lines.append(f"Answered at `{level}`.")
    return "\n".join(lines).strip()


def render_pending(question_id: str, waited: float) -> str:
    """What `ask` says when the wait ran out. Never 'failed'."""
    return (
        f"Still being answered after {waited:.0f}s. It has not failed and it is "
        f"already paid for — call `collect` with question_id "
        f"\"{question_id}\" to pick it up. Re-asking pays for it twice."
    )


def negotiate(requested: Any) -> str:
    """The protocol revision to answer in."""
    return requested if requested in SUPPORTED else PROTOCOL


def dispatch(
    message: Mapping[str, Any],
    *,
    libraries: Callable[[], Sequence[Mapping[str, Any]]],
    ask: Callable[[str, str, str], tuple[str, Mapping[str, Any] | None, float]],
    collect: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Answer one JSON-RPC message, or `None` for a notification.

    `None` is the whole handling of a notification and it is load-bearing:
    JSON-RPC forbids a response to a message with no `id`, and a server that
    replied to `notifications/initialized` would have every strict client close
    the connection at the handshake.
    """
    if message.get("jsonrpc") != "2.0":
        return _error(message.get("id"), INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method = message.get("method")
    ident = message.get("id")
    if method is None:
        return _error(ident, INVALID_REQUEST, "no method")
    if ident is None:
        return None  # a notification: `initialized`, `cancelled`, anything else
    params = message.get("params") or {}

    if method == "initialize":
        return _ok(ident, {
            "protocolVersion": negotiate(params.get("protocolVersion")),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _ok(ident, {})
    if method == "tools/list":
        return _ok(ident, {"tools": [tool_schema(libraries()), collect_schema()]})
    if method == "tools/call":
        return _call(ident, params, ask=ask, collect=collect)
    return _error(ident, METHOD_NOT_FOUND, f"unknown method {method!r}")


def _call(ident: Any, params: Mapping[str, Any], *, ask, collect) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    try:
        if name == "ask":
            question = str(args.get("question") or "").strip()
            library_id = str(args.get("library_id") or "").strip()
            if not question or not library_id:
                return _ok(ident, _content(
                    "Both `question` and `library_id` are required.", error=True))
            effort = str(args.get("effort") or DEFAULT_EFFORT)
            if effort not in EFFORTS:
                # Refused here rather than sent on, because the free plane would
                # answer FastAPI's own 422-with-a-list and a calling model reads
                # a sentence better than a validation dump.
                return _ok(ident, _content(
                    f"`effort` must be one of {', '.join(EFFORTS)}.", error=True))
            state, payload, waited = ask(question, library_id, effort)
            if payload is None:
                return _ok(ident, _content(render_pending(state, waited)))
            return _ok(ident, _content(render_answer(payload)))
        if name == "collect":
            qid = str(args.get("question_id") or "").strip()
            if not qid:
                return _ok(ident, _content("`question_id` is required.", error=True))
            return _ok(ident, _content(render_answer(collect(qid))))
    except ToolFailed as e:
        # An `isError` result rather than a JSON-RPC error, which is what the
        # specification asks for: the model should see what went wrong and be
        # able to say so, not have the call vanish into the transport.
        return _ok(ident, _content(str(e), error=True))
    return _error(ident, INVALID_PARAMS, f"unknown tool {name!r}")


def _content(text: str, *, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        out["isError"] = True
    return out


def _ok(ident: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
