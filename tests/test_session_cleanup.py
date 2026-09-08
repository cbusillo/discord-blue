from __future__ import annotations

import asyncio
import unittest
from contextlib import suppress
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import discord
from aiohttp import web

from discord_blue.config import AgentSessionConfig, Config, DiscordConfig
from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.sessions import AgentSession, PendingSessionCleanup
from discord_blue.plugs.discord_plug import BlueBot
from tests.fakes_agent_session import FakeBot, FakeThread, FakeWebSocket, make_hello


class SessionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.thread_patcher = patch.object(bridge_module.discord, "Thread", FakeThread)
        self.thread_patcher.start()

    async def asyncTearDown(self) -> None:
        self.thread_patcher.stop()

    @staticmethod
    def make_bridge(thread: FakeThread | None = None) -> AgentSessionBridge:
        config = cast(Config, SimpleNamespace(agent_session=AgentSessionConfig(), discord=DiscordConfig()))
        return AgentSessionBridge(cast(BlueBot, FakeBot(config, thread=thread)))

    @staticmethod
    def cleanup_record(thread_id: int, *steps: bridge_module.CleanupStep) -> PendingSessionCleanup:
        return PendingSessionCleanup(
            session_id="session-1",
            session_epoch="epoch-1",
            thread_id=thread_id,
            notification_message_id=None,
            pending_steps=set(steps),
        )

    def test_finalization_budgets_fit_reconnect_deadline(self) -> None:
        thread_budget = (
            bridge_module.THREAD_LOOKUP_TIMEOUT_SECONDS
            + bridge_module.THREAD_UNARCHIVE_TIMEOUT_SECONDS
            + bridge_module.THREAD_DISCONNECT_NOTICE_TIMEOUT_SECONDS
            + bridge_module.THREAD_MEMBER_CLEANUP_TIMEOUT_SECONDS
            + bridge_module.THREAD_ARCHIVE_TIMEOUT_SECONDS
            + bridge_module.THREAD_LEAVE_TIMEOUT_SECONDS
        )
        self.assertLessEqual(thread_budget, bridge_module.SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS)
        self.assertEqual(
            bridge_module.SESSION_FINALIZATION_TIMEOUT_SECONDS,
            bridge_module.SESSION_WEBSOCKET_CLOSE_TIMEOUT_SECONDS
            + bridge_module.SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS
            + bridge_module.SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS,
        )
        self.assertLess(
            bridge_module.SESSION_FINALIZATION_TIMEOUT_SECONDS,
            bridge_module.SESSION_LIFECYCLE_LOCK_TIMEOUT_SECONDS,
        )

    async def test_member_failure_does_not_skip_archive_and_retry_recloses_thread(self) -> None:
        thread = FakeThread(555, members=[111])
        bridge = self.make_bridge(thread)
        cleanup = self.cleanup_record(555, "members", "archive", "leave")
        with patch.object(bridge, "remove_thread_members", new=AsyncMock(side_effect=[False, True])):
            residual = await bridge.cleanup_session_artifacts(cleanup)
            self.assertIsNotNone(residual)
            self.assertEqual(cast(PendingSessionCleanup, residual).pending_steps, {"members"})
            self.assertTrue(thread.archived)
            self.assertTrue(thread.left)

            bridge.remember_pending_cleanup(cast(PendingSessionCleanup, residual))
            await bridge.retry_pending_cleanups()

        self.assertEqual(bridge._pending_cleanups, {})
        self.assertTrue(thread.archived)
        self.assertTrue(thread.locked)
        self.assertTrue(thread.left)

    async def test_thread_lock_recheck_lets_new_attachment_win(self) -> None:
        thread = FakeThread(555)
        bridge = self.make_bridge(thread)
        cleanup = self.cleanup_record(555, "archive", "leave")
        thread_lock = bridge.thread_lifecycle_lock(thread.id)
        await thread_lock.acquire()
        cleanup_task = asyncio.create_task(bridge.cleanup_session_artifacts(cleanup))
        await asyncio.sleep(0)
        self.assertFalse(cleanup_task.done())

        session = AgentSession(
            hello=make_hello(),
            websocket=cast(web.WebSocketResponse, FakeWebSocket()),
            thread_id=thread.id,
        )
        bridge.sessions.register(session)
        thread_lock.release()

        self.assertIsNone(await cleanup_task)
        self.assertFalse(thread.archived)
        self.assertFalse(thread.left)

    async def test_cancelled_finalizer_unregisters_and_retains_retry_record(self) -> None:
        bridge = self.make_bridge(FakeThread(555))
        session = AgentSession(
            hello=make_hello(),
            websocket=cast(web.WebSocketResponse, FakeWebSocket()),
            thread_id=555,
        )
        bridge.sessions.register(session)
        cleanup_started = asyncio.Event()

        async def blocked_cleanup(_session: AgentSession) -> None:
            cleanup_started.set()
            await asyncio.Event().wait()

        with patch.object(bridge, "close_session_thread", new=blocked_cleanup):
            task = asyncio.create_task(bridge.finalize_session(session))
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self.assertIsNone(bridge.sessions.get(session.session_id))
        self.assertTrue(bridge._pending_cleanups)

    async def test_fetch_members_failure_remains_pending_but_archive_continues(self) -> None:
        thread = FakeThread(555, members=[111])
        bridge = self.make_bridge(thread)

        async def failed_fetch() -> list[object]:
            raise discord.Forbidden(
                response=SimpleNamespace(status=403, reason="Forbidden"),
                message="cannot fetch members",
            )

        thread.fetch_members = failed_fetch  # type: ignore[method-assign]
        thread.members = [SimpleNamespace(id=111)]  # type: ignore[attr-defined]
        remaining = await bridge.close_thread(cast(discord.Thread, thread), {"members", "archive", "leave"})

        self.assertEqual(remaining, {"members"})
        self.assertEqual(thread.removed_user_ids, [111])
        self.assertTrue(thread.archived)
        self.assertTrue(thread.left)

    async def test_archive_failure_retains_leave_and_bot_membership_for_retry(self) -> None:
        thread = FakeThread(555)
        bridge = self.make_bridge(thread)
        with patch.object(thread, "edit", new=AsyncMock(side_effect=RuntimeError("archive failed"))):
            remaining = await bridge.close_thread(cast(discord.Thread, thread), {"archive", "leave"})

        self.assertEqual(remaining, {"archive", "leave"})
        self.assertFalse(thread.archived)
        self.assertFalse(thread.left)

    async def test_retry_cancellation_keeps_existing_record(self) -> None:
        bridge = self.make_bridge(FakeThread(555))
        cleanup = self.cleanup_record(555, "archive")
        bridge.remember_pending_cleanup(cleanup)
        started = asyncio.Event()

        async def blocked_cleanup(_cleanup: PendingSessionCleanup) -> None:
            started.set()
            await asyncio.Event().wait()

        with patch.object(bridge, "cleanup_session_artifacts", new=blocked_cleanup):
            task = asyncio.create_task(bridge.retry_pending_cleanups())
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self.assertIs(bridge._pending_cleanups[cleanup.key], cleanup)

    async def test_cancellation_during_websocket_close_retains_artifact_retry(self) -> None:
        close_started = asyncio.Event()

        class BlockingWebSocket(FakeWebSocket):
            async def close(self, *, message: bytes = b"", drain: bool = True) -> bool:
                close_started.set()
                await asyncio.Event().wait()
                return await super().close(message=message, drain=drain)

        bridge = self.make_bridge(FakeThread(555))
        session = AgentSession(
            hello=make_hello(),
            websocket=cast(web.WebSocketResponse, BlockingWebSocket()),
            thread_id=555,
        )
        bridge.sessions.register(session)
        task = asyncio.create_task(bridge.finalize_session(session))
        await asyncio.wait_for(close_started.wait(), timeout=1)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        self.assertIsNone(bridge.sessions.get(session.session_id))
        self.assertTrue(bridge._pending_cleanups)

    async def test_retry_deletes_old_duplicate_notice_without_closing_live_thread(self) -> None:
        thread = FakeThread(555)
        bridge = self.make_bridge(thread)
        current = AgentSession(
            hello=make_hello(),
            websocket=cast(web.WebSocketResponse, FakeWebSocket()),
            thread_id=555,
            notification_message_id=222,
        )
        bridge.sessions.register(current)
        cleanup = PendingSessionCleanup(
            session_id="orphan-notification-111",
            session_epoch="orphan",
            thread_id=555,
            notification_message_id=111,
            pending_steps={"notification"},
        )
        bridge.remember_pending_cleanup(cleanup)

        with patch.object(bridge, "delete_session_notification", new=AsyncMock(return_value=True)) as delete:
            await bridge.retry_pending_cleanups()

        delete.assert_awaited_once_with(111)
        self.assertEqual(bridge._pending_cleanups, {})
        self.assertFalse(thread.archived)
        self.assertFalse(thread.left)


if __name__ == "__main__":
    unittest.main()
