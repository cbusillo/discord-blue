from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

import discord

from tests.discord_http import discord_bot, scaled_discord_sleeps
from tests.fake_discord import BOT_ID, PARENT_ID, FakeDiscord, Fault


class FakeDiscordTests(unittest.IsolatedAsyncioTestCase):
    """The fake behaves like Discord where the attach scenarios depend on it, through the real discord.py client."""

    async def test_discord_py_waits_out_a_rate_limit_and_retries(self) -> None:
        fake = FakeDiscord()
        state = fake.add_thread("t")
        fake.faults.append(Fault("PATCH", "/channels/{channel}", status=429, retry_after=0.2))
        async with discord_bot(fake, SimpleNamespace()) as bot:
            thread = await bot.fetch_thread(state.id)
            started = time.monotonic()
            await thread.edit(name="renamed")

        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(state.name, "renamed")
        self.assertEqual(fake.requests.count(("PATCH", "/channels/{channel}")), 2)

    async def test_a_create_that_fails_after_taking_effect_is_retried_into_a_duplicate(self) -> None:
        fake = FakeDiscord()
        fake.faults.append(Fault("POST", "/channels/{channel}/threads", status=502, applied=True))
        with scaled_discord_sleeps(0.01):
            async with discord_bot(fake, SimpleNamespace()) as bot:
                created = await bot.parent.create_thread(name="session")

        # discord.py retried the 502 and reported only the second thread.
        self.assertEqual(len(fake.threads), 2)
        self.assertIn(created.id, fake.threads)

    async def test_the_channel_cache_follows_archive_and_reopen_events(self) -> None:
        fake = FakeDiscord(gateway_delay=0.05)
        state = fake.add_thread("t")
        async with discord_bot(fake, SimpleNamespace()) as bot:
            thread = await bot.fetch_thread(state.id)
            await thread.edit(archived=True, locked=True)
            await asyncio.sleep(0.1)
            archived = bot.get_channel(state.id)
            await thread.edit(archived=False, locked=False)
            right_after = bot.get_channel(state.id)
            await asyncio.sleep(0.1)
            later = bot.get_channel(state.id)

        # As in discord.py: an archived thread leaves the cache and returns only once the gateway says so.
        self.assertEqual((archived, right_after, later is not None), (None, None, True))

    async def test_archived_threads_are_paginated_and_joining_one_is_refused(self) -> None:
        fake = FakeDiscord()
        states = [fake.add_thread(f"t{n}", archived=True, members={BOT_ID}) for n in range(120)]
        async with discord_bot(fake, SimpleNamespace()) as bot:
            listed = [t.id async for t in bot.parent.archived_threads(private=True, joined=True, limit=None)]
            first_page = [t.id async for t in bot.parent.archived_threads(private=True, joined=True, limit=50)]
            with self.assertRaises(discord.HTTPException):
                await (await bot.fetch_thread(states[0].id)).join()

        self.assertEqual(listed, [s.id for s in reversed(states)])
        self.assertEqual(first_page, listed[:50])

    async def test_a_request_the_client_stops_waiting_for_still_lands(self) -> None:
        fake = FakeDiscord()
        state = fake.add_thread("t")
        fake.route_latency[("PATCH", "/channels/{channel}")] = 0.2
        async with discord_bot(fake, SimpleNamespace()) as bot:
            thread = await bot.fetch_thread(state.id)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(thread.edit(archived=True, locked=True), timeout=0.05)
            await asyncio.sleep(0.3)

        self.assertEqual((state.archived, state.locked), (True, True))

    async def test_messages_page_in_both_directions_and_deleted_threads_are_not_found(self) -> None:
        fake = FakeDiscord()
        state = fake.add_thread("t", marker="Agent session connected")
        async with discord_bot(fake, SimpleNamespace()) as bot:
            for n in range(150):
                await bot.parent.send(f"notice {n}")
            newest_first = [m.content async for m in bot.parent.history(limit=None)]
            oldest = [m.content async for m in (await bot.fetch_thread(state.id)).history(limit=10, oldest_first=True)]
            fake.delete_thread(state.id)
            with self.assertRaises(discord.NotFound):
                await bot.fetch_thread(state.id)

        self.assertEqual(newest_first, [f"notice {n}" for n in reversed(range(150))])
        self.assertEqual(oldest, ["Agent session connected"])
        self.assertEqual(PARENT_ID, bot.parent.id)
