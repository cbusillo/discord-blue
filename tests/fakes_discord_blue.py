"""A stand-in for the deployed agent-session server, for tests of the local bridges."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

Json = dict[str, Any]
TOKEN = "test-token"
RESPONSES = ("command_ack", "command_reject", "approval_decision_ack", "approval_decision_reject")


class FakeDiscordBlue:
    """Stands in for the deployed agent-session server: acks hello and records events."""

    def __init__(self, features: list[str] | None = None) -> None:
        # None acknowledges like a server that predates hello_ack features.
        self.features = features
        self.received: asyncio.Queue[Json] = asyncio.Queue()
        self.sockets: list[web.WebSocketResponse] = []

    async def connect(self, request: web.Request) -> web.WebSocketResponse:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        self.sockets.append(websocket)
        async for frame in websocket:
            message = frame.json()
            if message["type"] == "hello":
                ack: Json = {"type": "hello_ack", "thread_id": 1}
                await websocket.send_json(ack if self.features is None else {**ack, "features": self.features})
            if message["type"] != "heartbeat":
                await self.received.put(message)
        return websocket

    async def close(self) -> None:
        for websocket in self.sockets:
            await websocket.close()

    async def next(self, *kinds: str, timeout: float = 5) -> Json:
        while True:
            message = await asyncio.wait_for(self.received.get(), timeout=timeout)
            if not kinds or message["type"] in kinds:
                return message

    async def control(self, message: Json) -> Json:
        await self.sockets[-1].send_json(message)
        return await self.next(*RESPONSES)
