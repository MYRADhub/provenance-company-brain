"""The brain's tools as a Model Context Protocol server, so another app's assistant (Tessel's first) can use them.

Streamable HTTP transport, stateless: every request is one JSON-RPC message POSTed to /mcp and answered with
one JSON body. There is no server-to-client stream, so GET is refused as the spec allows.

Identity comes from a bearer token (app/tokens.py), never from the client's arguments, and every tool runs
through the same `agent._run_tool` as the brain's own assistant: ethical walls apply unchanged, and a walled
deal looks exactly like one that does not exist. No LLM runs here; the client's model reads the results.
"""

import json
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import agent, governance, tokens

SERVER_NAME = "Provenance company brain"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")

# Browsers send an Origin; a page on another site must not reach a brain running on localhost (DNS
# rebinding). Desktop clients and servers send none. Extra origins: MCP_ALLOWED_ORIGINS, comma separated.
LOCAL_ORIGIN = re.compile(r"^(https?://(localhost|127\.0\.0\.1|\[::1\]|tauri\.localhost)(:\d+)?|tauri://localhost)$")

INSTRUCTIONS = (
    "Tools over the firm's model history and documents. Database tools (list_my_deals, list_models, "
    "get_model_graph, diff_commits, find_stale_references) give facts: what changed in each Excel model, when "
    "and by whom. search_brain gives the reasons, from memos, calls and emails. Join them: take the fact, then "
    "find and cite the reason. A 'restricted' result means the user is not cleared for that, or it does not "
    "exist; never guess at what it might be."
)

router = APIRouter()


def _allowed_origin(origin: str | None) -> bool:
    if not origin:
        return True
    extra = {o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()}
    return origin in extra or bool(LOCAL_ORIGIN.match(origin))


def _absolute_links(value, base: str):
    """Make the brain's links (/files/..., #commit=...) usable from another app."""
    if isinstance(value, dict):
        return {
            k: (base + v if k == "url" and isinstance(v, str) and v.startswith("/") else
                base + "/" + v if k == "url" and isinstance(v, str) and v.startswith("#") else
                _absolute_links(v, base))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_absolute_links(v, base) for v in value]
    return value


def tool_list() -> list[dict]:
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"],
         "annotations": {"readOnlyHint": True, "openWorldHint": False}}
        for t in agent.TOOLS
    ]


TOOL_NAMES = {t["name"] for t in agent.TOOLS}


def _result(id_, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


def _error(id_, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}, status_code=status)


def _unauthorized() -> Response:
    return JSONResponse(
        {"error": "A valid bearer token is required. Create one with: uv run python scripts/api_token.py create <email>"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="provenance"'},
    )


def call_tool(email: str, name: str, args: dict, base: str) -> dict:
    """Run one tool for a user and shape the outcome as an MCP tool result."""
    if name not in TOOL_NAMES:
        return {"content": [{"type": "text", "text": f"Unknown tool {name}"}], "isError": True}
    try:
        output, summary = agent._run_tool(email, name, args, {})
    except Exception as e:  # reported to the client's model so it can recover, as the brain's own agent does
        governance.log(email, "mcp_tool", f"{name} failed: {e}", "error")
        return {"content": [{"type": "text", "text": f"The tool failed: {e}"}], "isError": True}
    blocked = summary == "restricted"
    governance.log(email, "blocked" if blocked else "mcp_tool", f"{name} {json.dumps(args)}", "blocked" if blocked else "ok")
    text = json.dumps(_absolute_links(output, base), default=str)
    return {"content": [{"type": "text", "text": text}], "isError": False}


@router.post("/mcp")
async def mcp_post(request: Request) -> Response:
    if not _allowed_origin(request.headers.get("origin")):
        return JSONResponse({"error": "Origin not allowed"}, status_code=403)
    auth = request.headers.get("authorization", "")
    email = tokens.user_for(auth[7:].strip()) if auth[:7].lower() == "bearer " else None
    if not email:
        return _unauthorized()

    try:
        message = json.loads(await request.body())
    except ValueError:
        return _error(None, -32700, "Parse error", 400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request", 400)

    method, params, id_ = message["method"], message.get("params") or {}, message.get("id")
    if id_ is None:  # a notification (notifications/initialized, cancellations): nothing to answer
        return Response(status_code=202)
    if not isinstance(params, dict):
        return _error(id_, -32602, "params must be an object")

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(id_, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": tool_list()})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _error(id_, -32602, "tools/call needs a name and an arguments object")
        base = str(request.base_url).rstrip("/")
        return _result(id_, call_tool(email, name, args, base))
    return _error(id_, -32601, f"Method not found: {method}")


@router.get("/mcp")
def mcp_get() -> Response:
    # No server-initiated messages, so no event stream to open.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
def mcp_delete() -> Response:
    # Stateless: there is no session to end.
    return Response(status_code=405, headers={"Allow": "POST"})
