from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator
from unittest.mock import patch

from discord_blue.doodads.agent_session import bridge as bridge_module
from tests.fakes_agent_session import FakeReplyMessage, FakeTextChannel, FakeThread, add_bot_message
from tests.test_session_cleanup_failures import make_bridge, register_stale


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.thread_patch = patch.object(bridge_module.discord, "Thread", FakeThread)
        self.channel_patch = patch.object(bridge_module.discord, "TextChannel", FakeTextChannel)
        self.thread_patch.start()
        self.channel_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.addCleanup(self.channel_patch.stop)

    async def test_periodic_scan_does_not_reopen_finalized_archived_threads(self) -> None:
        bridge = make_bridge()
        # Public archives remain discoverable even after the bot leaves; joined
        # private archives can also survive until an explicit leave retry.
        public = FakeThread(501, private=False)
        private = FakeThread(502, archived=True, locked=True)
        channel = FakeTextChannel(321, [public, private])
        for thread in (public, private):
            add_bot_message(thread, thread.id, "Agent session connected")
        with patch.object(bridge_module, "get_agent_session_channel", return_value=channel):
            await bridge.cleanup_stale_session_threads()
            self.assertTrue(public.archived)
            self.assertTrue(public.locked)
            notices = list(public.sent_messages)
            edits = list(public.edits)
            await bridge.cleanup_stale_session_threads()
            await bridge.cleanup_stale_session_threads()
        self.assertEqual(public.sent_messages, notices)
        self.assertEqual(public.edits, edits)
        self.assertEqual(private.sent_messages, [])
        self.assertEqual(private.edits, [])

    async def test_history_scan_does_not_hold_global_attach_lock(self) -> None:
        bridge = make_bridge()
        channel = FakeTextChannel(321, [])
        started = asyncio.Event()
        release = asyncio.Event()

        async def history(**_kwargs: object) -> AsyncIterator[FakeReplyMessage]:
            started.set()
            await release.wait()
            yield FakeReplyMessage(777, channel)

        with (
            patch.object(bridge_module, "get_agent_session_channel", return_value=channel),
            patch.object(channel, "history", history),
        ):
            scan = asyncio.create_task(bridge.cleanup_stale_session_notifications())
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                self.assertFalse(bridge._session_attach_lock.locked())
                await asyncio.wait_for(bridge._session_attach_lock.acquire(), timeout=1)
                bridge._session_attach_lock.release()
            finally:
                release.set()
                await scan

    async def test_blocked_notice_delete_does_not_skip_later_notice(self) -> None:
        bridge = make_bridge()
        channel = FakeTextChannel(321, [])
        blocked = add_bot_message(channel, 101, "Agent session connected for `old`: <#501>")
        later = add_bot_message(channel, 102, "Agent session connected for `later`: <#502>")
        cancelled = asyncio.Event()

        async def hang() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch.object(bridge_module, "get_agent_session_channel", return_value=channel),
            patch.object(blocked, "delete", new=hang),
            patch.object(bridge_module, "SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS", 0.01),
            self.assertLogs(bridge_module.logger, level="WARNING"),
        ):
            await bridge.cleanup_stale_session_notifications()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(later.deleted)
        self.assertTrue(bridge._pending_cleanups)

    async def test_notice_changed_during_discovery_is_not_overwritten(self) -> None:
        bridge = make_bridge()
        channel = FakeTextChannel(321, [])
        old_notice = add_bot_message(channel, 101, "Agent session connected for `old`: <#501>")
        duplicate = add_bot_message(channel, 102, "Agent session connected for `duplicate`: <#501>")
        new_notice = add_bot_message(channel, 999, "Agent session connected for `current`: <#501>")
        session = register_stale(bridge, "current")
        bridge.sessions.bind_thread(session.session_id, 501, old_notice.id)
        scanned_first = asyncio.Event()
        release = asyncio.Event()

        async def history(**_kwargs: object) -> AsyncIterator[FakeReplyMessage]:
            yield old_notice
            scanned_first.set()
            await release.wait()
            yield duplicate

        with (
            patch.object(bridge_module, "get_agent_session_channel", return_value=channel),
            patch.object(channel, "history", history),
        ):
            scan = asyncio.create_task(bridge.cleanup_stale_session_notifications())
            try:
                await asyncio.wait_for(scanned_first.wait(), timeout=1)
                async with bridge.thread_lifecycle_lock(501):
                    session.notification_message_id = new_notice.id
            finally:
                release.set()
                await scan
        self.assertEqual(session.notification_message_id, new_notice.id)
        self.assertFalse(new_notice.deleted)
        self.assertTrue(old_notice.deleted)
        self.assertTrue(duplicate.deleted)

    async def test_orphan_scan_skips_thread_being_attached(self) -> None:
        bridge = make_bridge()
        thread = FakeThread(501)
        channel = FakeTextChannel(321, [thread])
        add_bot_message(thread, 1, "Agent session connected")
        lock = bridge.thread_lifecycle_lock(thread.id)
        await lock.acquire()
        try:
            with patch.object(bridge_module, "get_agent_session_channel", return_value=channel):
                await bridge.cleanup_stale_session_threads()
            self.assertFalse(thread.archived)
            self.assertFalse(thread.left)
            session = register_stale(bridge, "connecting")
            bridge.sessions.bind_thread(session.session_id, thread.id)
        finally:
            lock.release()
        with patch.object(bridge_module, "get_agent_session_channel", return_value=channel):
            await bridge.cleanup_stale_session_threads()
        self.assertFalse(thread.archived)
        self.assertFalse(thread.left)
        self.assertIsNotNone(bridge.sessions.get_by_thread(thread.id))

    async def test_orphan_scan_preserves_new_thread_before_attach_binds_it(self) -> None:
        bridge = make_bridge()
        thread = FakeThread(501)
        channel = FakeTextChannel(321, [thread])
        add_bot_message(thread, 1, "Agent session connected")
        # The create path publishes a marker while still holding the attach
        # lock, before its new thread ID can be bound in the registry.
        async with bridge._session_attach_lock:
            with patch.object(bridge_module, "get_agent_session_channel", return_value=channel):
                await bridge.cleanup_stale_session_threads()
            self.assertFalse(thread.archived)
            self.assertFalse(thread.left)
        with patch.object(bridge_module, "get_agent_session_channel", return_value=channel):
            await bridge.cleanup_stale_session_threads()
        self.assertTrue(thread.archived)
        self.assertTrue(thread.left)
