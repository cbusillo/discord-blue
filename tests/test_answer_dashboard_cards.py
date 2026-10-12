from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable
from aiohttp import web
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import discord
from discord.http import handle_message_parameters

from discord_blue.doodads.agent_session.cards import (
    ANSWER_PREVIEW_LIMIT,
    ANSWER_PREVIEW_LINE_LIMIT,
    DASHBOARD_PAGE_SIZE,
    active_session_cards,
)
from discord_blue.doodads.agent_session.formatting import is_assistant_message, mark_assistant_message
from discord_blue.doodads.agent_session.protocol import SessionStatus
from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.doodads.agent_session_doodad import AgentSessionDoodad
from tests.fakes_agent_prompts import prompt_fixture
from tests.fakes_agent_session import FakeWebSocket, make_hello


def text_in(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


class AnswerDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_and_recovered_answers_share_cards_and_full_file_for_both_harnesses(self) -> None:
        for harness in ("codex", "claude"):
            for answer in ("Fixed the phone layout.", "```python\n" + 'print("hello")\n' * ANSWER_PREVIEW_LIMIT + "```"):
                with self.subTest(harness=harness, long=len(answer) > ANSWER_PREVIEW_LIMIT), prompt_fixture() as fixture:
                    bridge, session, thread = fixture.bridge, fixture.session, fixture.thread
                    session.hello.harness = harness
                    await bridge.handle_session_status(
                        "turn_complete",
                        SessionStatus(
                            session_id=session.session_id,
                            session_epoch=session.session_epoch,
                            message="Turn complete",
                            assistant_message=answer,
                        ),
                    )
                    views = [
                        view
                        for view in thread.sent_views
                        if isinstance(view, discord.ui.LayoutView)
                        and any(
                            isinstance(item, discord.ui.TextDisplay) and is_assistant_message(item.content)
                            for item in view.walk_children()
                        )
                    ]
                    self.assertEqual(len(views), 1)
                    view = views[0]
                    with handle_message_parameters(view=view, allowed_mentions=discord.AllowedMentions.none()) as params:
                        payload = params.payload
                        assert payload is not None
                        self.assertFalse(payload.get("content"))
                        self.assertTrue(discord.MessageFlags._from_value(payload["flags"]).components_v2)
                        self.assertFalse(payload["allowed_mentions"]["parse"])
                    self.assertFalse(thread.sent_kwargs[0]["silent"])
                    files = [item for item in view.walk_children() if isinstance(item, discord.ui.File)]
                    if len(answer) > ANSWER_PREVIEW_LIMIT:
                        self.assertEqual(thread.sent_files, [answer.encode()])
                        self.assertEqual(len(files), 1)
                        self.assertEqual(files[0].media.url, f"attachment://{thread.sent_filenames[0]}")
                        self.assertLess(len(text_in(view)), len(answer))
                    else:
                        self.assertFalse(files)
                        self.assertIn(answer, text_in(view))
                    posted = len(thread.sent_messages)
                    session.hello.assistant_message = answer
                    await bridge.backfill_latest_assistant_message(cast(discord.Thread, thread), session.hello)
                    self.assertEqual(len(thread.sent_messages), posted, "V2 history must prevent duplicate backfill")

    async def test_blank_answers_do_not_send_or_notify_and_line_dense_answers_stay_compact(self) -> None:
        with prompt_fixture() as fixture:
            for answer in ("", "  ", "\n", mark_assistant_message("  ")):
                await fixture.bridge.post_assistant_message(fixture.thread.id, answer)
                fixture.session.hello.assistant_message = answer
                await fixture.bridge.backfill_latest_assistant_message(cast(discord.Thread, fixture.thread), fixture.session.hello)
            self.assertFalse(fixture.thread.sent_messages)
            answer = "short line\n" * (ANSWER_PREVIEW_LINE_LIMIT + 1)
            await fixture.bridge.post_assistant_message(fixture.thread.id, answer)
            self.assertEqual(fixture.thread.sent_files, [answer.encode()])
            self.assertLess(len(fixture.thread.sent_messages[0].splitlines()), len(answer.splitlines()))

    async def test_only_bot_authored_cards_count_as_answers_in_history(self) -> None:
        with prompt_fixture() as fixture:
            await fixture.bridge.post_assistant_message(fixture.thread.id, "Answer from the agent")
            message = await fixture.thread.fetch_message(901)
            self.assertEqual(message.content, "")
            self.assertTrue(fixture.bridge.is_bot_assistant_message(cast(discord.Message, message)))
            # A Discord readback that normalizes invisible text still retains the card ID.
            for container in message.components:
                for child in container.children:
                    if isinstance(child, discord.TextDisplay):
                        child.content = child.content.replace(mark_assistant_message(""), "")
            self.assertTrue(fixture.bridge.is_bot_assistant_message(cast(discord.Message, message)))
            message.author.id = 123
            self.assertFalse(fixture.bridge.is_bot_assistant_message(cast(discord.Message, message)))
            message.author.id = 999
            await message.edit(view=discord.ui.LayoutView())
            self.assertFalse(fixture.bridge.is_bot_assistant_message(cast(discord.Message, message)))

    async def test_dashboard_prioritizes_attention_and_includes_every_thread_in_bounded_pages(self) -> None:
        sessions = []
        for index in range(DASHBOARD_PAGE_SIZE * 3 + 1):
            hello = make_hello()
            hello.title = f"Session {index}"
            hello.session_id = str(index)
            hello.host_label = "Host " + "h" * 1000
            session = AgentSession(
                hello=hello, websocket=cast(web.WebSocketResponse, cast(object, FakeWebSocket())), thread_id=1000 + index
            )
            session.display_state = "working" if index < DASHBOARD_PAGE_SIZE * 3 else "waiting"
            session.last_status_message = mark_assistant_message("Details " + "d" * 1000)
            sessions.append(session)
        views = active_session_cards(sessions, guild_id=42)
        self.assertIn(sessions[-1].hello.title, text_in(views[0]))
        links: list[str | None] = []
        for view in views:
            with handle_message_parameters(view=view) as params:
                assert params.payload is not None
                self.assertFalse(params.payload.get("content"))
            items = list(view.walk_children())
            self.assertLessEqual(len(items), 40)  # Discord API component limit.
            self.assertLessEqual(len(text_in(view)), 4000)  # Discord V2 aggregate text limit.
            self.assertFalse(is_assistant_message(text_in(view)))
            links.extend(item.url for item in items if isinstance(item, discord.ui.Button))
        self.assertCountEqual(links, [f"https://discord.com/channels/42/{session.thread_id}" for session in sessions])
        empty = active_session_cards([], guild_id=42)
        self.assertEqual(len(empty), 1)
        self.assertFalse(any(isinstance(item, discord.ui.Button) for item in empty[0].walk_children()))

    async def test_active_command_pages_privately_and_excludes_disconnected_sessions(self) -> None:
        with prompt_fixture() as fixture:
            fixture.bridge.bot.config.agent_session.enabled = True
            fixture.bridge.bot.config.discord.guild_id = 42
            for index in range(DASHBOARD_PAGE_SIZE + 2):
                hello = make_hello()
                hello.session_id = f"dashboard-{index}"
                hello.title = hello.session_id
                session = AgentSession(
                    hello=hello,
                    websocket=cast(web.WebSocketResponse, cast(object, FakeWebSocket(closed=index == 0))),
                    thread_id=5000 + index,
                )
                fixture.bridge.sessions.register(session)
            cog = SimpleNamespace(bot=fixture.bridge.bot, bridge=fixture.bridge)
            interaction = SimpleNamespace(
                user=SimpleNamespace(id=123),
                response=SimpleNamespace(send_message=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )
            command = AgentSessionDoodad.code_group.get_command("active")
            assert isinstance(command, discord.app_commands.Command)
            await cast(Callable[[AgentSessionDoodad, discord.Interaction], Awaitable[None]], command.callback)(
                cast(AgentSessionDoodad, cast(object, cog)), cast(discord.Interaction, cast(object, interaction))
            )
            calls = interaction.response.send_message.call_args_list + interaction.followup.send.call_args_list
            self.assertGreater(len(calls), 1)
            text = ""
            for call in calls:
                self.assertTrue(call.kwargs["ephemeral"])
                self.assertEqual(call.kwargs["allowed_mentions"].to_dict()["parse"], [])
                text += text_in(call.kwargs["view"])
            self.assertNotIn("dashboard-0", text)
            for index in range(1, DASHBOARD_PAGE_SIZE + 2):
                self.assertIn(f"dashboard-{index}", text)
