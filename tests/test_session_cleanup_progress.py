from __future__ import annotations

import asyncio
import unittest
from contextlib import suppress
from typing import cast
from unittest.mock import AsyncMock, patch

import discord
from aiohttp import web

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.sessions import AgentSession
from tests.fakes_agent_session import FakeReplyMessage, FakeThread, FakeWebSocket, UserLike, make_hello
from tests import test_session_cleanup as cleanup_tests


class CleanupProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        patcher = patch.object(bridge_module.discord, "Thread", FakeThread)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stale_duplicate_record_cannot_resurrect_completed_steps(self) -> None:
        bridge = cleanup_tests.SessionCleanupTests.make_bridge()
        residual = cleanup_tests.SessionCleanupTests.cleanup_record(555, "leave")
        bridge.remember_pending_cleanup(residual)
        stale = cleanup_tests.SessionCleanupTests.cleanup_record(555, "disconnect_notice", "members", "archive", "leave")
        bridge.remember_pending_cleanup(stale)
        self.assertIs(bridge._pending_cleanups[residual.key], residual)
        self.assertEqual(residual.pending_steps, {"leave"})

    async def test_maintenance_continues_after_failed_iteration(self) -> None:
        bridge = cleanup_tests.SessionCleanupTests.make_bridge()
        second_iteration = asyncio.Event()
        calls = 0

        async def retry() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("failed maintenance")
            second_iteration.set()
            await asyncio.Event().wait()

        with (
            patch.object(bridge_module, "STARTUP_RECONNECT_GRACE_SECONDS", 0),
            patch.object(bridge_module, "MAINTENANCE_INTERVAL_SECONDS", 0),
            patch.object(bridge, "retry_pending_cleanups", new=retry),
            self.assertLogs(bridge_module.logger, level="ERROR"),
        ):
            task = asyncio.create_task(bridge.cleanup_stale_sessions())
            try:
                await asyncio.wait_for(second_iteration.wait(), timeout=1)
                self.assertTrue(bridge._maintenance_has_run)
                self.assertFalse(task.done())
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def test_finalizer_interruption_preserves_completed_steps(self) -> None:
        for interruption in ("cancel", "timeout"):
            with self.subTest(interruption=interruption):
                thread = FakeThread(555, members=[111])
                bridge = cleanup_tests.SessionCleanupTests.make_bridge(thread)
                session = AgentSession(
                    hello=make_hello(),
                    websocket=cast(web.WebSocketResponse, FakeWebSocket()),
                    thread_id=555,
                    notification_message_id=101,
                )
                bridge.sessions.register(session)
                leaving = asyncio.Event()

                async def blocked_leave(_leaving: asyncio.Event = leaving) -> None:
                    _leaving.set()
                    await asyncio.Event().wait()

                with (
                    patch.object(thread, "leave", new=blocked_leave),
                    patch.object(bridge, "delete_session_notification", new=AsyncMock(return_value=True)) as delete,
                    patch.object(bridge_module, "SHUTDOWN_THREAD_CLEANUP_TIMEOUT_SECONDS", 0.05),
                ):
                    task = asyncio.create_task(bridge.finalize_session(session))
                    await asyncio.wait_for(leaving.wait(), timeout=1)
                    if interruption == "cancel":
                        task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    delete.assert_awaited_once_with(101)
                self.assertIsNone(bridge.sessions.get(session.session_id))
                self.assertTrue(thread.archived)
                pending = list(bridge._pending_cleanups.values())
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].pending_steps, {"leave"})
                notices = list(thread.sent_messages)
                edits = list(thread.edits)
                await bridge.retry_pending_cleanups()
                self.assertEqual(bridge._pending_cleanups, {})
                self.assertTrue(thread.left)
                self.assertEqual(thread.sent_messages, notices)
                self.assertEqual(thread.edits, edits)

    async def test_retry_defers_busy_attachment_without_spending_attempt(self) -> None:
        for lock_kind in ("attach", "thread"):
            with self.subTest(lock_kind=lock_kind):
                thread = FakeThread(555)
                bridge = cleanup_tests.SessionCleanupTests.make_bridge(thread)
                cleanup = cleanup_tests.SessionCleanupTests.cleanup_record(555, "archive", "leave")
                bridge.remember_pending_cleanup(cleanup)
                lock = bridge._session_attach_lock if lock_kind == "attach" else bridge.thread_lifecycle_lock(555)
                async with lock:
                    await asyncio.wait_for(bridge.retry_pending_cleanups(), timeout=0.1)
                    self.assertEqual(cleanup.attempts, 0)
                    self.assertFalse(thread.archived)
                    self.assertFalse(thread.left)
                await bridge.retry_pending_cleanups()
                self.assertEqual(bridge._pending_cleanups, {})
                self.assertTrue(thread.archived)
                self.assertTrue(thread.left)

    async def test_cleanup_allows_slow_rest_calls_and_multiple_members(self) -> None:
        thread = FakeThread(555, archived=True, locked=True, members=[111, 222])
        bridge = cleanup_tests.SessionCleanupTests.make_bridge(thread)
        original_lookup = bridge.get_thread_for_cleanup
        original_edit = thread.edit
        original_send = thread.send
        original_fetch = thread.fetch_members
        original_remove = thread.remove_user

        async def lookup(thread_id: int) -> tuple[discord.Thread | None, bool]:
            await asyncio.sleep(0.3)
            return await original_lookup(thread_id)

        async def edit(**kwargs: object) -> None:
            await asyncio.sleep(0.3)
            await original_edit(**kwargs)

        async def send(content: str | None = None, **kwargs: object) -> FakeReplyMessage:
            await asyncio.sleep(0.3)
            return await original_send(content, **kwargs)

        async def fetch() -> list[object]:
            await asyncio.sleep(0.3)
            return await original_fetch()

        async def remove(user: UserLike) -> None:
            await asyncio.sleep(0.3)
            await original_remove(user)

        cleanup = cleanup_tests.SessionCleanupTests.cleanup_record(555, "disconnect_notice", "members", "archive", "leave")
        with (
            patch.object(bridge, "get_thread_for_cleanup", new=lookup),
            patch.object(thread, "edit", new=edit),
            patch.object(thread, "send", new=send),
            patch.object(thread, "fetch_members", new=fetch),
            patch.object(thread, "remove_user", new=remove),
        ):
            residual = await bridge.cleanup_session_artifacts(cleanup)
        self.assertIsNone(residual)
        self.assertEqual(thread.removed_user_ids, [111, 222])
        self.assertEqual(thread.sent_messages, ["Agent session disconnected"])
        self.assertTrue(thread.archived)
        self.assertTrue(thread.locked)
        self.assertTrue(thread.left)
