"""The shared discovery index, against the real bridge and discord.py (#148 PR 5)."""

from __future__ import annotations

import asyncio
import unittest

import aiohttp

from tests.fake_discord import FakeDiscord, Fault
from tests.test_attach_scenarios import hello_for, marker, scenario


class DiscoveryIndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_thread_created_moments_ago_is_found_by_the_next_hello(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("new")
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            first = await running.connect(http)
            await first.send_json(hello)
            created = await first.receive_json(timeout=10)
            await first.send_json({"type": "session_end", "session_id": "new", "session_epoch": "e1"})
            await first.close()
            await asyncio.sleep(0.2)  # Well inside the index's reuse window.
            second = await running.connect(http)
            await second.send_json({**hello, "session_epoch": "e2"})
            ack = await second.receive_json(timeout=10)
            await second.close()

        self.assertEqual(ack["thread_id"], created["thread_id"])
        self.assertEqual(len(fake.threads), 1, "the reconnect created a second thread")

    async def test_a_candidate_that_cannot_be_read_blocks_creation_instead_of_being_skipped(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("unreadable")
        existing = fake.add_thread("unreadable", marker=marker(hello), archived=True, locked=True)
        fake.faults.append(
            Fault(
                "GET", "/channels/{channel}/messages", times=1000, status=502, match=lambda ids: ids["channel"] == str(existing.id)
            )
        )
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello)
            answer = await websocket.receive(timeout=20)
            await websocket.close()

        self.assertEqual(answer.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(list(fake.threads), [existing.id], "discovery gave up on a candidate and created a thread")


if __name__ == "__main__":
    unittest.main()
