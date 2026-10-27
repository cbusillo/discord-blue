from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import discord

from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.cards import assistant_answer_cards, notice_card
from discord_blue.doodads.agent_session.interactions import RequestUserInputView
from discord_blue.doodads.agent_session.question_modal import AnswerTogetherButton, AnswerTogetherModal, questionnaire_fits_modal
from discord_blue.doodads.agent_session.reply_modal import ReplyAboutModal, answer_excerpt, open_reply_modal
from tests.fakes_agent_prompts import prompt_fixture
from tests.fakes_agent_session import FakeInteraction, FakeReplyMessage, FakeThread
from discord_blue.doodads.agent_session.protocol import (
    RequestUserInputQuestion,
    RequestUserInputQuestionOption,
    RemoteRequestUserInput,
)


class InteractiveModalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = self.enterContext(prompt_fixture())
        self.fixture.session.acknowledged = True
        self.fixture.bridge.bot.config.agent_session.enabled = True

    @staticmethod
    def question(key: str) -> RequestUserInputQuestion:
        return RequestUserInputQuestion(
            id=key,
            header="Build mode",
            question="Choose a mode",
            is_other=True,
            is_secret=False,
            options=[
                RequestUserInputQuestionOption("Fast", "Skip extra checks"),
                RequestUserInputQuestionOption("Safe", "Full checks"),
            ],
        )

    async def questionnaire(self) -> RequestUserInputView:
        await self.fixture.prompt()
        request = self.fixture.session.pending_user_inputs
        self.assertTrue(request)
        session = self.fixture.session
        await self.fixture.bridge.interactions.handle_request_user_input(
            RemoteRequestUserInput(
                session_id=session.session_id,
                session_epoch=session.session_epoch,
                call_id="combined",
                turn_id="turn-1",
                questions=[self.question("scope"), replace(self.question("priority"), is_other=False)],
            )
        )
        return next(v for v in reversed(self.fixture.thread.sent_views) if isinstance(v, RequestUserInputView))

    async def test_combined_submission_sends_choices_and_other_once(self) -> None:
        view = await self.questionnaire()
        button = next(item for item in view.walk_children() if isinstance(item, AnswerTogetherButton))
        interaction = FakeInteraction(self.fixture.thread)
        await button.callback(cast(Any, interaction))
        modal = cast(AnswerTogetherModal, interaction.response.modals[0])
        modal.radios["scope"]._value = "other"
        modal.texts["scope"]._value = "Only the phone screen"
        modal.radios["priority"]._value = "1"
        self.assertEqual(self.fixture.socket.sent_json, [])
        await asyncio.gather(*(modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread))) for _ in range(2)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)
        self.assertEqual(
            self.fixture.socket.sent_json[0]["response"],
            {"answers": {"scope": {"answers": ["Only the phone screen"]}, "priority": {"answers": ["Safe"]}}},
        )
        self.assertTrue(self.fixture.session.pending_user_inputs["combined"].submitted)

    async def test_combined_form_rejects_stale_context_and_operator(self) -> None:
        for invalid in ("operator", "epoch", "thread", "replaced", "submitted"):
            with self.subTest(invalid=invalid):
                view = await self.questionnaire()
                modal = AnswerTogetherModal(view)
                modal.radios["scope"]._value = "0"
                modal.radios["priority"]._value = "1"
                interaction = FakeInteraction(self.fixture.thread)
                if invalid == "epoch":
                    self.fixture.session.hello = replace(self.fixture.session.hello, session_epoch="next")
                elif invalid == "thread":
                    interaction.channel = FakeThread(556)
                elif invalid == "replaced":
                    await self.questionnaire()
                elif invalid == "submitted":
                    self.fixture.session.pending_user_inputs["combined"].submitted = True
                with patch.object(AgentSessionBridge, "is_operator", return_value=invalid != "operator"):
                    await modal.on_submit(cast(Any, interaction))
                self.assertEqual(self.fixture.socket.sent_json, [])
                self.assertTrue(interaction.response.messages)

    async def test_combined_form_bounds_fall_back_without_truncating(self) -> None:
        view = await self.questionnaire()
        request = view.request
        cases = [
            replace(request, questions=request.questions * 3),
            replace(request, questions=[replace(request.questions[0], is_secret=True)]),
            replace(request, questions=[replace(request.questions[0], question="q" * 101)]),
            replace(
                request,
                questions=[replace(request.questions[0], options=[replace(request.questions[0].options[0], description="d" * 101)])],
            ),
            replace(request, questions=[replace(request.questions[0], options=request.questions[0].options * 5)]),
        ]
        for oversized in cases:
            self.assertFalse(questionnaire_fits_modal(oversized))
            fallback = RequestUserInputView(view.interactions, view.session_id, oversized)
            self.assertFalse(any(isinstance(i, AnswerTogetherButton) for i in fallback.walk_children()))
        modal = AnswerTogetherModal(view)
        self.assertLessEqual(len(modal.to_dict()["components"]), 5)
        self.assertEqual(modal.radios["priority"].options[1].description, request.questions[1].options[1].description)

    async def test_missing_other_or_conflicting_text_does_not_submit_or_cancel(self) -> None:
        view = await self.questionnaire()
        for choice, text in (("other", ""), ("0", "Unchosen text")):
            modal = AnswerTogetherModal(view)
            modal.radios["scope"]._value = choice
            modal.texts["scope"]._value = text
            modal.radios["priority"]._value = "0"
            await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertFalse(self.fixture.session.pending_user_inputs["combined"].submitted)
        self.assertEqual(self.fixture.socket.sent_json, [])

    def source(self, content: str = "The second option uses larger text.") -> FakeReplyMessage:
        message = FakeReplyMessage(90, self.fixture.thread, author_id=999)
        view = assistant_answer_cards(content)[0][0]
        message.components = cast(Any, [discord.components._component_factory(cast(Any, c)) for c in view.to_components()])
        message.jump_url = "https://discord.com/channels/42/555/90"
        self.fixture.thread.add_message(message)
        return message

    async def reply_form(self, message: FakeReplyMessage) -> ReplyAboutModal:
        interaction = FakeInteraction(self.fixture.thread)
        await open_reply_modal(self.fixture.bridge, cast(Any, interaction), cast(Any, message))
        return cast(ReplyAboutModal, interaction.response.modals[0])

    async def test_context_reply_extracts_components_and_uses_ordinary_delivery_once(self) -> None:
        source = self.source()
        modal = await self.reply_form(source)
        modal.reply._value = "Use this, with larger text."
        await asyncio.gather(*(modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread))) for _ in range(2)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)
        command = self.fixture.socket.sent_json[0]
        self.assertEqual(command["kind"], "reply")
        self.assertEqual(command["issued_by"], "123")
        self.assertIn("The second option uses larger text.", str(command["text"]))
        self.assertIn(source.jump_url, str(command["text"]))
        self.assertIn(modal.reply.value, str(command["text"]))
        self.assertFalse(any(c in str(command["text"]) for c in ("\u2060", "\u200b")))

    async def test_context_reply_accepts_claude_reply_capability_and_labels_older_context(self) -> None:
        self.fixture.session.hello = replace(self.fixture.session.hello, harness="claude", capabilities=frozenset({"reply"}))
        source = self.source()
        source.created_at = self.fixture.session.attached_at - timedelta(days=1)
        modal = await self.reply_form(source)
        modal.reply._value = "Please explain."
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertIn("earlier context", str(self.fixture.socket.sent_json[0]["text"]))

    async def test_context_reply_revalidates_deletion_reconnect_operator_and_changed_answer(self) -> None:
        for invalid in ("deleted", "epoch", "operator", "answer", "thread"):
            with self.subTest(invalid=invalid):
                source = self.source()
                modal = await self.reply_form(source)
                modal.reply._value = "Use this."
                interaction = FakeInteraction(self.fixture.thread)
                if invalid == "deleted":
                    self.fixture.thread.delete_message(source.id)
                elif invalid == "epoch":
                    self.fixture.session.hello = replace(self.fixture.session.hello, session_epoch="new")
                elif invalid == "answer":
                    source.components = []
                elif invalid == "thread":
                    interaction.channel = FakeThread(556)
                with patch.object(AgentSessionBridge, "is_operator", return_value=invalid != "operator"):
                    await modal.on_submit(cast(Any, interaction))
                self.assertEqual(self.fixture.socket.sent_json, [])
                self.assertTrue(interaction.response.messages)

    async def test_context_menu_rejects_foreign_messages_and_notices(self) -> None:
        source = self.source()
        source.author = SimpleNamespace(id=123)
        self.assertFalse(answer_excerpt(cast(Any, source), 999))
        source.author = SimpleNamespace(id=999)
        source.components = cast(
            Any, [discord.components._component_factory(cast(Any, c)) for c in notice_card("Notice").to_components()]
        )
        interaction = FakeInteraction(self.fixture.thread)
        await open_reply_modal(self.fixture.bridge, cast(Any, interaction), cast(Any, source))
        self.assertEqual(interaction.response.modals, [])
        self.assertTrue(interaction.response.messages)
