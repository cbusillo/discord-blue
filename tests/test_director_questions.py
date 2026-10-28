from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import Mock, patch

import discord

from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.director_questions import (
    QuestionAnswerButton,
    QuestionAnswerModal,
    QuestionCards,
    detect_questions,
)
from discord_blue.doodads.agent_session.store import SessionStore, StoredQuestion
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
        self,
        text: str = (
            "**Q12.** Ship it?\nRecommendation: yes.\nCost: upkeep.\nAlternative: wait.\n\n**Q13.** Keep both?\nRecommendation: yes."
        ),
    ) -> QuestionAnswerModal:
        await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, text)
        view = cast(discord.ui.LayoutView, self.parent.sent_views[-1])
        button = next(item for item in view.walk_children() if isinstance(item, QuestionAnswerButton))
        interaction = FakeInteraction(self.parent)
        await button.callback(cast(Any, interaction))
        return cast(QuestionAnswerModal, interaction.response.modals[0])

    async def test_card_is_silent_and_combined_answer_is_dispatched_once_then_native_ack_marks_delivered(self) -> None:
        modal = await self.card()
        payload = self.parent.sent_kwargs[-1]
        self.assertTrue(payload["silent"])
        self.assertEqual(cast(discord.AllowedMentions, payload["allowed_mentions"]).to_dict()["parse"], [])
        for expected in ("Cost: upkeep.", "Alternative: wait.", self.fixture.thread._history[-1].jump_url):
            self.assertIn(expected, visible_view_text(self.parent.sent_views[-1]))
        modal.fields[0]._value = "Ship it"
        modal.fields[1]._value = "Keep both"
        await asyncio.gather(*(modal.on_submit(cast(Any, FakeInteraction(self.parent))) for _ in range(2)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)
        command = self.fixture.socket.sent_json[0]
        self.assertIn("Q12: Ship it", str(command["text"]))
        self.assertIn("Q13: Keep both", str(command["text"]))
        self.assertEqual(cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, "Uncertain")
        await self.fixture.bridge.controls.handle_command_ack(command)
        self.assertEqual(cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, "Delivered")
        self.assertIn("Delivered", visible_view_text(self.parent._history[-1].view))

    async def test_changed_session_source_and_operator_cannot_receive_a_form_answer(self) -> None:
        for invalid in ("epoch", "source", "operator", "disabled", "replaced", "offline", "wrong-channel"):
            with self.subTest(invalid=invalid):
                modal = await self.card(f"**Q12.** Ship {invalid}?")
                modal.fields[0]._value = "Yes"
                interaction = FakeInteraction(self.parent)
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

    async def test_recovery_deduplicates_and_preserves_consumed_card(self) -> None:
        modal = await self.card()
        modal.fields[0]._value = "Yes"
        modal.fields[1]._value = "Yes"
        await modal.on_submit(cast(Any, FakeInteraction(self.parent)))
        self.fixture.bridge.save_session(self.fixture.session, "live")
        await self.fixture.bridge.store.flush()
        loaded = SessionStore(self.fixture.bridge.store.path)
        await loaded.start()
        self.addAsyncCleanup(loaded.close)
        self.fixture.bridge.store = loaded
        recovered = QuestionCards(self.fixture.bridge)
        recovered.restore()
        await recovered.post(
            self.fixture.session,
            cast(discord.Thread, self.fixture.thread),
            (
                "**Q12.** Ship it?\nRecommendation: yes.\nCost: upkeep.\nAlternative: wait.\n\n"
                "**Q13.** Keep both?\nRecommendation: yes."
            ),
            [cast(discord.Message, self.fixture.thread._history[0])],
        )
        self.assertEqual(len(self.parent.sent_messages), 1)
        self.assertEqual(cast(StoredQuestion, recovered.record(modal.session_id, modal.question.key)).state, "Uncertain")
        await modal.on_submit(cast(Any, FakeInteraction(self.parent)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)

    async def test_ambiguous_send_failure_blocks_retry_and_keeps_uncertain(self) -> None:
        modal = await self.card("**Q14.** Proceed?")
        modal.fields[0]._value = "Yes"
        with patch.object(FakeWebSocket, "send_json", side_effect=OSError("connection lost")):
            await modal.on_submit(cast(Any, FakeInteraction(self.parent)))
        await modal.on_submit(cast(Any, FakeInteraction(self.parent)))
        self.assertEqual(self.fixture.socket.sent_json, [])
        self.assertEqual(cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, "Uncertain")

    async def test_large_question_context_is_attached_without_losing_alternatives(self) -> None:
        body = "**Q14.** Proceed?\nRecommendation: yes.\nCost: " + "large " * 1000 + "\nAlternative: wait."
        modal = await self.card(body)
        self.assertEqual(self.parent.sent_files[-1].decode(), body)
        self.assertIn(self.parent.sent_filenames[-1], visible_view_text(self.parent.sent_views[-1]))
        self.assertEqual(len(modal.fields), 1)

    async def test_many_questions_require_every_number_and_submit_together(self) -> None:
        modal = await self.card("\n\n".join(f"**Q{number}.** Proceed?" for number in range(20, 25)))
        self.assertEqual(len(modal.fields), 1)
        modal.fields[0]._value = "Q20: Yes"
        await modal.on_submit(cast(Any, FakeInteraction(self.parent)))
        self.assertEqual(self.fixture.socket.sent_json, [])
        modal.fields[0]._value = "\n".join(f"Q{number}: Yes" for number in range(20, 25))
        await modal.on_submit(cast(Any, FakeInteraction(self.parent)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)
        for number in range(20, 25):
            self.assertIn(f"Q{number}: Yes", str(self.fixture.socket.sent_json[0]["text"]))

    async def test_definite_failure_shows_not_delivered_and_a_thread_failure_notice(self) -> None:
        modal = await self.card("**Q14.** Proceed?")
        modal.fields[0]._value = "Yes"
        original = self.fixture.thread.send

        async def reconnect(content: str | None = None, **kwargs: object) -> FakeReplyMessage:
            message = await original(content, **kwargs)
            self.fixture.bridge.sessions.register(replace(self.fixture.session))
            return message

        with patch.object(FakeThread, "send", side_effect=reconnect):
            await modal.on_submit(cast(Any, FakeInteraction(self.parent)))
        self.assertEqual(self.fixture.socket.sent_json, [])
        self.assertEqual(cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, "Not delivered")
        self.assertTrue(self.fixture.thread.sent_messages[-1])
        self.fixture.bridge.sessions.register(self.fixture.session)
        rejected = await self.card("**Q15.** Proceed?")
        rejected.fields[0]._value = "Yes"
        await rejected.on_submit(cast(Any, FakeInteraction(self.parent)))
        await self.fixture.bridge.controls.handle_command_reject({**self.fixture.socket.sent_json[-1], "reason": "session busy"})
        self.assertEqual(
            cast(StoredQuestion, rejected.cards.record(rejected.session_id, rejected.question.key)).state, "Not delivered"
        )

    async def test_identical_new_answer_gets_a_new_card_and_long_answer_checks_first_chunk(self) -> None:
        text = "**Q14.** Proceed?\n" + "Context and alternatives. " * 200
        first = await self.card(text)
        second = await self.card(text)
        self.assertEqual(len(self.parent.sent_messages), 2)
        self.assertNotEqual(first.question.key, second.question.key)
        self.fixture.thread._history[len(self.fixture.thread._history) - len(second.question.sources)].components = []
        second.fields[0]._value = "Yes"
        await second.on_submit(cast(Any, FakeInteraction(self.parent)))
        self.assertEqual(self.fixture.socket.sent_json, [])

    async def test_failed_publication_is_visible_and_waiting_cards_close_with_the_session(self) -> None:
        self.parent.send_raises = True
        await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, "**Q15.** Proceed?")
        self.assertFalse(self.fixture.bridge.store.records[self.fixture.session.session_id].questions)
        self.assertTrue(self.fixture.thread.sent_kwargs[-1]["silent"])
        self.parent.send_raises = False
        modal = await self.card("**Q15.** Proceed?")
        await self.fixture.bridge.questions.close(self.fixture.session.session_id)
        self.assertEqual(cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, "Closed")
        self.assertTrue(
            next(
                item
                for item in cast(discord.ui.LayoutView, self.parent._history[-1].view).walk_children()
                if isinstance(item, QuestionAnswerButton)
            ).disabled
        )
