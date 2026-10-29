from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from discord_blue.doodads.agent_session.formatting import filter_injected_tags, format_user_message
from discord_blue.doodads.agent_session.terminal_prompts import WEBHOOK_NAME, TERMINAL_SUBTEXT, CLIPPED_TERMINAL_SUBTEXT
from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT
from tests.fakes_agent_session import FakeThread
from discord_blue.doodads.agent_session.controls import SessionControls
from discord_blue.plugs.discord_plug import BlueBot
from discord_blue.doodads.agent_session.protocol import LAUNCH_BRIEF_ORIGIN, UserMessage
from discord_blue.doodads.agent_session.session_status import SessionStatusUpdates
from discord_blue.doodads.agent_session_doodad import AgentSessionDoodad
from tests.fakes_agent_prompts import prompt_fixture


class TerminalPromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fixture = self.enterContext(prompt_fixture())
        self.bridge, self.session, self.thread = fixture.bridge, fixture.session, fixture.thread
        self.member = SimpleNamespace(
            id=123, bot=False, display_name="Chris in this guild", display_avatar="https://cdn.example/avatar"
        )
        self.guild = SimpleNamespace(
            id=self.thread.guild.id, me=SimpleNamespace(id=999), get_member=lambda _: self.member, fetch_member=AsyncMock()
        )
        self.parent = MagicMock(spec=discord.TextChannel)
        self.parent.id = 444
        self.parent.permissions_for.return_value = discord.Permissions(manage_webhooks=True)
        self.hook = SimpleNamespace(
            name=WEBHOOK_NAME,
            user=self.guild.me,
            token="fake-token",
            type=discord.WebhookType.incoming,
            send=AsyncMock(),
        )
        self.parent.webhooks = AsyncMock(return_value=[])
        self.parent.create_webhook = AsyncMock(side_effect=self.create_webhook)
        self.thread.parent_id = self.parent.id
        self.thread.parent = self.parent
        self.thread.guild = self.guild
        self.bridge.bot.config.agent_session.auto_join_user_ids = [123]

    async def create_webhook(self, *, name: str, **_options: object) -> object:
        if any(word in name.casefold() for word in ("discord", "clyde")):  # Discord refuses these names with HTTP 400.
            raise discord.HTTPException(cast(Any, SimpleNamespace(status=400, reason="Bad Request")), "Invalid Form Body")
        return self.hook

    async def prompt(self, text: str = "Please fix this", origin: str | None = None) -> None:
        await self.bridge.status.handle_user_message(UserMessage(self.session.session_id, self.session.session_epoch, text, origin))

    async def test_both_harnesses_use_member_identity_and_plain_prompt_in_thread(self) -> None:
        for harness in ("claude", "codex"):
            self.session.hello.harness = harness
            await self.prompt("First line\nSecond line @everyone")
            content = self.hook.send.call_args.args[0]
            self.assertEqual(content, "First line\nSecond line @everyone" + TERMINAL_SUBTEXT)
            options = self.hook.send.call_args.kwargs
            self.assertIs(options["thread"], self.thread)
            self.assertEqual(options["username"], self.member.display_name)
            self.assertEqual(options["avatar_url"], str(self.member.display_avatar))
            self.assertTrue(options["wait"])
            self.assertEqual(options["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertEqual(self.thread.sent_messages, [])
        self.parent.create_webhook.assert_awaited_once()

    async def test_concurrent_threads_share_hook_and_restart_rediscovers_it(self) -> None:
        sibling = FakeThread(556)
        sibling.parent, sibling.parent_id, sibling.guild = self.parent, self.parent.id, self.guild
        await asyncio.gather(
            self.bridge.status.terminal_prompts.send(cast(discord.Thread, cast(object, self.thread)), "First thread"),
            self.bridge.status.terminal_prompts.send(cast(discord.Thread, cast(object, sibling)), "Second thread"),
        )
        self.assertEqual([call.kwargs["thread"].id for call in self.hook.send.call_args_list], [555, 556])
        self.parent.create_webhook.assert_awaited_once()
        self.parent.webhooks.return_value = [self.hook]
        self.bridge.status = SessionStatusUpdates(self.bridge)
        self.thread.id = 556
        self.bridge.sessions.bind_thread(self.session.session_id, 556)
        await self.prompt()
        self.parent.create_webhook.assert_awaited_once()
        self.assertEqual(self.parent.webhooks.await_count, 2)

    async def test_missing_permission_falls_back_and_logs_once(self) -> None:
        self.parent.permissions_for.return_value.manage_webhooks = False
        with self.assertLogs("discord_blue.doodads.agent_session.terminal_prompts", level="WARNING") as logs:
            await self.prompt()
            await self.prompt()
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(len(self.thread.sent_messages), 2)
        self.assertEqual(self.thread.sent_messages[0], format_user_message("Please fix this"))
        self.parent.create_webhook.assert_not_awaited()

    async def test_automated_inputs_keep_labels_and_spawned_brief_stays_hidden(self) -> None:
        await self.prompt("Automated brief", LAUNCH_BRIEF_ORIGIN)
        await self.prompt("<task-notification><summary>Worker finished</summary></task-notification>")
        await self.prompt('<agent-message from="worker">Findings</agent-message>')
        await self.prompt('<channel source="other">Forwarded</channel>')
        self.session.hello.spawned = True
        await self.prompt("Hidden brief", LAUNCH_BRIEF_ORIGIN)
        self.hook.send.assert_not_awaited()
        self.assertEqual(len(self.thread.sent_messages), 4)
        self.assertEqual(
            self.thread.sent_messages[1],
            format_user_message(filter_injected_tags("<task-notification><summary>Worker finished</summary></task-notification>")),
        )

    async def test_ambiguous_or_non_operator_identity_uses_fallback(self) -> None:
        self.bridge.bot.config.agent_session.auto_join_user_ids = [123, 456]
        await self.prompt()
        self.bridge.bot.config.agent_session.auto_join_user_ids = [123]
        self.member.bot = True
        await self.prompt()
        self.hook.send.assert_not_awaited()
        self.assertEqual(len(self.thread.sent_messages), 2)

    async def test_a_reserved_display_name_keeps_the_bot_mirror(self) -> None:
        self.member.display_name = "Chris on Discord"
        await self.prompt()
        self.hook.send.assert_not_awaited()
        self.assertEqual(len(self.thread.sent_messages), 1)

    async def test_uncached_member_is_fetched_and_profile_changes_are_used(self) -> None:
        self.guild.get_member = lambda _: None
        self.guild.fetch_member.return_value = self.member
        await self.prompt()
        self.guild.fetch_member.assert_awaited_with(self.member.id)
        self.member.display_name = "Updated guild name"
        await self.prompt()
        self.assertEqual(self.hook.send.call_args.kwargs["username"], self.member.display_name)

    async def test_lookup_or_creation_refusal_keeps_the_bot_mirror(self) -> None:
        for operation in (self.parent.webhooks, self.parent.create_webhook, self.guild.fetch_member):
            with self.subTest(operation=operation):
                operation.side_effect = discord.Forbidden(cast(Any, SimpleNamespace(status=403, reason="Forbidden")), "denied")
                self.guild.get_member = lambda _: None
                self.guild.fetch_member.return_value = self.member
                await self.prompt()
                operation.side_effect = None
        self.assertEqual(len(self.thread.sent_messages), 3)
        self.hook.send.assert_not_awaited()

    async def test_long_prompt_stays_one_message_with_terminal_note(self) -> None:
        text = "hello " * DISCORD_MESSAGE_LIMIT
        await self.prompt(text)
        self.hook.send.assert_awaited_once()
        content = self.hook.send.call_args.args[0]
        self.assertLessEqual(len(content), DISCORD_MESSAGE_LIMIT)
        self.assertTrue(content.startswith(text[:1000]))
        self.assertTrue(content.endswith(CLIPPED_TERMINAL_SUBTEXT))
        self.assertEqual(self.thread.sent_messages, [])

    async def test_any_http_send_failure_falls_back_without_disconnect(self) -> None:
        for status in (400, 403, 404, 503):
            with self.subTest(status=status):
                self.hook.send.side_effect = discord.HTTPException(cast(Any, SimpleNamespace(status=status, reason="Failed")), "")
                await self.prompt()
        self.assertEqual(len(self.thread.sent_messages), 4)
        self.assertEqual(self.session.display_state, "working")

    async def test_webhook_input_is_ignored_even_with_human_author_flag(self) -> None:
        self.bridge.bot.config.agent_session.enabled = True
        cog = AgentSessionDoodad(cast(BlueBot, cast(object, self.bridge.bot)))
        message = SimpleNamespace(webhook_id=876, author=SimpleNamespace(bot=False, id=123))
        with patch.object(SessionControls, "send_thread_reply", new_callable=AsyncMock) as send:
            await cog.route_thread_reply(cast(discord.Message, cast(object, message)))
            send.assert_not_awaited()
