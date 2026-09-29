"""A session thread's worker: intent reread between requests, and nothing ever cancelled (#148)."""

from __future__ import annotations

import asyncio
import unittest
from typing import cast
from unittest.mock import patch

import discord

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.thread_worker import RenameTarget, ThreadWorkers
from tests.fakes_agent_session import FakeTextChannel, FakeThread, add_bot_message
from tests.test_session_cleanup_failures import make_bridge, register_stale

BOT_ID = 999


class Hooks:
    def owned(self, _thread_id: int) -> bool:
        return False

    def rename_target(self, _thread_id: int, _epoch: str) -> RenameTarget | None:
        return None

    def bot_user_id(self) -> int | None:
        return BOT_ID

    async def post_close_notice(self, thread: discord.Thread) -> None:
        await thread.send("Session ended")

    async def add_configured_members(self, _thread: discord.Thread) -> None:
        return None


class ThreadWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_reopen_asked_for_while_members_are_listed_removes_none_of_them(self) -> None:
        thread = FakeThread(555, members=[111, 222])
        workers = ThreadWorkers(Hooks())
        listing, release = asyncio.Event(), asyncio.Event()
        original_fetch = thread.fetch_members

        async def slow_fetch() -> list[object]:
            listing.set()
            await release.wait()
            return await original_fetch()

        with patch.object(thread, "fetch_members", new=slow_fetch):
            steps: set[bridge_module.CleanupStep] = {"members", "archive", "leave"}
            closing = asyncio.create_task(workers.close(cast(discord.Thread, thread), steps))
            await asyncio.wait_for(listing.wait(), timeout=1)
            # The session reconnects while the close is still listing the thread's members.
            reopening = asyncio.create_task(workers.open(cast(discord.Thread, thread)))
            release.set()
            await asyncio.wait_for(asyncio.gather(closing, reopening), timeout=1)

        self.assertEqual(thread.removed_user_ids, [])
        self.assertFalse(thread.archived)
        self.assertFalse(thread.left)

    async def test_stopping_lets_a_request_already_sent_finish(self) -> None:
        thread = FakeThread(555)
        workers = ThreadWorkers(Hooks())
        archiving, release = asyncio.Event(), asyncio.Event()
        original_edit = thread.edit

        async def slow_edit(**kwargs: object) -> None:
            archiving.set()
            await release.wait()  # As discord.py sleeping out a global rate limit.
            await original_edit(**kwargs)

        with patch.object(thread, "edit", new=slow_edit):
            steps: set[bridge_module.CleanupStep] = {"archive"}
            await workers.close(cast(discord.Thread, thread), steps, timeout=0)
            await asyncio.wait_for(archiving.wait(), timeout=1)
            worker = workers.workers[thread.id]
            workers.stop()
            release.set()
            assert worker.task is not None
            await asyncio.wait_for(asyncio.shield(worker.task), timeout=1)

        self.assertTrue(thread.archived, "stopping cancelled a request that was already sent")
        self.assertEqual(steps, set())

    async def test_a_notification_adopted_during_a_slow_cleanup_is_not_deleted(self) -> None:
        bridge = make_bridge()
        channel = FakeTextChannel(321, [])
        notice = add_bot_message(channel, 101, "Agent session connected for `repo`: <#501>")
        fetching, release = asyncio.Event(), asyncio.Event()
        original_fetch = channel.fetch_message

        async def slow_fetch(message_id: int) -> object:
            fetching.set()
            await release.wait()
            return await original_fetch(message_id)

        with (
            patch.object(bridge_module, "get_agent_session_channel", return_value=channel),
            patch.object(channel, "fetch_message", new=slow_fetch),
            patch.object(bridge_module, "SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS", 0.01),
        ):
            with self.assertRaises(TimeoutError):
                await bridge.threads.bounded(bridge.delete_session_notification(notice.id), 0.01)
            await asyncio.wait_for(fetching.wait(), timeout=1)
            # Teardown stopped waiting; a reconnect now reuses the same notification.
            session = register_stale(bridge, "reconnected")
            bridge.sessions.bind_thread(session.session_id, 501, notice.id)
            release.set()
            await asyncio.sleep(0.05)

        self.assertFalse(notice.deleted, "a cleanup nobody waited for deleted the reconnected session's notification")

    async def test_a_restarted_bridge_reopens_a_thread_only_after_the_old_archive_lands(self) -> None:
        bridge = make_bridge()
        bridge.bot.config.agent_session.listen_host, bridge.bot.config.agent_session.listen_port = "127.0.0.1", 0
        thread = FakeThread(555)
        archiving, release = asyncio.Event(), asyncio.Event()
        original_edit = thread.edit

        async def slow_archive(**kwargs: object) -> None:
            if kwargs.get("archived") is True:
                archiving.set()
                await release.wait()
            await original_edit(**kwargs)

        with patch.object(thread, "edit", new=slow_archive):
            await bridge.start()
            await bridge.threads.close(cast(discord.Thread, thread), {"archive"}, timeout=0)
            await asyncio.wait_for(archiving.wait(), timeout=1)
            await bridge.stop()
            await bridge.start()
            # A session reattaches while the old bridge's archive is still in flight.
            reopening = asyncio.create_task(bridge.threads.open(cast(discord.Thread, thread)))
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(reopening, timeout=1)
            await bridge.stop()

        self.assertFalse(thread.archived, "the old archive landed after the restarted bridge reopened the thread")

    async def test_a_request_waiting_for_a_slot_is_not_sent_after_stop(self) -> None:
        busy, idle = FakeThread(1), FakeThread(2)
        workers = ThreadWorkers(Hooks(), concurrency=1)
        archiving, release = asyncio.Event(), asyncio.Event()
        original_edit = busy.edit

        async def slow_edit(**kwargs: object) -> None:
            archiving.set()
            await release.wait()
            await original_edit(**kwargs)

        with patch.object(busy, "edit", new=slow_edit):
            await workers.close(cast(discord.Thread, busy), {"archive"}, timeout=0)
            await asyncio.wait_for(archiving.wait(), timeout=1)
            # The only slot is taken; this archive waits for it, and the bridge stops meanwhile.
            await workers.close(cast(discord.Thread, idle), {"archive"}, timeout=0)
            workers.stop()
            release.set()
            await asyncio.sleep(0.05)

        self.assertTrue(busy.archived)
        self.assertFalse(idle.archived, "a request chosen before the stop was sent after it")

    async def test_a_notification_whose_delete_is_still_pending_is_not_adopted(self) -> None:
        bridge = make_bridge()
        channel = FakeTextChannel(321, [])
        notice = add_bot_message(channel, 101, "Agent session connected for `repo`: <#501>")
        deleting, release = asyncio.Event(), asyncio.Event()
        original_delete = notice.delete

        async def rate_limited_delete() -> None:
            deleting.set()
            await release.wait()  # discord.py sleeping through the DELETE's rate limit.
            await original_delete()

        with (
            patch.object(bridge_module, "get_agent_session_channel", return_value=channel),
            patch.object(notice, "delete", new=rate_limited_delete),
        ):
            with self.assertRaises(TimeoutError):
                await bridge.threads.bounded(bridge.delete_session_notification(notice.id), 0.01)
            await asyncio.wait_for(deleting.wait(), timeout=1)
            adopted = await bridge.find_session_notification_for_thread(501)
            release.set()
            await asyncio.sleep(0.05)

        self.assertIsNone(adopted, "an attach adopted a notification that was about to be deleted")


if __name__ == "__main__":
    unittest.main()
