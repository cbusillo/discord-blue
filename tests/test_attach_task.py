"""A session's attach outlives the sockets that wait for it, against the real bridge and discord.py (#148 PR 4)."""

from __future__ import annotations

import asyncio
import unittest

import aiohttp

from tests.fake_discord import FakeDiscord
from tests.test_attach_scenarios import hello_for, marker, scenario, until

DISCOVERY = ("GET", "/channels/{channel}/threads/archived/public")


def slow_discovery() -> FakeDiscord:
    fake = FakeDiscord(latency=0.002)
    fake.route_latency[("GET", "/channels/{channel}/messages")] = 0.3  # Each candidate read is slow.
    return fake


class AttachTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_reconnect_joins_the_running_attach_instead_of_starting_over(self) -> None:
        fake = slow_discovery()
        hello = hello_for("patient")
        thread = fake.add_thread("patient", marker=marker(hello), archived=True, locked=True)
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            first = await running.connect(http)
            await first.send_json(hello)
            await asyncio.sleep(0.1)
            await first.close()  # The client stopped waiting for its ack and reconnects.
            second = await running.connect(http)
            await second.send_json({**hello, "session_epoch": "e2"})
            ack = await second.receive_json(timeout=10)
            await second.close()

        self.assertEqual(ack["thread_id"], thread.id)
        self.assertEqual(fake.requests.count(DISCOVERY), 1, "the reconnect started discovery again")

    async def test_only_the_newest_connection_gets_the_ack(self) -> None:
        fake = slow_discovery()
        hello = hello_for("twice")
        thread = fake.add_thread("twice", marker=marker(hello))
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            older = await running.connect(http)
            await older.send_json(hello)
            await asyncio.sleep(0.1)
            newer = await running.connect(http)
            await newer.send_json({**hello, "session_epoch": "e2"})
            ack = await newer.receive_json(timeout=10)
            answer = await older.receive(timeout=5)
            owner = running.bridge.sessions.get_by_thread(thread.id)
            await newer.close()

        self.assertEqual(ack["thread_id"], thread.id)
        self.assertEqual(answer.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(owner.session_epoch if owner is not None else None, "e2")

    async def test_a_socket_that_leaves_mid_attach_leaves_its_session_in_grace(self) -> None:
        fake = slow_discovery()
        hello = hello_for("gone")
        thread = fake.add_thread("gone", marker=marker(hello), archived=True, locked=True)
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello)
            await asyncio.sleep(0.1)
            await websocket.close()
            in_grace = await until(
                lambda: (session := running.bridge.sessions.get("gone")) is not None and session.grace_task is not None,
                timeout=10,
            )
            discoveries = fake.requests.count(DISCOVERY)
            later = await running.connect(http)
            await later.send_json({**hello, "session_epoch": "e2"})
            ack = await later.receive_json(timeout=10)
            await later.close()

        self.assertTrue(in_grace, "the attach finished for nobody and its session was dropped")
        self.assertEqual(ack["thread_id"], thread.id)
        self.assertEqual(fake.requests.count(DISCOVERY), discoveries, "the later hello searched for the thread again")
        self.assertFalse(thread.archived)


if __name__ == "__main__":
    unittest.main()
