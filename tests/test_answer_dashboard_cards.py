from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable
from aiohttp import web
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import discord
from discord.http import handle_message_parameters

from discord_blue.doodads.agent_session.cards import (
    ANSWER_CHUNK_LIMIT,
    MAX_INLINE_ANSWER_CARDS,
    DASHBOARD_PAGE_SIZE,
    active_session_cards,
    assistant_answer_cards,
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
    async def test_complete_and_recovered_answers_share_inline_cards_for_both_harnesses(self) -> None:
        for harness in ("codex", "claude"):
            for answer in ("Fixed the phone layout.", "```python\n" + 'print("hello")\n' * 600 + "```"):
                with self.subTest(harness=harness, long=len(answer) > ANSWER_CHUNK_LIMIT), prompt_fixture() as fixture:
                    bridge, session, thread = fixture.bridge, fixture.session, fixture.thread
                    session.hello.harness = harness
                    await bridge.status.handle_session_status(
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
                    self.assertGreaterEqual(len(views), 1)
                    view = views[0]
                    with handle_message_parameters(view=view, allowed_mentions=discord.AllowedMentions.none()) as params:
                        payload = params.payload
                        assert payload is not None
                        self.assertFalse(payload.get("content"))
                        self.assertTrue(discord.MessageFlags._from_value(payload["flags"]).components_v2)
                        self.assertFalse(payload["allowed_mentions"]["parse"])
                    answer_index = thread.sent_views.index(view)
                    self.assertFalse(thread.sent_kwargs[answer_index].get("silent", False))
                    self.assertFalse(thread.sent_files)
                    displayed = "\n".join(text_in(card) for card in views)
                    self.assertIn('print("hello")' if answer.startswith("```") else answer, displayed)
                    if answer.startswith("```"):
                        self.assertEqual(displayed.count('print("hello")'), answer.count('print("hello")'))
                    for card in views:
                        self.assertLessEqual(len(text_in(card)), 4000)
                        if answer.startswith("```"):
                            self.assertEqual(text_in(card).count("```"), 2)
                    posted = len(thread.sent_messages)
                    session.hello.assistant_message = answer
                    await bridge.session_threads.backfill_latest_assistant_message(cast(discord.Thread, thread), session.hello)
                    self.assertEqual(len(thread.sent_messages), posted, "V2 history must prevent duplicate backfill")

    async def test_blank_answers_do_not_send_and_line_dense_answers_remain_inline(self) -> None:
        with prompt_fixture() as fixture:
            for answer in ("", "  ", "\n", mark_assistant_message("  ")):
                await fixture.bridge.status.post_assistant_message(fixture.thread.id, answer)
                fixture.session.hello.assistant_message = answer
                await fixture.bridge.session_threads.backfill_latest_assistant_message(
                    cast(discord.Thread, fixture.thread), fixture.session.hello
                )
            self.assertFalse(fixture.thread.sent_messages)
            answer = "short line\n" * 30
            await fixture.bridge.status.post_assistant_message(fixture.thread.id, answer)
            self.assertFalse(fixture.thread.sent_files)
            self.assertIn(answer.strip(), fixture.thread.sent_messages[0])

    async def test_denied_attachment_preserves_text_delivery_and_completion_controls(self) -> None:
        with prompt_fixture() as fixture:
            fixture.thread.deny_attachments = True
            answer = "fallback line\n" * (ANSWER_CHUNK_LIMIT * MAX_INLINE_ANSWER_CARDS) + "\n**Q901:** Ship this?\nRecommend yes."
            await fixture.bridge.status.handle_session_status(
                "turn_complete",
                SessionStatus(
                    session_id=fixture.session.session_id,
                    session_epoch=fixture.session.session_epoch,
                    message="Turn complete",
                    assistant_message=answer,
                ),
            )
            self.assertIn("**Q901:** Ship this?", "\n".join(fixture.thread.sent_messages))
            self.assertFalse(fixture.thread.sent_files)
            self.assertIsNotNone(fixture.session.control_message_id)
            self.assertEqual(fixture.session.display_state, "done")
            self.assertFalse(fixture.thread.sent_kwargs[0].get("silent", False))
            fixture.session.hello.assistant_message = answer
            count = len(fixture.thread.sent_messages)
            await fixture.bridge.session_threads.backfill_latest_assistant_message(
                cast(discord.Thread, fixture.thread), fixture.session.hello
            )
            self.assertEqual(len(fixture.thread.sent_messages), count)

    async def test_huge_answers_attach_full_source_and_keep_every_question_inline(self) -> None:
        with prompt_fixture() as fixture:
            questions = [
                "**Q901:** Shall we ship?\n\nRecommend yes. The ongoing cost is one release.",
                "### **Q902**: Keep the fallback?\n- Yes\n- No",
                "**Q903: Choose the next step.**\n" + "A long question detail. " * 250,
            ]
            filler = "Background detail. " * (ANSWER_CHUNK_LIMIT * MAX_INLINE_ANSWER_CARDS)
            answer = f"{filler}\n\n" + "\n\n## Next topic\n\n".join(questions)
            await fixture.bridge.status.post_assistant_message(fixture.thread.id, answer)
            self.assertEqual(fixture.thread.sent_files, [answer.encode()])
            views = [view for view in fixture.thread.sent_views if isinstance(view, discord.ui.LayoutView)]
            displayed = "\n".join(text_in(view) for view in views)
            self.assertLess(displayed.index("Q901"), displayed.index("Q902"))
            self.assertLess(displayed.index("Q902"), displayed.index("Q903"))
            self.assertIn("Recommend yes. The ongoing cost is one release.", displayed)
            self.assertIn("- Yes\n- No", displayed)
            bodies = " ".join(
                item.content.removesuffix(mark_assistant_message(""))
                for view in views
                for item in view.walk_children()
                if isinstance(item, discord.ui.TextDisplay) and is_assistant_message(item.content)
            )
            self.assertEqual(" ".join(bodies.split()).count("A long question detail."), 250)
            self.assertNotIn("Background detail.", displayed)
            for view in views:
                self.assertLessEqual(len(text_in(view)), 4000)
                self.assertFalse(any(isinstance(item, discord.ui.Button) for item in view.walk_children()))

    def test_inline_boundary_keeps_complete_text_and_huge_output_attaches(self) -> None:
        for length in (
            ANSWER_CHUNK_LIMIT - 1,
            ANSWER_CHUNK_LIMIT,
            ANSWER_CHUNK_LIMIT + 1,
            ANSWER_CHUNK_LIMIT * MAX_INLINE_ANSWER_CARDS,
            ANSWER_CHUNK_LIMIT * MAX_INLINE_ANSWER_CARDS + 1,
        ):
            with self.subTest(length=length):
                answer = "x" * length
                views, attached = assistant_answer_cards(answer)
                bodies = [
                    item.content.removesuffix(mark_assistant_message(""))
                    for view in views
                    for item in view.walk_children()
                    if isinstance(item, discord.ui.TextDisplay) and is_assistant_message(item.content)
                ]
                if attached:
                    self.assertLess(len("".join(bodies)), len(answer))
                    self.assertTrue(any(isinstance(item, discord.ui.File) for view in views for item in view.walk_children()))
                else:
                    self.assertEqual("".join(bodies), answer)
                for view in views:
                    self.assertLessEqual(len(text_in(view)), 4000)

    async def test_multipart_answer_notifies_once_and_recovers_missing_tail_after_partial_send(self) -> None:
        with prompt_fixture() as fixture:
            await fixture.bridge.status.post_assistant_message(fixture.thread.id, "Previous complete answer")
            answer = "A new answer. " * 500 + "\n\n**Q999:** Is the recovered tail readable?"
            original_send = fixture.thread.send
            attempts = 0

            async def fail_second(content: str | None = None, **kwargs: object) -> object:
                nonlocal attempts
                attempts += 1
                if attempts == 2:
                    raise RuntimeError("Interrupted delivery")
                return await original_send(content, **kwargs)

            with patch.object(fixture.thread, "send", side_effect=fail_second), self.assertRaises(RuntimeError):
                await fixture.bridge.status.post_assistant_message(fixture.thread.id, answer)
            self.assertFalse(await fixture.bridge.session_threads.thread_has_assistant_message(cast(discord.Thread, fixture.thread)))
            fixture.session.hello.assistant_message = answer
            start = len(fixture.thread.sent_messages)
            await fixture.bridge.session_threads.backfill_latest_assistant_message(
                cast(discord.Thread, fixture.thread), fixture.session.hello
            )
            self.assertIn("**Q999:** Is the recovered tail readable?", "\n".join(fixture.thread.sent_messages[start:]))
            self.assertEqual(sum(not kwargs["silent"] for kwargs in fixture.thread.sent_kwargs[start:]), 1)
            self.assertTrue(await fixture.bridge.session_threads.thread_has_assistant_message(cast(discord.Thread, fixture.thread)))

    def test_huge_output_keeps_questions_without_unrelated_report_or_inline_references(self) -> None:
        answer = "**Q901:** May we continue?\n\nRecommend yes.\n\n" + "report line\n" * 10000
        answer += "\n\nAs noted in **Q902** above, this is a reference."
        views, attached = assistant_answer_cards(answer)
        self.assertTrue(attached)
        self.assertEqual(len(views), 1)
        displayed = text_in(views[0])
        self.assertIn("**Q901:** May we continue?", displayed)
        self.assertIn("Recommend yes.", displayed)
        self.assertNotIn("report line", displayed)
        self.assertNotIn("Q902", displayed)

    def test_emoji_heavy_answers_fit_discord_text_budget(self) -> None:
        views, _ = assistant_answer_cards("🟢" * 4000)
        for view in views:
            self.assertLessEqual(len(text_in(view).encode("utf-16-le")) // 2, 4000)

    async def test_only_bot_authored_cards_count_as_answers_in_history(self) -> None:
        with prompt_fixture() as fixture:
            await fixture.bridge.status.post_assistant_message(fixture.thread.id, "Answer from the agent")
            message = fixture.thread._history[-1]
            self.assertEqual(message.content, "")
            self.assertTrue(fixture.bridge.session_threads.is_bot_assistant_message(cast(discord.Message, message)))
            # A Discord readback that normalizes invisible text still retains the card ID.
            for container in message.components:
                for child in container.children:
                    if isinstance(child, discord.TextDisplay):
                        child.content = child.content.replace(mark_assistant_message(""), "")
            self.assertTrue(fixture.bridge.session_threads.is_bot_assistant_message(cast(discord.Message, message)))
            message.author.id = 123
            self.assertFalse(fixture.bridge.session_threads.is_bot_assistant_message(cast(discord.Message, message)))
            bot_user = fixture.bridge.bot.user
            assert bot_user is not None
            message.author.id = bot_user.id
            await message.edit(view=discord.ui.LayoutView())
            self.assertFalse(fixture.bridge.session_threads.is_bot_assistant_message(cast(discord.Message, message)))

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
            fixture.bridge.bot.config.discord.guild_id = 0
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
                guild_id=42,
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
                for item in call.kwargs["view"].walk_children():
                    if isinstance(item, discord.ui.Button):
                        assert item.url is not None
                        self.assertIn("/42/", item.url)
            self.assertNotIn("dashboard-0", text)
            for index in range(1, DASHBOARD_PAGE_SIZE + 2):
                self.assertIn(f"dashboard-{index}", text)
