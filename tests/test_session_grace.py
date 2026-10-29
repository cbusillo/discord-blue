"""A dropped connection's grace period and a clean session_end, against the real bridge and discord.py (#148)."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import aiohttp

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session import cleanup as cleanup_module
from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.doodads.agent_session.store import StoredSession
from tests.test_attach_scenarios import Scenario, hello_for, marker, scenario, until
from tests.fake_discord import BOT_ID, FakeDiscord, FakeMessage, Fault, Hold

# Any of these on a session's thread is churn a reconnect within grace must not cause.
CHURN = {
    ("POST", "/channels/{channel}/messages"),
    ("PUT", "/channels/{channel}/thread-members/{member}"),
    ("DELETE", "/channels/{channel}/thread-members/{member}"),
    ("PATCH", "/channels/{channel}"),
}
OWNER_ID = 7


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
    async def test_a_held_typing_request_does_not_block_native_heartbeats(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("held-typing")
        fake.add_thread("held-typing", marker=marker(hello), members={BOT_ID})
        async with scenario(fake, heartbeat_timeout_seconds=1) as running, aiohttp.ClientSession() as http:
            socket = await attach(running, http, hello)
            await socket.receive_json(timeout=5)
            session = running.bridge.sessions.get(hello["session_id"])
            assert session is not None
            before = session.last_seen
            hold = Hold("POST", "/channels/{channel}/typing")
            fake.holds.append(hold)
            try:
                await socket.send_json({**hello, "type": "status_changed", "message": "Turn started"})
                await socket.send_json({**hello, "type": "heartbeat"})
                self.assertTrue(await until(lambda: session.last_seen > before, 0.5))
                await socket.send_json({**hello, "type": "turn_complete", "message": "Turn complete"})
                self.assertTrue(await until(lambda: session.display_state == "done", 0.5))
                await running.bridge.close_timed_out_sessions()
                self.assertFalse(session.websocket.closed)
            finally:
                hold.released.set()
            await socket.close()

    async def test_reconnect_acknowledges_while_typing_or_old_card_retirement_is_blocked(self) -> None:
        for blocked in ("typing", "retirement"):
            with self.subTest(blocked=blocked):
                await self.reconnect_with_blocked_request(blocked)

    async def reconnect_with_blocked_request(self, blocked: str) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("quiet-reconnect")
        thread = fake.add_thread("quiet-reconnect", marker=marker(hello), members={BOT_ID})
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            first = await attach(running, http, hello)
            await first.receive_json(timeout=5)
            old = running.bridge.sessions.get(hello["session_id"])
            assert old is not None
            if blocked == "typing":
                hold = Hold("POST", "/channels/{channel}/typing")
                fake.holds.append(hold)
                await first.send_json({**hello, "type": "status_changed", "message": "Turn started"})
                self.assertTrue(await until(lambda: ("POST", "/channels/{channel}/typing") in fake.requests, 5))
            else:
                hold = Hold("DELETE", "/channels/{channel}/messages/{message}")
                fake.holds.append(hold)
                card = FakeMessage(next(fake.ids), thread.id, "", components=[{"type": 17, "id": 213, "components": []}])
                thread.messages.append(card)
                running.bridge.store.records[hello["session_id"]] = StoredSession(
                    thread.id, None, "", "live", 0, 0, control_message_id=card.id
                )
            try:
                await first.close()
                self.assertTrue(await until(lambda: old.grace_task is not None, 5))
                second = await attach(running, http, {**hello, "session_epoch": "e2"})
                ack = await second.receive_json(timeout=0.5)
                self.assertEqual(ack["thread_id"], thread.id)
            finally:
                hold.released.set()
            await second.close()
            await asyncio.wait_for(hold.landed.wait(), 1)

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

    async def test_session_end_closes_without_new_notice(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("finishes")
        thread = fake.add_thread("finishes", marker=marker(hello), members={BOT_ID})
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello)
            await websocket.receive_json(timeout=10)
            before_messages = [message.id for message in thread.messages]
            await websocket.send_json({"type": "session_end", "session_id": "finishes", "session_epoch": "e1"})
            await websocket.close()  # As the clients do right after session_end.
            archived = await until(lambda: thread.archived and thread.locked, timeout=5)

        self.assertTrue(archived, "a clean session_end did not archive and lock the thread")
        self.assertIsNone(running.bridge.sessions.get("finishes"))
        self.assertEqual([message.id for message in thread.messages], before_messages)

    async def test_grace_expiry_archives_without_new_notice(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("gone")
        thread = fake.add_thread("gone", marker=marker(hello), members={BOT_ID})
        with patch.object(bridge_module, "SESSION_DISCONNECT_GRACE_SECONDS", 0.5):
            async with scenario(fake) as running, aiohttp.ClientSession() as http:
                websocket = await running.connect(http)
                await websocket.send_json(hello)
                await websocket.receive_json(timeout=10)
                before_messages = [message.id for message in thread.messages]
                await websocket.close()
                await asyncio.sleep(0.2)
                during_grace = thread.archived
                archived = await until(lambda: thread.archived and thread.locked, timeout=5)

        self.assertFalse(during_grace)
        self.assertTrue(archived, "the thread stayed open after its grace period expired")
        self.assertEqual([message.id for message in thread.messages], before_messages)

    async def test_sweeps_leave_a_session_in_grace_alone(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("waiting")
        fake.add_thread("waiting", marker=marker(hello), members={BOT_ID})
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
            await running.bridge.cleanups.cleanup_stale_session_threads()
            await asyncio.sleep(0.2)

        self.assertEqual(running.archived, [])
        self.assertIs(running.bridge.sessions.get("waiting"), session)

    async def test_an_expiry_archive_that_lands_late_does_not_close_the_reconnected_thread(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("late")
        thread = fake.add_thread("late", marker=marker(hello), members={BOT_ID})
        with (
            patch.object(bridge_module, "SESSION_DISCONNECT_GRACE_SECONDS", 0.05),
            patch.object(cleanup_module, "THREAD_CLOSE_WAIT_SECONDS", 0.05),
        ):
            async with scenario(fake) as running, aiohttp.ClientSession() as http:
                first = await attach(running, http, hello)
                await first.receive_json(timeout=10)
                fake.body_latency.append(("PATCH", "/channels/{channel}", lambda body: body.get("archived") is True, 0.2))
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
        with patch.object(bridge_module, "SESSION_DISCONNECT_GRACE_SECONDS", 0.5):
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
