from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import discord
from discord_blue.doodads.agent_session.messages import edit_agent_session_message

from discord_blue.doodads.agent_session.interactions import MAX_INPUT_QUESTIONS, RequestUserInputAnswerModal, RequestUserInputSelect
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
        await self.fixture.bridge.interactions.handle_request_user_input(
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
        self.assertIn(prefix + "one", visible_view_text(view))
        self.assertIn(prefix + "two", visible_view_text(view))
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
        self.assertEqual(self.fixture.session.display_state, "waiting")
        self.assertTrue(any("native TUI" in message for message in self.fixture.thread.sent_messages))

    async def test_full_question_and_long_option_description_remain_visible(self) -> None:
        question = self.question()
        question.question = "Read this complete context. " * 20 + "Then choose a mode."
        question.options[0].description = "Important choice context. " * 6
        view = await self.post([question])
        self.assertIn(question.question, visible_view_text(view))
        self.assertIn(question.options[0].description, visible_view_text(view))

    async def test_full_text_over_budget_stays_native(self) -> None:
        question = self.question()
        question.question = "Long context " * 400
        await self.post([question])
        self.assertEqual(self.fixture.session.pending_user_inputs, {})
        self.assertEqual(self.fixture.session.display_state, "waiting")

    async def test_secret_options_are_visible_but_answer_is_hidden(self) -> None:
        question = replace(self.question(), is_secret=True)
        view = await self.post([question])
        self.assertIn("Fast", visible_view_text(view))
        self.assertIn("Safe", visible_view_text(view))
        select = next(item for item in view.walk_children() if isinstance(item, RequestUserInputSelect))
        select._refresh_state(cast(Any, FakeInteraction(self.fixture.thread)), cast(Any, {"values": [select.options[1].value]}))
        await select.callback(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertIn("[hidden]", visible_view_text(view))
        self.assertFalse(any(option.default for option in select.options))
        await cast(Any, view).submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(self.fixture.socket.sent_json[0]["response"], {"answers": {question.id: {"answers": ["Safe"]}}})
        cast(Any, view).set_answer(question.id, "Private custom answer")
        self.assertNotIn("Private custom answer", visible_view_text(view))

    async def test_multi_select_sends_every_chosen_option(self) -> None:
        question = replace(self.question(), is_other=False, multi_select=True)
        question.options.append(RequestUserInputQuestionOption("Lint", "Style checks"))
        view = await self.post([question])
        select = next(item for item in view.walk_children() if isinstance(item, RequestUserInputSelect))
        self.assertEqual(select.max_values, 3)
        self.assertFalse(any(getattr(item, "label", None) == "Answer together" for item in view.walk_children()))
        chosen = {"values": [select.options[0].value, select.options[2].value]}
        select._refresh_state(cast(Any, FakeInteraction(self.fixture.thread)), cast(Any, chosen))
        await select.callback(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual([option.default for option in select.options], [True, False, True])
        await cast(Any, view).submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(self.fixture.socket.sent_json[0]["response"], {"answers": {question.id: {"answers": ["Fast", "Lint"]}}})

    async def test_question_notification_is_not_suppressed_in_discord_payload(self) -> None:
        from discord.http import handle_message_parameters

        view = await self.post([self.question()])
        kwargs = self.fixture.thread.sent_kwargs[0]
        flags = discord.MessageFlags(suppress_notifications=cast(bool, kwargs["silent"]))
        with handle_message_parameters(view=view, flags=flags) as parameters:
            assert parameters.payload is not None
            flags = discord.MessageFlags._from_value(parameters.payload["flags"])
            self.assertFalse(flags.suppress_notifications)
            self.assertTrue(flags.components_v2)

    async def test_duplicate_labels_mark_only_one_option_selected(self) -> None:
        question = self.question()
        question.options[1].label = question.options[0].label
        view = await self.post([question])
        cast(Any, view).set_answer(question.id, question.options[0].label)
        select = next(item for item in view.walk_children() if isinstance(item, RequestUserInputSelect))
        self.assertEqual(sum(option.default for option in select.options), 1)

    async def test_v2_flag_keeps_a_message_without_components_valid_when_retired(self) -> None:
        from tests.fakes_agent_session import FakeReplyMessage

        message = FakeReplyMessage(99, self.fixture.thread)
        flagged = cast(Any, message)
        flagged.flags = SimpleNamespace(components_v2=True)
        del flagged.components
        await edit_agent_session_message(flagged, content="Resolved")
        self.assertEqual(message.content, "")
        self.assertEqual(visible_view_text(message.view), "Resolved")
