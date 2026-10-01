"""The shared session client's outbox, against a stand-in agent-session server."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from discord_blue.agent_client import OUTBOX_LIMIT, AgentSessionClient, Json
from tests.fakes_discord_blue import TOKEN, FakeDiscordBlue


@dataclass(frozen=True)
class Connection:
    server_url: str
    token: str = TOKEN
    host_label: str = "test"
    heartbeat_seconds: float = 30
    reconnect_seconds: float = 0.05
    hello_timeout_seconds: float = 5


class DroppingClient(AgentSessionClient):
    """A client whose connection breaks while it sends the first turn_complete."""

    def __init__(self, config: Connection) -> None:
        super().__init__(config, "session-1")
        self.dropped = False

    def hello(self, *, first: bool) -> Json:
        return self.event("hello")

    async def send(self, websocket: aiohttp.ClientWebSocketResponse, message: Json) -> None:
        if message["type"] == "turn_complete" and not self.dropped:
            self.dropped = True
            raise ConnectionResetError("connection reset while sending")
        await super().send(websocket, message)


class FloodingClient(AgentSessionClient):
    """A client that queues a full outbox while its first event is still being sent."""

    def __init__(self, config: Connection) -> None:
        super().__init__(config, "session-1")
        self.flooded = False

    def hello(self, *, first: bool) -> Json:
        return self.event("hello")

    async def send(self, websocket: aiohttp.ClientWebSocketResponse, message: Json) -> None:
        if message["type"] == "status_changed" and not self.flooded:
            self.flooded = True
            for number in range(OUTBOX_LIMIT):
                self.publish("assistant_message", text=str(number))
        await super().send(websocket, message)


async def served(discord: FakeDiscordBlue) -> TestServer:
    app = web.Application()
    app.router.add_get("/agent-session/connect", discord.connect)
    return TestServer(app, host="127.0.0.1")


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_event_whose_send_fails_is_delivered_after_the_reconnect(self) -> None:
        discord = FakeDiscordBlue()
        async with await served(discord) as server, aiohttp.ClientSession() as http:
            client = DroppingClient(Connection(f"ws://127.0.0.1:{server.port}/agent-session/connect"))
            running = asyncio.create_task(client.run(http))
            try:
                await discord.next("hello")
                client.publish("turn_complete", assistant_message="The final answer")
                await discord.next("hello")  # The broken send ended the connection; the client reconnected.
                delivered = await discord.next("turn_complete")
            finally:
                await client.stop()
                await asyncio.wait_for(running, 5)

        self.assertTrue(client.dropped)
        self.assertEqual(delivered["assistant_message"], "The final answer")

    async def test_events_queued_during_a_send_that_overflows_the_outbox_are_all_delivered(self) -> None:
        discord = FakeDiscordBlue()
        async with await served(discord) as server, aiohttp.ClientSession() as http:
            client = FloodingClient(Connection(f"ws://127.0.0.1:{server.port}/agent-session/connect"))
            running = asyncio.create_task(client.run(http))
            try:
                await discord.next("hello")
                client.publish("status_changed", message="Working")
                await discord.next("status_changed")
                texts = [(await discord.next("assistant_message"))["text"] for _ in range(OUTBOX_LIMIT)]
            finally:
                await client.stop()
                await asyncio.wait_for(running, 5)

        self.assertEqual(texts, [str(number) for number in range(OUTBOX_LIMIT)])
