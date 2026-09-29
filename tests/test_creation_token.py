"""A new thread's creation token, against the real bridge and discord.py (#148 PR 6b)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import aiohttp

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.protocol import SessionHello
from discord_blue.doodads.agent_session.threads import session_thread_name
from tests.fake_discord import FakeDiscord, Fault
from tests.test_attach_scenarios import hello_for, scenario, until


class CreationTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_token_leaves_the_name_with_the_first_rename(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("fresh")
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello)
            ack = await websocket.receive_json(timeout=10)
            thread = fake.threads[ack["thread_id"]]
            plain = session_thread_name(SessionHello.from_payload(hello))
            renamed = await until(lambda: thread.name == plain, timeout=5)
            await websocket.close()

        self.assertTrue(renamed, f"the thread kept its creation token: {thread.name!r}")

    async def test_another_creations_thread_is_never_deleted(self) -> None:
        fake = FakeDiscord(latency=0.002)
        other = fake.add_thread("someone else · work [zzzzzz]")  # Another creation's token, still open.
        fake.faults.append(Fault("POST", "/channels/{channel}/threads", status=502, applied=True))
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello_for("fresh"))
            ack = await websocket.receive_json(timeout=15)
            await websocket.close()

        self.assertIn(other.id, fake.threads, "a thread carrying another creation's token was deleted")
        self.assertEqual(sorted(fake.threads), sorted([other.id, ack["thread_id"]]))

    async def test_a_thread_with_messages_is_never_deleted_even_with_the_token_in_its_name(self) -> None:
        fake = FakeDiscord(latency=0.002)
        copied = fake.add_thread("a discussion [abcdef]", marker="someone copied a session thread's name")
        fake.faults.append(Fault("POST", "/channels/{channel}/threads", status=502, applied=True))
        with patch.object(bridge_module, "new_creation_token", return_value="abcdef"):
            async with scenario(fake) as running, aiohttp.ClientSession() as http:
                websocket = await running.connect(http)
                await websocket.send_json(hello_for("fresh"))
                ack = await websocket.receive_json(timeout=15)
                await websocket.close()

        self.assertEqual(sorted(fake.threads), sorted([copied.id, ack["thread_id"]]))

    async def test_a_duplicate_check_that_failed_is_retried_by_maintenance(self) -> None:
        fake = FakeDiscord(latency=0.002)
        fake.faults.append(Fault("POST", "/channels/{channel}/threads", status=502, applied=True))
        # Listing active threads fails on every one of discord.py's five tries, once.
        fake.faults.append(Fault("GET", "/guilds/{guild}/threads/active", times=5, status=502))
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello_for("fresh"))
            ack = await websocket.receive_json(timeout=15)
            before = sorted(fake.threads)
            await running.bridge.retry_creation_checks()
            await websocket.close()

        self.assertEqual(len(before), 2, "the test needs the duplicate to survive the first check")
        self.assertEqual(sorted(fake.threads), [ack["thread_id"]])


if __name__ == "__main__":
    unittest.main()
