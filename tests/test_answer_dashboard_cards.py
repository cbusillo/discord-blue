from __future__ import annotations

import time
import unittest
from collections.abc import Awaitable, Callable
from aiohttp import web
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import discord
from discord.http import handle_message_parameters

from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.cards import (
    ANSWER_CHUNK_LIMIT,
    MAX_INLINE_ANSWER_CARDS,
    DASHBOARD_PAGE_SIZE,
    assistant_answer_cards,
)
from discord_blue.doodads.agent_session.dashboard import ActiveSessionDashboard, DashboardButton, DashboardFilter
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
            self.assertFalse(
                any(
                    isinstance(item, discord.ui.File)
                    for view in fixture.thread.sent_views
                    if isinstance(view, discord.ui.LayoutView)
                    for item in view.walk_children()
                )
            )
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
            self.assertGreater(" ".join(bodies.split()).count("A long question detail."), 0)
            self.assertLess(len(bodies), ANSWER_CHUNK_LIMIT * len(questions) * 2)
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

    def test_huge_question_excerpts_preserve_lists_quotes_options_and_later_questions(self) -> None:
        questions = [
            "1. **Q901:** Ship?\n\nRecommend yes.\n\n- Option A\n- Option B",
            "> **Q902:** Continue?\n>\n> Recommend yes.",
            "> - **Q903:** Keep both?",
            "+ **Q904:** Finish?\n```text\n\n**Q905:** This is code, not a question.\n```",
        ]
        source = "\n\n## Next section\n\n".join(questions)
        source += "\n\n## Report\n" + "report line\n" * 10000
        source += "\n**Q906:** Glued report?\n" + "report line\n" * 10000
        source += "\n**Q907:** Is the tail visible?"
        views, attached = assistant_answer_cards(source)
        self.assertTrue(attached)
        displayed = "\n".join(text_in(view) for view in views)
        for number in (901, 902, 903, 904, 906, 907):
            self.assertIn(f"**Q{number}", displayed)
        self.assertLess(displayed.index("Q906"), displayed.index("Q907"))
        self.assertIn("Recommend yes.", displayed)
        self.assertIn("- Option A\n- Option B", displayed)
        self.assertIn("**Q905:** This is code, not a question.", displayed)
        self.assertLess(len(displayed), ANSWER_CHUNK_LIMIT * len(questions) * 2)

    def test_huge_output_scans_all_questions_without_extracting_fenced_examples(self) -> None:
        source = "```text\n**Q999:** Fenced example.\n```\n\n## Report\n" + "background\n" * 10000
        numbers = list(range(100, 100 + MAX_INLINE_ANSWER_CARDS * 4))
        source += "\n" + "\n".join(f"{index}. **Q{number}:** Continue?" for index, number in enumerate(numbers, 1))
        views, attached = assistant_answer_cards(source)
        self.assertTrue(attached)
        displayed = "\n".join(text_in(view) for view in views)
        self.assertNotIn("Q999", displayed)
        for number in numbers:
            self.assertIn(f"**Q{number}", displayed)

    def test_huge_report_indentation_does_not_stall_question_extraction(self) -> None:
        source = "Background. " * 10000 + "\n" + " " * 20000 + "ordinary report\n**Q901:** Continue?"
        source += "\n" + ">" + "\t>" * 20000 + "ordinary quoted report\n> **Q902:** Quoted tail?"
        started = time.process_time()
        views, attached = assistant_answer_cards(source)
        self.assertTrue(attached)
        self.assertIn("**Q901:** Continue?", "\n".join(text_in(view) for view in views))
        self.assertIn("**Q902:** Quoted tail?", "\n".join(text_in(view) for view in views))
        self.assertLess(time.process_time() - started, 1.0)

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

    async def test_dashboard_pages_in_place_with_live_filters_and_churn(self) -> None:
        with prompt_fixture() as fixture:
            fixture.bridge.bot.config.agent_session.enabled = True
            fixture.bridge.sessions.by_session.clear()
            sessions = []
            for index in range(DASHBOARD_PAGE_SIZE * 3 + 1):
                hello = make_hello()
                hello.session_id = str(index)
                hello.title = f"Session {index}"
                hello.harness = "codex" if index % 2 else "claude"
                hello.host_label = "Host " + "h" * 1000
                session = AgentSession(
                    hello=hello, websocket=cast(web.WebSocketResponse, cast(object, FakeWebSocket())), thread_id=1000 + index
                )
                session.display_state = "working" if index < DASHBOARD_PAGE_SIZE * 3 else "unknown"
                session.last_status_message = mark_assistant_message("Details " + "d" * 1000)
                fixture.bridge.sessions.register(session)
                sessions.append(session)
            dashboard = ActiveSessionDashboard(fixture.bridge, 123, 42)
            self.assertIn(sessions[-1].hello.title, text_in(dashboard))
            interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
            links: list[str | None] = []
            for page in range(4):
                with handle_message_parameters(view=dashboard) as params:
                    payload = params.payload
                    assert payload is not None
                    self.assertFalse(payload.get("content"))
                items = list(dashboard.walk_children())
                self.assertLessEqual(len(items), 40)
                self.assertLessEqual(len(text_in(dashboard)), 4000)
                self.assertFalse(is_assistant_message(text_in(dashboard)))
                links.extend(cast(discord.ui.Button, item).url for item in items if isinstance(item, discord.ui.Button) and item.url)
                if page < 3:
                    button = next(item for item in items if isinstance(item, DashboardButton) and item.action == "next")
                    self.assertFalse(cast(DashboardButton, button).disabled)
                    await button.callback(cast(discord.Interaction, cast(object, interaction)))
            self.assertCountEqual(links, [f"https://discord.com/channels/42/{session.thread_id}" for session in sessions])
            last = next(item for item in dashboard.walk_children() if isinstance(item, DashboardButton) and item.action == "next")
            self.assertTrue(cast(DashboardButton, last).disabled)
            status = next(item for item in dashboard.walk_children() if isinstance(item, DashboardFilter) and item.kind == "status")
            status._values = ["waiting"]
            await status.callback(cast(discord.Interaction, cast(object, interaction)))
            self.assertEqual(dashboard.page, 0)
            self.assertEqual(
                [
                    cast(discord.ui.Button, item).url
                    for item in dashboard.walk_children()
                    if isinstance(item, discord.ui.Button) and item.url
                ],
                [f"https://discord.com/channels/42/{sessions[-1].thread_id}"],
            )
            await dashboard.update(cast(discord.Interaction, cast(object, interaction)), "status", "working")
            await dashboard.update(cast(discord.Interaction, cast(object, interaction)), "harness", "codex")
            await dashboard.update(cast(discord.Interaction, cast(object, interaction)), "next")
            self.assertEqual(dashboard.page, 1)
            for session in sessions[1:]:
                cast(FakeWebSocket, cast(object, session.websocket)).closed = True
            await dashboard.update(cast(discord.Interaction, cast(object, interaction)), "refresh")
            self.assertEqual(dashboard.page, 0)
            self.assertFalse(any(isinstance(item, discord.ui.Button) and item.url for item in dashboard.walk_children()))
            await dashboard.update(cast(discord.Interaction, cast(object, interaction)), "harness", "all")
            self.assertIn(sessions[0].hello.title, text_in(dashboard))
            sessions[0].display_state = "failed"
            await dashboard.update(cast(discord.Interaction, cast(object, interaction)), "status", "failed")
            self.assertIn(sessions[0].hello.title, text_in(dashboard))
            await dashboard.update(cast(discord.Interaction, cast(object, interaction)), "status", "done")
            self.assertFalse(any(isinstance(item, discord.ui.Button) and item.url for item in dashboard.walk_children()))
            for call in interaction.edit_original_response.call_args_list:
                self.assertIs(call.kwargs["view"], dashboard)
                self.assertEqual(call.kwargs["allowed_mentions"].to_dict()["parse"], [])
            self.assertEqual(interaction.response.defer.await_count, interaction.edit_original_response.await_count)

    async def test_active_command_sends_one_private_dashboard_and_rechecks_operator(self) -> None:
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
            interaction.response.send_message.assert_awaited_once()
            interaction.followup.send.assert_not_awaited()
            kwargs = interaction.response.send_message.call_args.kwargs
            self.assertTrue(kwargs["ephemeral"])
            self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
            dashboard = kwargs["view"]
            self.assertIsInstance(dashboard, ActiveSessionDashboard)
            self.assertNotIn("dashboard-0", text_in(dashboard))
            self.assertTrue(await dashboard.interaction_check(cast(discord.Interaction, cast(object, interaction))))
            interaction.user.id = 456
            self.assertFalse(await dashboard.interaction_check(cast(discord.Interaction, cast(object, interaction))))
            interaction.user.id = 123
            with patch.object(AgentSessionBridge, "is_operator", return_value=False):
                self.assertFalse(await dashboard.interaction_check(cast(discord.Interaction, cast(object, interaction))))
            fixture.bridge.bot.config.agent_session.enabled = False
            self.assertFalse(await dashboard.interaction_check(cast(discord.Interaction, cast(object, interaction))))
            dashboard.stop()
