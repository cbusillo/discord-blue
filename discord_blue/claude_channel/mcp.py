"""A minimal stdio MCP server that Claude Code registers as a channel.

Claude Code starts one per session and speaks newline-delimited JSON-RPC 2.0 on
stdin/stdout. Only what a channel needs is implemented: ``initialize``,
``ping``, ``tools/list`` and ``tools/call``, and notifications both ways. The
channel method (``notifications/claude/channel``) is a Claude Code extension
described in its channels reference.

Each request runs on its own, since a hook call can wait for a Discord answer
while other hooks keep arriving; ``notifications/cancelled`` stops it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

Json = dict[str, Any]
logger = logging.getLogger(__name__)

# Revisions this server speaks. Claude Code does not register a channel on 2026-07-28 or later,
# which has no unsolicited notification path, so a newer request is answered with the newest listed.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LINE_LIMIT = 16 * 1024 * 1024
CANCELLED = "notifications/cancelled"
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class Output(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


NotificationHandler = Callable[[Json], Awaitable[None]]
# Called with the tool arguments and the request's `_meta`; returns the text result.
ToolHandler = Callable[[Json, Json], Awaitable[str]]


class RpcFailure(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class ChannelServer:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        output: Output,
        *,
        name: str,
        version: str,
        instructions: str,
    ) -> None:
        self.reader, self.output = reader, output
        self.name, self.version, self.instructions = name, version, instructions
        self.notification_handlers: dict[str, NotificationHandler] = {}
        self.tools: dict[str, tuple[Json, ToolHandler]] = {}
        self.write_lock = asyncio.Lock()
        self.requests: dict[object, asyncio.Task[None]] = {}

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self.notification_handlers[method] = handler

    def add_tool(self, spec: Json, handler: ToolHandler) -> None:
        self.tools[spec["name"]] = (spec, handler)

    async def notify(self, method: str, params: Json) -> None:
        await self.write({"jsonrpc": "2.0", "method": method, "params": params})

    async def write(self, message: Json) -> None:
        async with self.write_lock:
            self.output.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
            await self.output.drain()

    async def serve(self) -> None:
        """Answer requests until Claude Code closes stdin, which it does when the session ends."""
        try:
            while line := await self.reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    logger.warning("Ignoring a line that is not JSON")
                    continue
                if isinstance(message, dict):
                    await self.dispatch(message)
        finally:
            for task in self.requests.values():
                task.cancel()
            await asyncio.gather(*self.requests.values(), return_exceptions=True)

    async def dispatch(self, message: Json) -> None:
        method = message.get("method")
        raw_params = message.get("params")
        params: Json = raw_params if isinstance(raw_params, dict) else {}
        if "id" not in message:
            if method == CANCELLED and (task := self.requests.get(params.get("requestId"))) is not None:
                task.cancel()
            elif isinstance(method, str) and (handler := self.notification_handlers.get(method)) is not None:
                await handler(params)
            return
        if not isinstance(method, str):
            return  # A response to a request this server never sends.
        request_id = message["id"]
        task = asyncio.create_task(self.respond(request_id, method, params), name=f"mcp-{method}")
        self.requests[request_id] = task
        task.add_done_callback(lambda _task: self.requests.pop(request_id, None))

    async def respond(self, request_id: object, method: str, params: Json) -> None:
        # A cancelled request gets no response, as MCP specifies.
        try:
            result = await self.answer(method, params)
        except RpcFailure as exc:
            await self.write({"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": str(exc)}})
        else:
            await self.write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def answer(self, method: str, params: Json) -> Json:
        if method == "initialize":
            logger.info("Initialized by %s", params.get("clientInfo"))
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"experimental": {"claude/channel": {}}, **({"tools": {}} if self.tools else {})},
                "serverInfo": {"name": self.name, "version": self.version},
                "instructions": self.instructions,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [spec for spec, _handler in self.tools.values()]}
        if method == "tools/call":
            name, arguments, meta = params.get("name"), params.get("arguments"), params.get("_meta")
            if not isinstance(name, str) or name not in self.tools:
                raise RpcFailure(INVALID_PARAMS, f"Unknown tool: {name}")
            text = await self.tools[name][1](
                arguments if isinstance(arguments, dict) else {}, meta if isinstance(meta, dict) else {}
            )
            return {"content": [{"type": "text", "text": text}]}
        raise RpcFailure(METHOD_NOT_FOUND, f"Method not found: {method}")
