"""A session thread's worker: intent reread between requests, and nothing ever cancelled (#148)."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock, patch

import discord
from aiohttp import web

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.doodads.agent_session.thread_worker import RenameTarget, ThreadWorkers
from tests.fakes_agent_session import (
    FakeReplyMessage,
    FakeTextChannel,
    FakeThread,
    FakeWebSocket,
    add_bot_message,
    make_hello,
)
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

    async def test_a_reopen_waiting_for_a_slot_is_not_sent_after_stop(self) -> None:
        busy, idle = FakeThread(1), FakeThread(2, archived=True, locked=True)
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
            # The only slot is taken; this reopen waits for it, and the bridge stops meanwhile.
            reopening = asyncio.create_task(workers.open(cast(discord.Thread, idle)))
            await asyncio.sleep(0.01)
            workers.stop()
            release.set()
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(reopening, timeout=1)
            await asyncio.sleep(0.05)

        self.assertTrue(busy.archived, "stopping kept a close from finishing")
        self.assertTrue(idle.archived, "a reopen asked for before the stop was sent after it")

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

    async def test_a_cleanup_retry_cannot_close_a_thread_reopened_but_not_yet_bound(self) -> None:
        bridge = make_bridge()
        thread = FakeThread(501, archived=True, members=[111])
        hello = make_hello()
        bridge.sessions.register(AgentSession(hello=hello, websocket=cast(web.WebSocketResponse, FakeWebSocket())))
        posting, release = asyncio.Event(), asyncio.Event()

        async def slow_notification(_hello: object, _thread: object) -> int:
            posting.set()
            await release.wait()
            return 7

        with (
            patch.object(bridge, "find_existing_session_thread", new=AsyncMock(return_value=thread)),
            patch.object(bridge, "ensure_session_notification", new=slow_notification),
        ):
            attaching = asyncio.create_task(bridge.find_or_create_session_thread(hello))
            await asyncio.wait_for(posting.wait(), timeout=1)
            # Reopened, not yet bound: an older session's cleanup retry reaches the thread now.
            retry = bridge.pending_cleanup_for_session(
                AgentSession(hello=make_hello(), websocket=cast(web.WebSocketResponse, FakeWebSocket()), thread_id=501)
            )
            retry.pending_steps.discard("notification")
            await bridge.close_thread(cast(discord.Thread, thread), retry.pending_steps, timeout=1)
            release.set()
            await asyncio.wait_for(attaching, timeout=1)

        self.assertFalse(thread.archived)
        self.assertFalse(thread.left)
        self.assertEqual(thread.removed_user_ids, [])

    async def test_a_failed_overlapping_delete_does_not_expose_a_notification_still_being_deleted(self) -> None:
        bridge = make_bridge()
        channel = FakeTextChannel(321, [])
        notice = add_bot_message(channel, 101, "Agent session connected for `repo`: <#501>")
        first_sent, release = asyncio.Event(), asyncio.Event()
        original_delete = notice.delete
        calls = 0

        async def delete() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_sent.set()
                await release.wait()
                await original_delete()
            else:
                raise discord.RateLimited(60.0)

        with (
            patch.object(bridge_module, "get_agent_session_channel", return_value=channel),
            patch.object(notice, "delete", new=delete),
        ):
            first = asyncio.create_task(bridge.delete_notification_message(cast(discord.Message, notice)))
            await asyncio.wait_for(first_sent.wait(), timeout=1)
            with self.assertRaises(discord.RateLimited):
                await bridge.delete_notification_message(cast(discord.Message, notice))
            adopted = await bridge.find_session_notification_for_thread(501)
            release.set()
            await first

        self.assertIsNone(adopted)

    async def test_a_history_page_read_before_a_delete_landed_does_not_bring_the_notification_back(self) -> None:
        bridge = make_bridge()
        channel = FakeTextChannel(321, [])
        notice = add_bot_message(channel, 101, "Agent session connected for `repo`: <#501>")
        stale_page = [notice]  # What Discord answered before the delete landed.

        async def history(**_kwargs: object) -> AsyncIterator[FakeReplyMessage]:
            for message in stale_page:
                yield message

        with (
            patch.object(bridge_module, "get_agent_session_channel", return_value=channel),
            patch.object(channel, "history", history),
        ):
            await bridge.delete_notification_message(cast(discord.Message, notice))
            adopted = await bridge.find_session_notification_for_thread(501)

        self.assertIsNone(adopted)

    async def test_a_reloaded_bridge_adds_members_back_only_after_the_old_removal_lands(self) -> None:
        old_bridge = make_bridge()
        bot = old_bridge.bot
        bot.config.agent_session.listen_host, bot.config.agent_session.listen_port = "127.0.0.1", 0
        bot.config.agent_session.auto_join_user_ids = [7]
        thread = FakeThread(555, members=[7])
        removing, release = asyncio.Event(), asyncio.Event()
        membership: list[str] = []

        async def slow_remove(user: object) -> None:
            removing.set()
            await release.wait()
            membership.append("removed")

        async def add_user(user: object) -> None:
            membership.append("added")

        with patch.object(thread, "remove_user", new=slow_remove), patch.object(thread, "add_user", new=add_user, create=True):
            await old_bridge.start()
            await old_bridge.close_thread(cast(discord.Thread, thread), {"members", "archive", "leave"}, timeout=0)
            await asyncio.wait_for(removing.wait(), timeout=1)
            await old_bridge.stop()
            # The doodad is reloaded: a new bridge on the same bot, and the session reattaches at once.
            new_bridge = bridge_module.AgentSessionBridge(bot)
            await new_bridge.start()
            reopening = asyncio.create_task(new_bridge.threads.open(cast(discord.Thread, thread)))
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(reopening, timeout=1)
            await new_bridge.stop()

        self.assertEqual(membership, ["removed", "added"])

    async def test_overlapping_closes_share_one_pass_and_each_record_sees_what_landed(self) -> None:
        thread = FakeThread(555)
        workers = ThreadWorkers(Hooks())
        posting, release = asyncio.Event(), asyncio.Event()
        original_send = thread.send

        async def slow_send(content: str | None = None, **kwargs: object) -> object:
            posting.set()
            await release.wait()
            return await original_send(content, **kwargs)

        with patch.object(thread, "send", new=slow_send):
            ended: set[bridge_module.CleanupStep] = {"disconnect_notice", "archive", "leave"}
            session_close = asyncio.create_task(workers.close(cast(discord.Thread, thread), ended))
            await asyncio.wait_for(posting.wait(), timeout=1)
            # An older orphan record for the same thread is retried while the notice is being posted.
            orphan: set[bridge_module.CleanupStep] = {"disconnect_notice", "archive", "leave"}
            orphan_close = asyncio.create_task(workers.close(cast(discord.Thread, thread), orphan))
            release.set()
            await asyncio.wait_for(asyncio.gather(session_close, orphan_close), timeout=1)

        self.assertEqual(thread.sent_messages, ["Session ended"])
        self.assertEqual((ended, orphan), (set(), set()))
        self.assertTrue(thread.archived)


if __name__ == "__main__":
    unittest.main()
