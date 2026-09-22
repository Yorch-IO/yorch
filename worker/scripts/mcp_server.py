#!/usr/bin/env python3
"""A Model Context Protocol server over stdio, serving answers from the corpus.

    cd worker
    uv run python scripts/mcp_server.py          # speaks MCP on stdin/stdout

Register it with an agent that speaks MCP over stdio, for example in Claude
Code's `.mcp.json`:

    {"mcpServers": {"company-brain": {
        "command": "uv",
        "args": ["run", "--project", "/home/kheiron/yorch/worker",
                 "python", "scripts/mcp_server.py"],
        "env": {"BRAIN_API_URL": "http://127.0.0.1:8787"}}}}

**It serves answers, never chunks**, and the reasoning for that and for the
localhost decision is in `brainworker/mcp.py`, which also holds every shape this
file sends. Here there is only the pump and the HTTP.

**stdout is the wire.** A stray `print` anywhere in this process corrupts the
protocol and the client reports a parse error with no sign of where it came
from, so everything this script says goes to stderr — including the failures.
That is also why it imports nothing that logs to stdout on the way in.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from brainworker.mcp import ToolFailed, dispatch  # noqa: E402

BASE = os.environ.get("BRAIN_API_URL", "http://127.0.0.1:8787").rstrip("/")

#: How long `ask` waits before handing the id back. Bounded rather than
#: unlimited because an MCP client has a timeout of its own and a tool that
#: never returns is worse than one that returns a way to carry on — and the
#: recorded `ASK_TIMEOUT` incident is exactly what the handing-back prevents:
#: an answer computed, billed, and reported as lost.
WAIT_SECONDS = float(os.environ.get("BRAIN_MCP_WAIT", "120"))
POLL_SECONDS = 1.0
HTTP_TIMEOUT = 30.0


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _request(method: str, path: str, body: Any = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("detail")
        except Exception:
            detail = raw
        # The plane's own `detail.kind`/`message` is the client contract, and it
        # is a better sentence than anything this file could invent — so it is
        # relayed rather than replaced.
        if isinstance(detail, dict):
            raise ToolFailed(str(detail.get("message") or detail)) from e
        raise ToolFailed(f"{BASE}{path} answered {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ToolFailed(
            f"The control API at {BASE} did not answer ({e.reason}). "
            f"It is the local stack: start it before asking."
        ) from e


def libraries() -> list[dict[str, Any]]:
    try:
        return list(_request("GET", "/libraries").get("libraries") or [])
    except ToolFailed as e:
        # `tools/list` must still answer: a client that cannot enumerate tools
        # drops the server entirely, and then the reason is invisible. The tool
        # exists with an empty catalogue and says so when called.
        log(f"could not list libraries: {e}")
        return []


def ask(question: str, library_id: str, effort: str):
    started = _request("POST", "/ask", {
        "text": question, "library_id": library_id, "effort": effort,
    })
    qid = started.get("question_id")
    if not qid:
        raise ToolFailed(f"/ask returned no question_id: {started}")
    t0 = time.monotonic()
    while time.monotonic() - t0 < WAIT_SECONDS:
        time.sleep(POLL_SECONDS)
        payload = _request("GET", f"/ask/{urllib.parse.quote(qid)}")
        if payload.get("state") not in (None, "running"):
            return qid, payload, time.monotonic() - t0
    return qid, None, time.monotonic() - t0


def collect(question_id: str) -> dict[str, Any]:
    payload = _request("GET", f"/ask/{urllib.parse.quote(question_id)}")
    if payload.get("state") in (None, "running"):
        raise ToolFailed(
            f"{question_id} is still being answered. Call `collect` again; it "
            f"is already paid for."
        )
    return payload


def main() -> int:
    if urllib.parse.urlparse(BASE).hostname not in ("127.0.0.1", "localhost", "::1"):
        # Warned, not refused: pointing this at a remote free plane exposes
        # nothing that plane was not already exposing to anyone who can route
        # to it, so refusing would be this script making a deployment decision
        # that is not its own. Saying so is the part that is.
        log(f"warning: {BASE} is not loopback, and the free plane has no "
            f"authentication.")
    log(f"company-brain MCP server on stdio, control API {BASE}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"unparseable message: {e}")
            continue
        try:
            response = dispatch(
                message, libraries=libraries, ask=ask, collect=collect)
        except Exception as e:  # never take the transport down with a tool
            log(f"dispatch failed: {type(e).__name__}: {e}")
            ident = message.get("id") if isinstance(message, dict) else None
            if ident is None:
                continue
            response = {"jsonrpc": "2.0", "id": ident,
                        "error": {"code": -32603, "message": str(e)}}
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
