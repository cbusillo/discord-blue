from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import discord

from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.director_questions import (
    QuestionAnswerButton,
    QuestionAnswerModal,
    QuestionCards,
    detect_questions,
)
from discord_blue.doodads.agent_session.store import QuestionState, SessionStore, StoredQuestion
from tests.fakes_agent_prompts import PromptFixture, prompt_fixture
from tests.fakes_agent_session import FakeInteraction, FakeReplyMessage, FakeThread, FakeWebSocket, visible_view_text


class QuestionDetectionTests(unittest.TestCase):
    def test_only_live_top_level_questions_keep_their_full_context(self) -> None:
        source = """> **Q1.** Quoted?
| **Q2.** | Table |
````text
**Q3.** Code?
````
    **Q4.** Indented code?
- **Q5.** List item?
**Q6.** Ship this?

Recommendation: yes. Cost: upkeep.
Alternative: wait.

**Q7: Keep both?**
Recommendation: keep both.
"""
        questions = detect_questions(source)
        self.assertEqual([number for number, _ in questions], ["Q6", "Q7"])
        self.assertIn("Alternative: wait.", questions[0][1])
        self.assertIn("Recommendation: keep both.", questions[1][1])

    def test_references_and_historical_labels_are_not_questions(self) -> None:
        for source in ("**Q12** was answered yesterday.", "**Q12 status**\nDone.", "See **Q12.** Later?", "**Q12.** Resolved."):
            with self.subTest(source=source):
                self.assertEqual(detect_questions(source), ())


class QuestionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture: PromptFixture = self.enterContext(prompt_fixture())
        self.fixture.session.acknowledged = True
        self.fixture.bridge.bot.config.agent_session.enabled = True
        temporary = self.enterContext(TemporaryDirectory())
        self.fixture.bridge.store = SessionStore(Path(temporary) / "sessions.json")
        await self.fixture.bridge.store.start()
        self.addAsyncCleanup(self.fixture.bridge.store.close)
        self.enterContext(patch.dict(self.fixture.thread.__dict__, get_partial_message=self.fixture.thread._messages.__getitem__))
        self.parent = FakeThread(777)
        cast(Any, self.parent).get_partial_message = self.parent._messages.__getitem__
        self.enterContext(patch.dict(self.fixture.thread.__dict__, parent=self.parent))
        self.fixture.thread.parent_id = self.parent.id
        self.enterContext(patch.object(discord, "TextChannel", FakeThread))
        self.enterContext(
            patch.object(
                self.fixture.bridge.bot,
                "get_channel",
                side_effect=lambda key: self.parent if key == self.parent.id else self.fixture.thread,
            )
        )
        self.enterContext(patch.dict(self.fixture.bridge.bot.__dict__, add_view=Mock()))

    async def card(
        self, text: str = "**Q12.** Ship it?\nRecommendation: yes.\nCost: upkeep.\nAlternative: wait."
    ) -> QuestionAnswerModal:
        await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, text)
        view = cast(discord.ui.LayoutView, self.fixture.thread.sent_views[-1])
        button = next(item for item in view.walk_children() if isinstance(item, QuestionAnswerButton))
        interaction = FakeInteraction(self.fixture.thread)
        await button.callback(cast(Any, interaction))
        return cast(QuestionAnswerModal, interaction.response.modals[0])

    async def test_questions_replace_plain_text_in_order_without_parent_posts_or_notifications(self) -> None:
        text = (
            "Opening prose.\n\n**Q12.** Ship it?\n\nRecommendation: yes.\nCost: upkeep.\nAlternative: wait."
            "\n\nClosing prose.\n\n**Q13.** Keep both?"
        )
        await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, text)
        views = self.fixture.thread.sent_views
        self.assertEqual(len(views), 4)
        self.assertIn("Opening prose.", visible_view_text(views[0]))
        self.assertIn("Alternative: wait.", visible_view_text(views[1]))
        self.assertIn("Closing prose.", visible_view_text(views[2]))
        self.assertIn("Q13", visible_view_text(views[3]))
        self.assertEqual(sum(visible_view_text(view).count("Q12") for view in views), 1)
        self.assertEqual(self.parent.sent_messages, [])
        for payload in self.fixture.thread.sent_kwargs:
            self.assertTrue(payload["silent"])
            self.assertEqual(cast(discord.AllowedMentions, payload["allowed_mentions"]).to_dict()["parse"], [])

    async def test_form_answer_collapses_once_and_native_ack_cannot_reopen_it(self) -> None:
        modal = await self.card()
        original = self.fixture.thread._history[-1]
        waiting_color = cast(discord.ui.Container, cast(discord.ui.LayoutView, original.view).children[0]).accent_color
        modal.fields[0]._value = "Ship it"
        await asyncio.gather(*(modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread))) for _ in range(2)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)
        self.assertIn("Q12: Ship it", str(self.fixture.socket.sent_json[0]["text"]))
        await self.fixture.bridge.controls.handle_command_ack(self.fixture.socket.sent_json[0])
        stored = cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key))
        self.assertEqual(stored.state, QuestionState.ANSWERED)
        self.assertEqual(stored.decision_url, self.fixture.thread._history[1].jump_url)
        shown = visible_view_text(original.view)
        self.assertIn("Ship it", shown)
        self.assertNotIn("\n", shown)
        self.assertIn(stored.decision_url, shown)
        self.assertFalse(
            any(isinstance(item, discord.ui.Button) for item in cast(discord.ui.LayoutView, original.view).walk_children())
        )
        self.assertNotEqual(
            cast(discord.ui.Container, cast(discord.ui.LayoutView, original.view).children[0]).accent_color, waiting_color
        )
        self.assertEqual(cast(discord.AllowedMentions, original.edit_kwargs[-1]["allowed_mentions"]).to_dict()["parse"], [])

    async def test_plain_reply_and_linked_recorded_decision_collapse_only_the_named_questions(self) -> None:
        first = await self.card()
        second = await self.card("**Q13.** Keep both?")
        message = FakeReplyMessage(500, self.fixture.thread, "Q12 yes, ship it")
        await self.fixture.bridge.controls.send_thread_reply(cast(Any, message))
        self.assertEqual(
            cast(StoredQuestion, first.cards.record(first.session_id, first.question.key)).state, QuestionState.ANSWERED
        )
        self.assertEqual(
            cast(StoredQuestion, second.cards.record(second.session_id, second.question.key)).state, QuestionState.WAITING
        )
        await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, "Q13 was mentioned. Not a decision.")
        self.assertEqual(
            cast(StoredQuestion, second.cards.record(second.session_id, second.question.key)).state, QuestionState.WAITING
        )
        url = "https://github.com/cbusillo/discord-blue/issues/304#issuecomment-123"
        await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, f"Director decision: Q13. Keep both.\n{url}")
        stored = cast(StoredQuestion, second.cards.record(second.session_id, second.question.key))
        self.assertEqual(stored.state, QuestionState.ANSWERED)
        self.assertEqual(stored.decision_url, url)

    async def test_quoted_decision_examples_do_not_answer_a_question(self) -> None:
        modal = await self.card()
        url = "https://github.com/cbusillo/discord-blue/issues/304#issuecomment-123"
        for text in (
            f"```text\nDirector decision: Q12. Yes. {url}\n```",
            f"> Director decision: Q12. Yes. {url}",
            f"| Example | Link |\n| --- | --- |\n| Director decision: Q12. Yes. | {url} |",
            "Director decision: Q12. Yes, but no record link.",
        ):
            await modal.cards.decisions(modal.session_id, text, recorded=True)
            self.assertEqual(
                cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, QuestionState.WAITING
            )

    async def test_an_offline_reply_records_the_decision_without_claiming_native_delivery(self) -> None:
        modal = await self.card()
        self.fixture.socket.closed = True
        message = FakeReplyMessage(500, self.fixture.thread, "Q12 yes")
        await self.fixture.bridge.controls.send_thread_reply(cast(Any, message))
        self.assertEqual(
            cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, QuestionState.ANSWERED
        )
        self.assertEqual(self.fixture.socket.sent_json, [])
        self.assertTrue(message.replies)

    async def test_stale_or_changed_forms_never_deliver(self) -> None:
        for index, invalid in enumerate(("epoch", "source", "operator", "disabled", "replaced", "offline", "wrong-channel")):
            with self.subTest(invalid=invalid):
                modal = await self.card(f"**Q{20 + index}.** Ship {invalid}?")
                modal.fields[0]._value = "Yes"
                interaction = FakeInteraction(self.fixture.thread)
                original = self.fixture.session.hello
                if invalid == "epoch":
                    self.fixture.session.hello = replace(original, session_epoch="new")
                elif invalid == "source":
                    self.fixture.thread._history[-1].components = []
                elif invalid == "disabled":
                    self.fixture.bridge.bot.config.agent_session.enabled = False
                elif invalid == "replaced":
                    self.fixture.bridge.sessions.register(replace(self.fixture.session))
                elif invalid == "offline":
                    self.fixture.socket.closed = True
                elif invalid == "wrong-channel":
                    interaction.channel = FakeThread(888)
                with patch.object(AgentSessionBridge, "is_operator", return_value=invalid != "operator"):
                    await modal.on_submit(cast(Any, interaction))
                self.assertEqual(self.fixture.socket.sent_json, [])
                self.assertTrue(interaction.response.messages)
                self.fixture.session.hello = original
                self.fixture.bridge.sessions.register(self.fixture.session)
                self.fixture.bridge.bot.config.agent_session.enabled = True
                self.fixture.socket.closed = False

    async def test_recovery_deduplicates_each_number_and_preserves_answered_state(self) -> None:
        modal = await self.card()
        modal.fields[0]._value = "Yes"
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        await self.fixture.bridge.store.flush()
        loaded = SessionStore(self.fixture.bridge.store.path)
        await loaded.start()
        self.addAsyncCleanup(loaded.close)
        self.fixture.bridge.store = loaded
        recovered = QuestionCards(self.fixture.bridge)
        recovered.restore()
        count = len(self.fixture.thread.sent_messages)
        await recovered.send_answer(self.fixture.session, cast(discord.Thread, self.fixture.thread), "**Q12.** Ship it?")
        self.assertEqual(len(self.fixture.thread.sent_messages), count)
        stored = cast(StoredQuestion, recovered.record(modal.session_id, modal.question.key))
        self.assertEqual(stored.state, QuestionState.ANSWERED)
        self.assertEqual(stored.answer, "Yes")
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)

    async def test_session_end_keeps_unanswered_question_content_and_answered_summary(self) -> None:
        first = await self.card()
        second = await self.card("**Q13.** Keep both?")
        await second.cards.decisions(second.session_id, "Q13 yes", url="https://discord.com/channels/1/2/3")
        await first.cards.close(first.session_id)
        stored = cast(StoredQuestion, first.cards.record(first.session_id, first.question.key))
        view = first.cards.view(first.session_id, stored)
        self.assertIn("Ship it?", visible_view_text(view))
        self.assertIn("Unanswered", visible_view_text(view))
        self.assertFalse(any(isinstance(item, discord.ui.Button) for item in view.walk_children()))
        self.assertEqual(
            cast(StoredQuestion, second.cards.record(second.session_id, second.question.key)).state, QuestionState.ANSWERED
        )

    async def test_large_context_attachment_and_forbidden_fallback_stay_in_thread(self) -> None:
        body = "**Q14.** Proceed?\nRecommendation: yes.\nCost: " + "large " * 1000 + "\nAlternative: wait."
        modal = await self.card(body)
        self.assertEqual(self.fixture.thread.sent_files[-1].decode(), body)
        self.assertTrue(
            any(
                isinstance(item, discord.ui.File)
                for item in cast(discord.ui.LayoutView, self.fixture.thread.sent_views[-1]).walk_children()
            )
        )
        self.fixture.thread.deny_attachments = True
        fallback = await self.card(body.replace("Q14", "Q15"))
        self.assertFalse(
            any(
                isinstance(item, discord.ui.File)
                for item in cast(discord.ui.LayoutView, self.fixture.thread.sent_views[-1]).walk_children()
            )
        )
        self.assertEqual(self.parent.sent_messages, [])
        self.assertNotEqual(modal.question.key, fallback.question.key)

    async def test_failed_durable_reservation_refuses_delivery_and_transient_source_read_can_retry(self) -> None:
        modal = await self.card()
        modal.fields[0]._value = "Yes"
        with patch.object(SessionStore, "flush", new=AsyncMock(side_effect=OSError("disk unavailable"))):
            await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(self.fixture.socket.sent_json, [])
        fresh = await self.card("**Q17.** Proceed?")
        fresh.fields[0]._value = "Yes"
        with patch.object(FakeThread, "fetch_message", new=AsyncMock(side_effect=TimeoutError)):
            await fresh.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(cast(StoredQuestion, fresh.cards.record(fresh.session_id, fresh.question.key)).state, QuestionState.WAITING)
        await fresh.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)

    async def test_form_answer_remains_recorded_when_native_delivery_is_uncertain(self) -> None:
        modal = await self.card()
        modal.fields[0]._value = "Yes"
        with patch.object(FakeWebSocket, "send_json", side_effect=OSError("connection lost")):
            await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(
            cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, QuestionState.ANSWERED
        )
        self.assertEqual(self.fixture.socket.sent_json, [])
        self.assertTrue(self.fixture.thread.sent_messages[-1])
