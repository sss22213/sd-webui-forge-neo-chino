"""Minimal MCP server (Model Context Protocol, Streamable HTTP transport) for the WebUI.

No extra dependencies: only FastAPI/Starlette, which the WebUI already has (the official
`mcp` SDK needs a newer pydantic than the one this WebUI pins).

Implements what a tool server needs: initialize, ping, tools/list and tools/call. A call whose
request carries a progress token (and accepts text/event-stream) is answered as an SSE stream
with notifications/progress before the result; everything else is a plain JSON response.
The server is stateless: every POST stands on its own, there is no server-initiated GET stream
(405), and the Mcp-Session-Id handed out by initialize is accepted but not required.

Spec: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
"""
from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import secrets
import traceback
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    """Raise from a tool: the call returns isError=true with this message (the model sees it)."""


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def image(b64: str, mime: str = "image/png") -> dict:
    return {"type": "image", "data": b64, "mimeType": mime}


class Result:
    """What a tool returns: content blocks plus optional structured JSON."""

    def __init__(self, content: list[dict] | None = None, structured: Any = None, is_error: bool = False):
        self.content = content or []
        self.structured = structured
        self.is_error = is_error

    def to_json(self) -> dict:
        out: dict[str, Any] = {"content": self.content, "isError": self.is_error}
        if isinstance(self.structured, dict):
            out["structuredContent"] = self.structured
        return out


def _as_result(value: Any) -> dict:
    if isinstance(value, Result):
        return value.to_json()
    if isinstance(value, str):
        return Result([text(value)]).to_json()
    # anything else (dict, list, number ...) is data: JSON text for the model + structuredContent
    dumped = json.dumps(value, ensure_ascii=False, indent=1, default=str)
    return Result([text(dumped)], value if isinstance(value, dict) else {"result": value}).to_json()


class Context:
    """Passed to every tool: progress() sends notifications/progress when the client asked for them."""

    def __init__(self, token: Any, queue: asyncio.Queue | None):
        self._token = token
        self._queue = queue
        self._last = -1.0

    async def progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
        if self._token is None or self._queue is None:
            return
        progress = max(float(progress), self._last + 1e-3)  # the spec wants it to keep increasing
        self._last = progress
        params: dict[str, Any] = {"progressToken": self._token, "progress": progress}
        if total is not None:
            params["total"] = total
        if message:
            params["message"] = message
        await self._queue.put({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, Context], Any]
    annotations: dict | None = None

    def describe(self) -> dict:
        d = {"name": self.name, "description": self.description, "inputSchema": self.input_schema}
        if self.annotations:
            d["annotations"] = self.annotations
        return d


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _progress_token(msg: dict) -> Any:
    return ((msg.get("params") or {}).get("_meta") or {}).get("progressToken")


def _sse(payload: dict) -> str:
    return f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class McpServer:
    def __init__(self, name: str, version: str, *, instructions: str = "",
                 get_token: Callable[[], str] | None = None, is_enabled: Callable[[], bool] | None = None,
                 log: Callable[[str], None] = print):
        self.name = name
        self.version = version
        self.instructions = instructions
        self.get_token = get_token
        self.is_enabled = is_enabled
        self.log = log
        self.tools: dict[str, Tool] = {}

    def add_tool(self, name: str, description: str, input_schema: dict | None,
                 handler: Callable[[dict, Context], Any], annotations: dict | None = None) -> None:
        schema = dict(input_schema or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        self.tools[name] = Tool(name, description, schema, handler, annotations)

    def mount(self, app: FastAPI, path: str) -> None:
        app.add_api_route(path, self._post, methods=["POST"], include_in_schema=False)
        app.add_api_route(path, self._other, methods=["GET", "DELETE"], include_in_schema=False)

    # ---------------- HTTP ----------------

    def _authorized(self, request: Request) -> bool:
        token = self.get_token() if self.get_token else ""
        if not token:
            return True
        got = request.headers.get("authorization", "")
        if got[:7].lower() == "bearer ":
            got = got[7:].strip()
        else:  # for clients that cannot set an Authorization header
            got = request.headers.get("x-mcp-token") or request.query_params.get("token") or ""
        return hmac.compare_digest(got.encode(), token.encode())

    def _gate(self, request: Request) -> Response | None:
        if self.is_enabled and not self.is_enabled():
            return JSONResponse({"error": "the MCP server is turned off in Settings"}, status_code=404)
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized: send Authorization: Bearer <token>"}, status_code=401,
                                headers={"WWW-Authenticate": 'Bearer realm="mcp"'})
        return None

    async def _other(self, request: Request) -> Response:
        denied = self._gate(request)
        if denied:
            return denied
        if request.method == "DELETE":  # end of session: nothing is kept, so nothing to do
            return Response(status_code=200)
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    async def _post(self, request: Request) -> Response:
        denied = self._gate(request)
        if denied:
            return denied
        try:
            body = json.loads(await request.body())
        except ValueError:
            return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)
        batch = isinstance(body, list)
        calls = [m for m in (body if batch else [body]) if isinstance(m, dict) and "method" in m and "id" in m]
        if not calls:  # only notifications / responses
            return Response(status_code=202)
        headers = {}
        if not batch and calls[0].get("method") == "initialize":
            headers["Mcp-Session-Id"] = secrets.token_hex(16)
        accepts_sse = "text/event-stream" in request.headers.get("accept", "")
        if not batch and accepts_sse and calls[0].get("method") == "tools/call" and _progress_token(calls[0]) is not None:
            return StreamingResponse(self._stream(calls[0]), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        out = [await self._dispatch(m, None) for m in calls]
        return JSONResponse(out if batch else out[0], headers=headers)

    async def _stream(self, msg: dict):
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.ensure_future(self._dispatch(msg, queue))
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield _sse(getter.result())
                    continue
                getter.cancel()
                while not queue.empty():
                    yield _sse(queue.get_nowait())
                yield _sse(task.result())
                return
        finally:
            if not task.done():
                task.cancel()

    # ---------------- JSON-RPC ----------------

    async def _dispatch(self, msg: dict, queue: asyncio.Queue | None) -> dict:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        try:
            if method == "initialize":
                wanted = params.get("protocolVersion")
                result: dict[str, Any] = {
                    "protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                }
                if self.instructions:
                    result["instructions"] = self.instructions
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [t.describe() for t in self.tools.values()]}
            elif method == "tools/call":
                tool = self.tools.get(params.get("name"))
                if tool is None:
                    return _error(mid, -32602, f"Unknown tool: {params.get('name')}")
                result = await self._call(tool, params.get("arguments") or {}, Context(_progress_token(msg), queue))
            else:
                return _error(mid, -32601, f"Method not found: {method}")
        except Exception as e:  # noqa: BLE001
            return _error(mid, -32603, f"{type(e).__name__}: {e}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    async def _call(self, tool: Tool, args: dict, ctx: Context) -> dict:
        missing = [k for k in tool.input_schema.get("required") or [] if args.get(k) in (None, "")]
        if missing:
            return Result([text(f"Missing required argument(s): {', '.join(missing)}")], is_error=True).to_json()
        try:
            out = tool.handler(args, ctx)
            if inspect.isawaitable(out):
                out = await out
            return _as_result(out)
        except ToolError as e:
            return Result([text(str(e))], is_error=True).to_json()
        except Exception as e:  # noqa: BLE001
            self.log(f"MCP tool {tool.name} failed: {e}\n{traceback.format_exc()}")
            return Result([text(f"{tool.name} failed: {type(e).__name__}: {e}")], is_error=True).to_json()
