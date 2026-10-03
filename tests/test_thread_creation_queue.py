"""Thread-creation cooldowns through real discord.py and the session WebSocket."""

from __future__ import annotations

import asyncio
import time
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from unittest.mock import patch

import aiohttp

from discord_blue.agent_client import AgentSessionClient, Json
from discord_blue.doodads.agent_session import bridge as bridge_module
from tests.fake_discord import FakeDiscord, Fault
from tests.test_attach_scenarios import HOST, TOKEN, hello_for, scenario, until

CREATE = ("POST", "/channels/{channel}/threads")


@dataclass(frozen=True)
class Connection:
    server_url: str
    token: str = TOKEN
    host_label: str = HOST
    heartbeat_seconds: float = 1
    reconnect_seconds: float = 0.02
    hello_timeout_seconds: float = 0.5


class QueuedClient(AgentSessionClient):
    def __init__(self, url: str, session_id: str) -> None:
        super().__init__(Connection(url), session_id)
        self.hellos = 0

    def hello(self, *, first: bool) -> Json:
        self.hellos += 1
        return hello_for(self.session_id, epoch=self.epoch)


class ThreadCreationQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_rate_limited_create_keeps_the_socket_and_delivers_queued_events(self) -> None:
        fake = FakeDiscord()
        retry_after = 1.2  # Longer than the client's hello inactivity deadline.
        fake.faults.append(Fault(*CREATE, status=429, retry_after=retry_after))
        attempts: list[float] = []
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            # Scale the max sleep threshold too, so real discord.py raises RateLimited rather than sleeping.
            original = running.bot.parent.create_thread

            async def create(**kwargs: object) -> object:
                attempts.append(time.monotonic())
                with patch.object(running.bot.http, "max_ratelimit_timeout", 0.05):
                    return await original(**kwargs)

            client = QueuedClient(running.url, "waiting")
            client.publish("user_message", message="Queued while waiting")
            with (
                patch.object(bridge_module, "HELLO_PENDING_INTERVAL_SECONDS", 0.04),
                patch.object(running.bot.parent, "create_thread", create),
            ):
                task = asyncio.create_task(client.run(http))
                try:
                    delivered = await until(
                        lambda: any("Queued while waiting" in m.content for t in fake.threads.values() for m in t.messages), 5
                    )
                    self.assertTrue(delivered)
                    session = running.bridge.sessions.get(client.session_id)
                    self.assertIsNotNone(session)
                    assert session is not None
                    self.assertTrue(session.acknowledged)
                    self.assertFalse(session.websocket.closed)
                finally:
                    await client.stop()
                    await asyncio.wait_for(task, 2)
        self.assertEqual(client.hellos, 1, "waiting caused a client reconnect")
        self.assertEqual(len(attempts), 2)
        self.assertGreaterEqual(attempts[1] - attempts[0], retry_after)
        self.assertEqual(len(fake.threads), 1)
        self.assertEqual(running.archived, [])

    async def test_a_burst_is_serialized_through_repeated_creation_limits(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hellos = [hello_for(f"burst-{n}", client_features=["hello_pending"]) for n in range(7)]

        # A two-creation window: sessions 2, 4, and 6 hit the next window's limit once each.
        def matches(number: int) -> Callable[[Json], bool]:
            return lambda body: f"burst-{number} " in body.get("name", "")

        for number in (2, 4, 6):
            fake.faults.append(Fault(*CREATE, status=429, retry_after=0.2, body=matches(number)))
        active, peak = 0, 0
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            original = running.bot.parent.create_thread

            async def create(**kwargs: object) -> object:
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    with patch.object(running.bot.http, "max_ratelimit_timeout", 0.05):
                        return await original(**kwargs)
                finally:
                    active -= 1

            with (
                patch.object(bridge_module, "HELLO_PENDING_INTERVAL_SECONDS", 0.04),
                patch.object(running.bot.parent, "create_thread", create),
            ):
                sockets = [await running.connect(http) for _ in hellos]
                for websocket, hello in zip(sockets, hellos, strict=True):
                    await websocket.send_json(hello)

                async def acknowledged(websocket: aiohttp.ClientWebSocketResponse) -> Json:
                    while True:
                        message = await websocket.receive_json(timeout=5)
                        if message["type"] == "hello_ack":
                            return message
                        self.assertEqual(message["type"], "hello_pending")

                acks = await asyncio.gather(*(acknowledged(ws) for ws in sockets))
                self.assertTrue(all(not ws.closed for ws in sockets))
                for ws in sockets:
                    await ws.close()
        self.assertEqual(peak, 1)
        self.assertEqual(len({ack["thread_id"] for ack in acks}), len(hellos))
        self.assertEqual(len(fake.threads), len(hellos))
        self.assertEqual(fake.requests.count(CREATE), len(hellos) + 3)
        self.assertTrue(all(len(running.threads_marked_for(hello)) == 1 for hello in hellos))

    async def test_shutdown_wakes_a_long_creation_cooldown_without_retrying(self) -> None:
        fake = FakeDiscord()
        fake.faults.append(Fault(*CREATE, status=429, retry_after=300))
        async with scenario(fake, listen_host="127.0.0.1", listen_port=0) as running, aiohttp.ClientSession() as http:
            await running.bridge.start()
            running.bot.http.max_ratelimit_timeout = 0.05
            ws = await running.connect(http)
            await ws.send_json(hello_for("stopping"))
            self.assertTrue(await until(lambda: fake.requests.count(CREATE) == 1, 2))
            await asyncio.wait_for(running.bridge.stop(), 3)
            await ws.close()
        self.assertEqual(fake.requests.count(CREATE), 1)
        self.assertEqual(fake.threads, {})
