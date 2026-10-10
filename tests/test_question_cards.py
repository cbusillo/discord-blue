from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any, cast

import discord

from discord_blue.doodads.agent_session.bridge import MAX_INPUT_QUESTIONS, RequestUserInputAnswerModal, RequestUserInputSelect
from discord_blue.doodads.agent_session.protocol import (
    RemoteRequestUserInput,
    RequestUserInputQuestion,
    RequestUserInputQuestionOption,
)
from tests.fakes_agent_prompts import prompt_fixture
from tests.fakes_agent_session import FakeInteraction, visible_view_text


class QuestionCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = self.enterContext(prompt_fixture())

    @staticmethod
    def question(key: str = "mode") -> RequestUserInputQuestion:
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

    async def post(self, questions: list[RequestUserInputQuestion]) -> discord.ui.LayoutView:
        session = self.fixture.session
        await self.fixture.bridge.handle_request_user_input(
            RemoteRequestUserInput(
                session_id=session.session_id,
                session_epoch=session.session_epoch,
                call_id="input",
                turn_id="turn-1",
                questions=questions,
            )
        )
        return cast(discord.ui.LayoutView, self.fixture.thread.sent_views[0])

    async def test_picker_preserves_full_labels_and_does_not_submit_on_selection(self) -> None:
        prefix = "same long option " * 10
        question = self.question()
        question.options = [RequestUserInputQuestionOption(prefix + suffix, "") for suffix in ("one", "two")]
        view = await self.post([question])
        select = next(item for item in view.walk_children() if isinstance(item, RequestUserInputSelect))
        # Simulate the Discord selection update, then call the actual callback.
        select._refresh_state(cast(Any, FakeInteraction(self.fixture.thread)), cast(Any, {"values": [select.options[1].value]}))
        interaction = FakeInteraction(self.fixture.thread)
        await select.callback(cast(Any, interaction))
        self.assertEqual(self.fixture.socket.sent_json, [])
        self.assertEqual([option.default for option in select.options], [False, True])
        self.assertEqual(interaction.response.edits[0][0], "")
        await cast(Any, view).submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(self.fixture.socket.sent_json[0]["response"], {"answers": {question.id: {"answers": [prefix + "two"]}}})

    async def test_all_questions_have_controls_and_send_all_answers(self) -> None:
        questions = [self.question(str(index)) for index in range(MAX_INPUT_QUESTIONS)]
        view = await self.post(questions)
        selects = [item for item in view.walk_children() if isinstance(item, RequestUserInputSelect)]
        self.assertEqual({item.question.id for item in selects}, {question.id for question in questions})
        for item in selects:
            item._refresh_state(cast(Any, FakeInteraction(self.fixture.thread)), cast(Any, {"values": [item.options[0].value]}))
            await item.callback(cast(Any, FakeInteraction(self.fixture.thread)))
        await cast(Any, view).submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(
            set(cast(dict[str, Any], self.fixture.socket.sent_json[0]["response"])["answers"]), {q.id for q in questions}
        )
        self.assertLessEqual(view.total_children_count, 40)  # Discord API component budget.
        self.assertLessEqual(view.content_length(), 4000)  # Discord API text budget.

    async def test_free_text_modal_reaches_agent_and_secret_is_hidden(self) -> None:
        for secret in (False, True):
            with self.subTest(secret=secret):
                self.fixture.thread.sent_views.clear()
                self.fixture.thread.sent_messages.clear()
                self.fixture.socket.sent_json.clear()
                question = replace(self.question(), options=[], is_secret=secret)
                view = await self.post([question])
                modal = RequestUserInputAnswerModal(cast(Any, view), question)
                modal.answer._value = "Use the staging database"
                await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
                text = visible_view_text(view)
                self.assertEqual("Use the staging database" in text, not secret)
                await cast(Any, view).submit(cast(Any, FakeInteraction(self.fixture.thread)))
                self.assertEqual(
                    self.fixture.socket.sent_json[0]["response"],
                    {"answers": {question.id: {"answers": ["Use the staging database"]}}},
                )

    async def test_oversized_prompt_stays_native_instead_of_hiding_questions(self) -> None:
        await self.post([self.question(str(index)) for index in range(MAX_INPUT_QUESTIONS + 1)])
        self.assertEqual(self.fixture.session.pending_user_inputs, {})
        self.assertIn("native TUI", self.fixture.thread.sent_messages[-1])
