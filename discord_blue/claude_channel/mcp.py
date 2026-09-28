"""A minimal stdio MCP server that Claude Code registers as a channel.

Claude Code starts one per session and speaks newline-delimited JSON-RPC 2.0 on
stdin/stdout. Only what a channel needs is implemented: ``initialize``,
``ping``, incoming notifications, and outgoing
notifications. The channel methods (``notifications/claude/channel*``) are
Claude Code extensions described in its channels reference.
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
METHOD_NOT_FOUND = -32601


class Output(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


NotificationHandler = Callable[[Json], Awaitable[None]]


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
        permission_relay: bool = True,
    ) -> None:
        self.reader, self.output = reader, output
        self.name, self.version, self.instructions = name, version, instructions
        self.permission_relay = permission_relay
        self.notification_handlers: dict[str, NotificationHandler] = {}
        self.write_lock = asyncio.Lock()

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self.notification_handlers[method] = handler

    async def notify(self, method: str, params: Json) -> None:
        await self.write({"jsonrpc": "2.0", "method": method, "params": params})

    async def write(self, message: Json) -> None:
        async with self.write_lock:
            self.output.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
            await self.output.drain()

    async def serve(self) -> None:
        """Answer requests until Claude Code closes stdin, which it does when the session ends."""
        while line := await self.reader.readline():
            try:
                message = json.loads(line)
            except ValueError:
                logger.warning("Ignoring a line that is not JSON")
                continue
            if isinstance(message, dict):
                await self.dispatch(message)

    async def dispatch(self, message: Json) -> None:
        method = message.get("method")
        raw_params = message.get("params")
        params: Json = raw_params if isinstance(raw_params, dict) else {}
        if "id" not in message:
            if isinstance(method, str) and (handler := self.notification_handlers.get(method)) is not None:
                await handler(params)
            return
        if not isinstance(method, str):
            return  # A response to a request this server never sends.
        try:
            result = await self.answer(method, params)
        except RpcFailure as exc:
            await self.write({"jsonrpc": "2.0", "id": message["id"], "error": {"code": exc.code, "message": str(exc)}})
        else:
            await self.write({"jsonrpc": "2.0", "id": message["id"], "result": result})

    async def answer(self, method: str, params: Json) -> Json:
        if method == "initialize":
            logger.info("Initialized by %s", params.get("clientInfo"))
            requested = params.get("protocolVersion")
            experimental: Json = {"claude/channel": {}}
            if self.permission_relay:
                experimental["claude/channel/permission"] = {}
            return {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"experimental": experimental},
                "serverInfo": {"name": self.name, "version": self.version},
                "instructions": self.instructions,
            }
        if method == "ping":
            return {}
        raise RpcFailure(METHOD_NOT_FOUND, f"Method not found: {method}")
