from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import discord
import aiohttp

from discord_blue.doodads.agent_session import threads
from discord_blue.doodads.agent_session.cards import assistant_answer_cards
from discord_blue.doodads.agent_session.controls import REACTION_CONTROL_STATUS
from discord_blue.plugs.discord_plug import BlueBot
from discord_blue.doodads.agent_session.protocol import SessionHello
from discord_blue.doodads.agent_session.store import StoredSession
from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from tests.fake_discord import FakeDiscord, BOT_ID
from tests.test_attach_scenarios import hello_for, scenario
from tests import test_thread_titles as title_tests
from discord_blue.doodads.agent_session.thread_worker import ThreadWorkers, ThreadHooks, INITIAL_TITLE_SETTLE_SECONDS
from tests.fakes_agent_prompts import prompt_fixture
from tests.fakes_agent_session import FakeThread, FakeReplyMessage, make_hello


class QuietSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_details_are_private_and_operator_gated_after_reconnect(self) -> None:
        with prompt_fixture() as fixture:
            for online in (True, False):
                if not online:
                    fixture.bridge.sessions.remove_if_current(fixture.session)
                    fixture.bridge.store.records[fixture.session.session_id] = StoredSession(
                        fixture.thread.id, None, threads.session_start_message(fixture.session.hello), "closed", 0, 0
                    )
                for permitted in (False, True):
                    with patch.object(AgentSessionBridge, "is_operator", return_value=permitted):
                        summary = fixture.bridge.controls.session_status_summary(
                            fixture.thread, cast(discord.User, cast(object, SimpleNamespace(id=123)))
                        )
                    self.assertEqual(fixture.session.hello.cwd in summary, permitted)
                    self.assertEqual(fixture.session.session_id in summary, permitted)

    async def test_cold_discovery_recovers_component_marker_without_parent_announcement(self) -> None:
        fake = FakeDiscord()
        hello = hello_for("quiet-recovery")
        original = fake.add_thread("quiet", archived=True, locked=True, members={BOT_ID})
        from tests.fake_discord import FakeMessage

        identity = FakeMessage(next(fake.ids), original.id, "")
        identity.components = threads.session_identity_view(SessionHello.from_payload(hello)).to_components()
        original.messages.append(identity)
        for pid in (hello["pid"], int(hello["pid"]) + 1):
            async with scenario(fake) as running, aiohttp.ClientSession() as http:
                self.assertTrue(
                    await running.bridge.cleanups.is_agent_session_session_thread(
                        cast(discord.Thread, cast(object, await running.bot.fetch_channel(original.id)))
                    )
                )
                socket = await running.connect(http)
                await socket.send_json({**hello, "pid": pid})
                ack = await socket.receive_json(timeout=10)
                self.assertEqual(ack["thread_id"], original.id)
                self.assertEqual(len(fake.threads), 1)
                self.assertEqual(fake.parent_messages, [])
                await socket.close()

    async def test_status_tap_refreshes_anchor_without_posting_summary(self) -> None:
        with prompt_fixture() as fixture:
            await fixture.bridge.controls.post_session_controls(fixture.session)
            anchor = cast(int, fixture.session.control_message_id)
            message = await fixture.thread.fetch_message(anchor)
            message.reactions.append(REACTION_CONTROL_STATUS)
            fixture.session.last_status_message = "New status from agent"
            with (
                patch.object(FakeReplyMessage, "clear_reactions", wraps=message.clear_reactions) as clear,
                patch.object(FakeReplyMessage, "add_reaction", wraps=message.add_reaction) as add,
            ):
                await fixture.bridge.controls.handle_session_control_reaction(
                    fixture.session,
                    cast(discord.Thread, fixture.thread),
                    anchor,
                    REACTION_CONTROL_STATUS,
                    cast(discord.User, cast(object, SimpleNamespace(id=123))),
                )
            clear.assert_not_awaited()
            add.assert_not_awaited()
            self.assertEqual(len(fixture.thread.sent_messages), 1)
            self.assertEqual(fixture.session.control_message_id, anchor)
            self.assertTrue(message.edit_kwargs)
            self.assertEqual(message.reactions.count(REACTION_CONTROL_STATUS), 1)

    async def test_reconnect_adds_only_missing_configured_members(self) -> None:
        with prompt_fixture() as fixture:
            fixture.bridge.bot.config.agent_session.auto_join_user_ids = [123, 456]
            thread = FakeThread(555, members=[999, 123])
            with patch.object(thread, "add_user", wraps=thread.add_user) as add:
                await threads.auto_join_configured_users(
                    cast(BlueBot, fixture.bridge.bot), cast(discord.Thread, cast(object, thread))
                )
                await threads.auto_join_configured_users(
                    cast(BlueBot, fixture.bridge.bot), cast(discord.Thread, cast(object, thread))
                )
            self.assertEqual([call.args[0].id for call in add.call_args_list], [456])

    async def test_identity_is_out_of_the_reading_path_but_persistent(self) -> None:
        with prompt_fixture() as fixture:
            thread = FakeThread(555)
            parent = SimpleNamespace(id=321, send=AsyncMock(), create_thread=AsyncMock(return_value=thread))
            with patch.object(threads, "get_agent_session_channel", return_value=parent):
                result = await threads.create_session_thread(cast(BlueBot, fixture.bridge.bot), make_hello())
            parent.send.assert_not_awaited()
            self.assertIsNone(result.notification_message_id)
            self.assertFalse(any(make_hello().cwd in text for text in thread.sent_messages))
            self.assertEqual(len(thread.sent_views), 1)
            view = cast(discord.ui.LayoutView, thread.sent_views[0])
            details = next(item for item in view.children if isinstance(item, discord.ui.Container))
            self.assertTrue(details.spoiler)
            marker = next(item.content for item in details.children if isinstance(item, discord.ui.TextDisplay))
            self.assertIn(marker, threads.expected_session_start_messages(make_hello()))
            self.assertEqual(
                threads.session_message_markers(cast(discord.Message, cast(object, await thread.fetch_message(901)))), ["", marker]
            )

    async def test_multicard_answer_has_one_heading_and_preserves_body(self) -> None:
        source = "First passage.\n\n" + "Later passage.\n" * 300
        views, attached = assistant_answer_cards(source)
        texts = [item.content for view in views for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
        self.assertGreater(len(views), 1)
        self.assertFalse(attached)
        self.assertEqual(
            [sum(isinstance(item, discord.ui.TextDisplay) for item in view.walk_children()) for view in views],
            [2, *[1 for _ in views[1:]]],
        )
        self.assertIn("First passage.", "\n".join(texts))
        self.assertEqual("\n".join(texts).count("Later passage."), 300)


class QuietTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_title_settles_before_first_rename_and_cosmetic_changes_are_quiet(self) -> None:
        thread, clock = title_tests.FakeThread("repo [abcdef]"), title_tests.FakeClock()
        workers = ThreadWorkers(
            cast(ThreadHooks, cast(object, title_tests.RenameOnlyHooks(lambda _thread_id, _epoch: thread))), clock=clock
        )
        self.addCleanup(workers.stop)
        workers.rename(1, "repo", "epoch")
        await title_tests.ThreadRenameTests.settle()
        workers.rename(1, "repo · Fix the login bug", "epoch")
        await title_tests.ThreadRenameTests.settle()
        self.assertEqual(thread.edits, [])
        await title_tests.ThreadRenameTests.pass_time(workers, clock, INITIAL_TITLE_SETTLE_SECONDS)
        self.assertEqual(thread.edits, ["repo · Fix the login bug"])
        workers.rename(1, "repo · Fix  the Login bug.", "epoch")
        await title_tests.ThreadRenameTests.settle()
        self.assertEqual(len(thread.edits), 1)
        workers.rename(1, "repo · Repair release upload", "epoch")
        await title_tests.ThreadRenameTests.settle()
        self.assertEqual(thread.edits[-1], "repo · Repair release upload")
