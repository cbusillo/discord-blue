"""A dropped connection's grace period and a clean session_end, against the real bridge and discord.py (#148)."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import aiohttp

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.sessions import AgentSession
from tests.test_attach_scenarios import Scenario, hello_for, marker, scenario, until
from tests.fake_discord import BOT_ID, FakeDiscord, FakeThreadState, Fault

# Any of these on a session's thread is churn a reconnect within grace must not cause.
CHURN = {
    ("POST", "/channels/{channel}/messages"),
    ("PUT", "/channels/{channel}/thread-members/{member}"),
    ("DELETE", "/channels/{channel}/thread-members/{member}"),
    ("PATCH", "/channels/{channel}"),
}
OWNER_ID = 7


def notices(thread: FakeThreadState) -> list[str]:
    return [message.content for message in thread.messages if message.content == bridge_module.SESSION_ENDED_NOTICE]


async def attach(running: Scenario, http: aiohttp.ClientSession, hello: dict[str, object]) -> aiohttp.ClientWebSocketResponse:
    websocket = await running.connect(http)
    await websocket.send_json(hello)
    return websocket


async def in_grace(running: Scenario, session_id: str) -> AgentSession:
    session = running.bridge.sessions.get(session_id)
    assert session is not None
    assert await until(lambda: session.grace_task is not None, timeout=2)
    return session


class SessionGraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_reconnect_within_grace_resumes_the_thread_without_churn(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("drops")
        thread = fake.add_thread("drops", marker=marker(hello), private=True, members={BOT_ID, OWNER_ID})
        async with scenario(fake, auto_join_user_ids=[OWNER_ID]) as running, aiohttp.ClientSession() as http:
            first = await running.connect(http)
            await first.send_json(hello)
            await first.receive_json(timeout=10)
            await asyncio.sleep(0.3)  # Let the attach's own background work (renaming) settle.
            before = len(fake.requests)
            await first.close()
            session = running.bridge.sessions.get("drops")
            self.assertTrue(await until(lambda: session is not None and session.grace_task is not None, timeout=2))
            second = await running.connect(http)
            await second.send_json({**hello, "session_epoch": "e2"})
            ack = await second.receive_json(timeout=10)
            await asyncio.sleep(0.3)
            churn = [request for request in fake.requests[before:] if request in CHURN]
            await second.close()

        self.assertEqual(ack["thread_id"], thread.id)
        self.assertEqual(churn, [])
        self.assertEqual(running.archived, [])
        self.assertEqual(notices(thread), [])

    async def test_session_end_closes_the_thread_at_once_with_one_notice(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("finishes")
        thread = fake.add_thread("finishes", marker=marker(hello), members={BOT_ID})
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello)
            await websocket.receive_json(timeout=10)
            await websocket.send_json({"type": "session_end", "session_id": "finishes", "session_epoch": "e1"})
            await websocket.close()  # As the clients do right after session_end.
            archived = await until(lambda: thread.archived and thread.locked, timeout=5)

        self.assertTrue(archived, "a clean session_end did not archive and lock the thread")
        self.assertEqual(notices(thread), [bridge_module.SESSION_ENDED_NOTICE])
        self.assertIsNone(running.bridge.sessions.get("finishes"))

    async def test_grace_expiry_posts_one_notice_and_archives(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("gone")
        thread = fake.add_thread("gone", marker=marker(hello), members={BOT_ID})
        with patch.object(bridge_module, "SESSION_DISCONNECT_GRACE_SECONDS", 0.5):
            async with scenario(fake) as running, aiohttp.ClientSession() as http:
                websocket = await running.connect(http)
                await websocket.send_json(hello)
                await websocket.receive_json(timeout=10)
                await websocket.close()
                await asyncio.sleep(0.2)
                during_grace = (thread.archived, notices(thread))
                archived = await until(lambda: thread.archived and thread.locked, timeout=5)

        self.assertEqual(during_grace, (False, []))
        self.assertTrue(archived, "the thread stayed open after its grace period expired")
        self.assertEqual(notices(thread), [bridge_module.SESSION_ENDED_NOTICE])

    async def test_sweeps_leave_a_session_in_grace_alone(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("waiting")
        thread = fake.add_thread("waiting", marker=marker(hello), members={BOT_ID})
        async with (
            scenario(fake, heartbeat_timeout_seconds=0.1) as running,
            aiohttp.ClientSession() as http,
        ):
            websocket = await running.connect(http)
            await websocket.send_json(hello)
            await websocket.receive_json(timeout=10)
            await websocket.close()
            session = running.bridge.sessions.get("waiting")
            self.assertTrue(await until(lambda: session is not None and session.grace_task is not None, timeout=2))
            await asyncio.sleep(0.2)  # Past the heartbeat timeout.
            await running.bridge.close_timed_out_sessions()
            await running.bridge.cleanup_stale_session_threads()
            await asyncio.sleep(0.2)

        self.assertEqual(running.archived, [])
        self.assertEqual(notices(thread), [])
        self.assertIs(running.bridge.sessions.get("waiting"), session)

    async def test_an_expiry_archive_that_lands_late_does_not_close_the_reconnected_thread(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("late")
        thread = fake.add_thread("late", marker=marker(hello), members={BOT_ID})
        with patch.object(bridge_module, "SESSION_DISCONNECT_GRACE_SECONDS", 0.2):
            async with scenario(fake) as running, aiohttp.ClientSession() as http:
                first = await attach(running, http, hello)
                await first.receive_json(timeout=10)
                # Only the archive is slow: it outlives close_thread's 1 s wait and lands after the reconnect.
                fake.body_latency.append(("PATCH", "/channels/{channel}", lambda body: body.get("archived") is True, 2.0))
                await first.close()
                self.assertTrue(await until(lambda: running.bridge.sessions.get("late") is None, timeout=5))
                self.assertFalse(thread.archived, "the archive landed before the reconnect; the test proves nothing")
                second = await attach(running, http, {**hello, "session_epoch": "e2"})
                ack = await second.receive_json(timeout=10)
                landed = await until(lambda: thread.id in running.archived, timeout=5)
                await asyncio.sleep(0.2)  # Room for anything that would react to it.
                open_after_archive = not thread.archived and not thread.locked
                await second.close()

        self.assertEqual(ack["thread_id"], thread.id)
        self.assertTrue(landed, "the expiry archive never reached Discord")
        self.assertTrue(open_after_archive, "the late expiry archive closed the reconnected session's thread")

    async def test_a_failed_reclaim_puts_the_old_session_back_in_grace(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("lost")
        thread = fake.add_thread("lost", marker=marker(hello), members={BOT_ID})
        with patch.object(bridge_module, "SESSION_DISCONNECT_GRACE_SECONDS", 1.0):
            async with scenario(fake) as running, aiohttp.ClientSession() as http:
                first = await attach(running, http, hello)
                await first.receive_json(timeout=10)
                await first.close()
                previous = await in_grace(running, "lost")
                # Someone archives the thread during grace, and Discord refuses to reopen it.
                thread.archived = thread.locked = True
                thread.archived_at = next(fake.clock)
                fake.faults.append(Fault("PATCH", "/channels/{channel}", times=10, status=403))
                second = await attach(running, http, {**hello, "session_epoch": "e2"})
                refused = await second.receive(timeout=10)
                restored = (running.bridge.sessions.get("lost"), running.bridge.sessions.by_thread.get(thread.id))
                ended = await until(lambda: running.bridge.sessions.get("lost") is None, timeout=5)

        self.assertEqual(refused.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(restored, (previous, "lost"))
        self.assertTrue(ended, "the old session's grace timer did not end it after the failed reclaim")
        self.assertEqual(running.bridge.sessions.by_thread, {})

    async def test_a_thread_deleted_during_grace_is_replaced_on_reconnect(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("deleted")
        thread = fake.add_thread("deleted", marker=marker(hello), members={BOT_ID})
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            first = await attach(running, http, hello)
            await first.receive_json(timeout=10)
            await first.close()
            await in_grace(running, "deleted")
            fake.delete_thread(thread.id)  # No gateway event yet: the bot's cache still has the thread.
            second = await attach(running, http, {**hello, "session_epoch": "e2"})
            ack = await second.receive_json(timeout=10)
            replacements = running.threads_marked_for(hello)
            await second.close()

        self.assertEqual([t.id for t in replacements], [ack["thread_id"]])
        self.assertNotEqual(ack["thread_id"], thread.id)
