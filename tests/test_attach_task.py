"""A session's attach outlives the sockets that wait for it, against the real bridge and discord.py (#148 PR 4)."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import discord

from discord_blue.doodads.agent_session import bridge as bridge_module
from tests.fake_discord import BOT_ID, FakeDiscord
from tests.test_attach_scenarios import Scenario, hello_for, marker, scenario, until

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

    async def test_a_connection_joining_after_the_thread_is_bound_is_bound_before_its_ack(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("late-joiner")
        thread = fake.add_thread("late-joiner", marker=marker(hello))
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            backfilling, release = asyncio.Event(), asyncio.Event()
            original = running.bridge.backfill_latest_assistant_message

            async def slow_backfill(*args: object) -> None:
                backfilling.set()
                await release.wait()
                await original(*args)  # type: ignore[arg-type]

            with patch.object(running.bridge, "backfill_latest_assistant_message", new=slow_backfill):
                first = await running.connect(http)
                await first.send_json(hello)
                await asyncio.wait_for(backfilling.wait(), timeout=5)  # Bound to the first connection by now.
                second = await running.connect(http)
                await second.send_json({**hello, "session_epoch": "e2"})
                await asyncio.sleep(0.1)
                release.set()
                ack = await second.receive_json(timeout=10)
                note = {"type": "notice", "session_id": "late-joiner", "session_epoch": "e2", "message": "from the joiner"}
                await second.send_json(note)
                delivered = await until(lambda: any(m.content == "from the joiner" for m in thread.messages), 5)
                await second.close()
                await first.close()

        self.assertEqual(ack["thread_id"], thread.id)
        self.assertTrue(delivered, "the acknowledged connection's events never reached its thread")

    async def test_a_grace_that_ran_out_while_the_reconnect_waited_still_closes_the_thread(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("unlucky")
        thread = fake.add_thread("unlucky", marker=marker(hello), members={BOT_ID})
        refused = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Discord is refusing")
        with patch.object(bridge_module, "SESSION_DISCONNECT_GRACE_SECONDS", 0.3):
            async with scenario(fake) as running, aiohttp.ClientSession() as http:
                first = await running.connect(http)
                await first.send_json(hello)
                await first.receive_json(timeout=10)
                await first.close()
                self.assertTrue(await until(lambda: running.bridge.sessions.by_thread != {} and _in_grace(running), 5))
                # Another session's slow attach holds the attach lock; the reconnect queues behind it.
                await running.bridge._session_attach_lock.acquire()
                second = await running.connect(http)
                await second.send_json({**hello, "session_epoch": "e2"})
                await asyncio.sleep(0.5)  # The old session's grace runs out meanwhile.
                with (
                    patch.object(running.bridge, "resume_thread_in_grace", new=AsyncMock(return_value=None)),
                    patch.object(running.bridge, "find_or_create_session_thread", new=AsyncMock(side_effect=refused)),
                ):
                    running.bridge._session_attach_lock.release()
                    answer = await second.receive(timeout=10)
                closed = await until(lambda: thread.archived, timeout=5)

        self.assertEqual(answer.type, aiohttp.WSMsgType.CLOSE)
        self.assertTrue(closed, "nobody closed the thread of a session whose grace ran out")
        self.assertEqual(running.bridge.sessions.by_thread, {})

    async def test_a_failed_backfill_still_attaches_a_late_joiner(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("no-history")
        thread = fake.add_thread("no-history", marker=marker(hello))
        refused = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "cannot post")
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            backfilling, release = asyncio.Event(), asyncio.Event()

            async def failing_backfill(*_args: object) -> None:
                backfilling.set()
                await release.wait()
                raise refused

            with patch.object(running.bridge, "backfill_latest_assistant_message", new=failing_backfill):
                first = await running.connect(http)
                await first.send_json(hello)
                await asyncio.wait_for(backfilling.wait(), timeout=5)
                second = await running.connect(http)
                await second.send_json({**hello, "session_epoch": "e2"})
                await asyncio.sleep(0.1)
                release.set()
                ack = await second.receive_json(timeout=10)
                owner = running.bridge.sessions.get("no-history")
                await second.close()
                await first.close()

        self.assertEqual(ack["thread_id"], thread.id)
        self.assertEqual(owner.thread_id if owner is not None else None, thread.id)

    async def test_stopping_with_a_reconnect_queued_still_closes_the_old_sessions_thread(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("unlucky")
        thread = fake.add_thread("unlucky", marker=marker(hello), members={BOT_ID})
        async with scenario(fake, listen_host="127.0.0.1", listen_port=0) as running, aiohttp.ClientSession() as http:
            await running.bridge.start()
            first = await running.connect(http)
            await first.send_json(hello)
            await first.receive_json(timeout=10)
            await first.close()
            self.assertTrue(await until(lambda: _in_grace(running), 5))
            # Another session's slow attach holds the attach lock; the reconnect queues behind it.
            await running.bridge._session_attach_lock.acquire()
            second = await running.connect(http)
            await second.send_json({**hello, "session_epoch": "e2"})
            await asyncio.sleep(0.2)
            stopping = asyncio.create_task(running.bridge.stop())
            await asyncio.sleep(0.1)
            running.bridge._session_attach_lock.release()
            await asyncio.wait_for(stopping, timeout=15)
            closed = await until(lambda: thread.archived, timeout=5)

        self.assertTrue(closed, "shutdown left the old session's thread open")


def _in_grace(running: Scenario) -> bool:
    session = running.bridge.sessions.get("unlucky")
    return session is not None and session.grace_task is not None


if __name__ == "__main__":
    unittest.main()
