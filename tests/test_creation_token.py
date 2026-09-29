"""A new thread's creation token, against the real bridge and discord.py (#148 PR 6b)."""

from __future__ import annotations

import unittest

import aiohttp

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


if __name__ == "__main__":
    unittest.main()
