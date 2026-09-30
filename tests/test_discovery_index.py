"""The shared discovery index, against the real bridge and discord.py (#148 PR 5)."""

from __future__ import annotations

import asyncio
import unittest
from typing import cast

import aiohttp
import discord

from discord_blue.doodads.agent_session.discovery import DiscoveryIndex
from tests.fake_discord import BOT_ID, PARENT_ID, FakeDiscord, Fault
from tests.fakes_agent_session import FakeTextChannel, FakeThread
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

    async def test_an_incomplete_scan_never_settles_on_a_pid_relaxed_thread(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("twice", pid=2)
        older = fake.add_thread("older", marker=marker(hello_for("twice", pid=1)), archived=True, locked=True)
        current = fake.add_thread("current", marker=marker(hello), archived=True, locked=True)
        # The current thread's read fails twice (discord.py tries each read five times), then succeeds.
        fake.faults.append(
            Fault("GET", "/channels/{channel}/messages", times=10, status=502, match=lambda ids: ids["channel"] == str(current.id))
        )
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello)
            ack = await websocket.receive_json(timeout=20)
            await websocket.close()

        self.assertEqual(ack["thread_id"], current.id, "an incomplete scan settled on the older, pid-relaxed thread")
        self.assertNotEqual(ack["thread_id"], older.id)

    async def test_a_thread_the_bot_cannot_read_does_not_block_new_sessions(self) -> None:
        fake = FakeDiscord(latency=0.002)
        unreadable = fake.add_thread("someone else's", archived=True, locked=True)
        fake.faults.append(
            Fault(
                "GET", "/channels/{channel}/messages", times=1000, status=403, match=lambda ids: ids["channel"] == str(unreadable.id)
            )
        )
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello_for("brand-new"))
            ack = await websocket.receive_json(timeout=10)
            await websocket.close()

        self.assertEqual(ack["type"], "hello_ack")
        self.assertNotEqual(ack["thread_id"], unreadable.id)


class DiscoveryIndexUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_thread_this_bot_created_stays_known_while_listings_omit_it(self) -> None:
        now = [0.0]
        index = DiscoveryIndex(lambda: BOT_ID, clock=lambda: now[0])
        channel = FakeTextChannel(PARENT_ID, [])  # The gateway has not reported the new thread; no archive lists it.
        created = FakeThread(501)
        index.add(cast(discord.Thread, created), ["the session's marker"])
        now[0] += 60
        await index.fresh(cast(discord.TextChannel, channel))

        self.assertTrue(index.complete)
        self.assertEqual([entry.thread.id for entry in index.entries()], [created.id])


if __name__ == "__main__":
    unittest.main()
