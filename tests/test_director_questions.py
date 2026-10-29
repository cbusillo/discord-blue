from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import discord

from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.cleanup import SessionCleanups
from discord_blue.doodads.agent_session.cards import STATUS_LABELS
from discord_blue.doodads.agent_session.director_questions import (
    ANSWERED_STATES,
    QuestionAnswerButton,
    QuestionAnswerModal,
    QuestionCards,
    QuestionChoiceButton,
    detect_questions,
    question_choices,
    question_excerpt,
)
from discord_blue.doodads.agent_session.sessions import AgentSession, PendingSessionCleanup
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

    def test_offered_answers_become_choices_only_when_short(self) -> None:
        bullets = "**Q1.** Skip notes?\n- **My recommendation:** no.\n- **On yes:** I brief it.\n- **On no:** I record it."
        self.assertEqual(question_choices(bullets), (("yes", False), ("no", True)))
        lines = "**Q2.** Ship?\nRecommendation: ship it. Cost: none.\nAlternatives: wait a day; drop it"
        self.assertEqual(question_choices(lines), (("ship it", True), ("wait a day", False), ("drop it", False)))
        prose = "**Q3.** Which?\nRecommendation: " + "a long explanation that is a paragraph, not a button " * 2
        self.assertEqual(question_choices(prose), ())
        example = "**Q4.** Adopt this template?\n```\n- **On yes:** x\n```\n    - **On no:** y\n" + (
            "Recommendation: adopt it.\nAlternative: keep it"
        )
        self.assertEqual(question_choices(example), (("adopt it", True), ("keep it", False)))
        self.assertEqual(question_choices("**Q5.** Describe it?\nRecommendation: free text answer."), ())
        inline = "**Q6.** Which color? I recommend blue because it matches the logo.\n\nAlternatives: green; red"
        self.assertEqual(question_choices(inline), (("blue", True), ("green", False), ("red", False)))

    def test_a_question_with_its_recommendation_on_the_same_line_is_a_card(self) -> None:
        text = "**Q1044.** Should CT125 also take full test runs? I recommend yes, set up as its own issue."
        self.assertEqual([number for number, _ in detect_questions(text)], ["Q1044"])
        self.assertEqual(question_excerpt(text), "Should CT125 also take full test runs?")
        self.assertEqual(detect_questions("**Q12.** Release v1.2 shipped. Anything else"), ())
        abbreviated = "**Q13.** Should we support e.g. CSV imports?"
        self.assertEqual([number for number, _ in detect_questions(abbreviated)], ["Q13"])
        self.assertEqual(question_excerpt(abbreviated), "Should we support e.g. CSV imports?")

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
        view = next(
            view
            for view in reversed(self.fixture.thread.sent_views)
            if isinstance(view, discord.ui.LayoutView)
            and any(isinstance(item, QuestionAnswerButton) for item in view.walk_children())
        )
        button = next(item for item in view.walk_children() if isinstance(item, QuestionAnswerButton))
        interaction = FakeInteraction(self.fixture.thread)
        await button.callback(cast(Any, interaction))
        return cast(QuestionAnswerModal, interaction.response.modals[0])

    async def test_questions_replace_plain_text_in_order_without_parent_posts_or_notifications(self) -> None:
        text = (
            "Opening prose.\n\n**Q12.** Ship it?\n\nRecommendation: yes.\nCost: upkeep.\nAlternative: wait."
            "\n\n## Closing prose.\n\n**Q13.** Keep both?"
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

    async def test_a_matching_code_example_stays_prose_and_only_the_live_question_becomes_a_card(self) -> None:
        for number, newline in ((40, "\n"), (41, "\r\n")):
            header = f"**Q{number}.** Ship it?"
            source = newline.join(("```text", header, "```", "", header))
            start = len(self.fixture.thread.sent_views)
            await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, source)
            views = self.fixture.thread.sent_views[start:]
            self.assertEqual(len(views), 2)
            self.assertIn(f"```text\n{header}\n```", visible_view_text(views[0]).replace("\r\n", "\n"))
            self.assertIn(header, visible_view_text(views[1]))
            self.assertEqual(
                len(
                    [
                        item
                        for item in cast(discord.ui.LayoutView, views[1]).walk_children()
                        if isinstance(item, QuestionAnswerButton)
                    ]
                ),
                1,
            )
            self.assertNotIn(header, visible_view_text(views[0]).split("```")[-1])

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
        shown = visible_view_text(original.view)
        self.assertIn("Ship it?", shown)  # The question stays readable beside its answer.
        self.assertIn("Ship it", shown.split("Ship it?", 1)[1])
        self.assertNotIn("\n", shown)
        posted = [visible_view_text(m.view) if m.view else m.content for m in self.fixture.thread._history]
        self.assertEqual([text for text in posted if "Ship it" in text], [shown])  # No copy of the answer is posted.
        command_id = str(self.fixture.socket.sent_json[0]["command_id"])
        anchor = self.fixture.session.pending_commands[command_id].message_id
        self.assertEqual(anchor, original.id)  # Delivery reactions show on the card itself.
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

    async def test_reasking_updates_the_existing_card_and_invalidates_its_old_form(self) -> None:
        original = await self.card()
        original.fields[0]._value = "Yes"
        count = len(self.fixture.thread.sent_views)
        updated = await self.card(
            "Context update.\n\n**Q12.** Ship the revision?\n\nI recommend waiting.\nAn experienced developer would test first."
        )
        self.assertEqual(len(self.fixture.thread.sent_views), count + 2)
        message = self.fixture.thread._messages[cast(int, original.question.message_id)]
        self.assertIn("I recommend waiting.", visible_view_text(message.view))
        self.assertIn("An experienced developer would test first.", visible_view_text(message.view))
        self.assertTrue(
            await self.fixture.bridge.session_threads.thread_has_assistant_message(cast(discord.Thread, self.fixture.thread))
        )
        await original.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(self.fixture.socket.sent_json, [])
        updated.fields[0]._value = "Wait"
        await updated.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)

    async def test_a_reasked_unanswered_question_reuses_its_card_after_an_epoch_change(self) -> None:
        original = await self.card()
        original.fields[0]._value = "Yes"
        await original.cards.close(original.session_id)
        self.fixture.session.hello = replace(self.fixture.session.hello, session_epoch="next-conversation")
        count = len(self.fixture.thread.sent_views)
        reopened = await self.card()
        self.assertEqual(len(self.fixture.thread.sent_views), count + 1)
        self.assertEqual(reopened.question.key, original.question.key)
        await original.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(self.fixture.socket.sent_json, [])
        reopened.fields[0]._value = "Yes"
        await reopened.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)

    async def test_clarification_is_not_an_answer_but_lowercase_decisions_work(self) -> None:
        modal = await self.card()
        for text in ("Q12 what does the cost mean?", "Q12 and Q13 look fine"):
            await modal.cards.decisions(modal.session_id, text, url="https://discord.com/channels/1/2/3")
            self.assertEqual(
                cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, QuestionState.WAITING
            )
        await modal.cards.decisions(modal.session_id, "q12 yes", url="https://discord.com/channels/1/2/3")
        self.assertEqual(
            cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, QuestionState.ANSWERED
        )

    async def test_failed_publication_keeps_question_text_and_completion_in_the_thread(self) -> None:
        original = self.fixture.thread.send
        attempts = 0

        async def send(content: str | None = None, **kwargs: object) -> FakeReplyMessage:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("connection lost")
            return await original(content, **kwargs)

        with patch.object(FakeThread, "send", side_effect=send):
            await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, "**Q50.** Ship it?")
        self.assertIn("**Q50.** Ship it?", "\n".join(self.fixture.thread.sent_messages))
        self.assertTrue(
            await self.fixture.bridge.session_threads.thread_has_assistant_message(cast(discord.Thread, self.fixture.thread))
        )
        self.assertFalse(self.parent.sent_messages)

    async def test_cleanup_edits_unanswered_cards_before_archival(self) -> None:
        modal = await self.card()
        self.fixture.bridge.sessions.remove(modal.session_id)
        cleanup = PendingSessionCleanup(modal.session_id, modal.question.epoch, self.fixture.thread.id, None, {"archive"})

        async def archive(*_args: object, **_kwargs: object) -> None:
            message = self.fixture.thread._messages[cast(int, modal.question.message_id)]
            self.assertFalse(
                any(isinstance(item, discord.ui.Button) for item in cast(discord.ui.LayoutView, message.view).walk_children())
            )
            self.fixture.thread.archived = True

        with (
            patch.object(SessionCleanups, "get_thread_for_cleanup", new=AsyncMock(return_value=(self.fixture.thread, True))),
            patch.object(SessionCleanups, "close_thread", side_effect=archive),
        ):
            await self.fixture.bridge.cleanups.cleanup_session_artifacts(cleanup)
        self.assertTrue(self.fixture.thread.archived)

    async def test_legacy_parent_cards_retire_to_a_thread_link(self) -> None:
        modal = await self.card()
        earlier = await self.parent.send("old card")
        value = {**asdict(modal.question), "parent_id": self.parent.id, "message_id": earlier.id}
        legacy = StoredQuestion.parse(value)
        modal.cards.save(modal.session_id, legacy)
        button = QuestionAnswerButton(modal.cards, modal.session_id, legacy)
        await button.callback(cast(Any, FakeInteraction(self.parent)))
        view = cast(discord.ui.LayoutView, earlier.view)
        self.assertIn(legacy.source_url, visible_view_text(view))
        self.assertFalse(any(isinstance(item, discord.ui.Button) for item in view.walk_children()))
        self.assertEqual(self.fixture.socket.sent_json, [])

    async def test_a_late_session_end_edit_cannot_overwrite_the_answered_card(self) -> None:
        modal = await self.card()
        message = self.fixture.thread._messages[cast(int, modal.question.message_id)]
        original = message.edit
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def edit(content: str | None = None, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            await original(content, **kwargs)

        with patch.object(FakeReplyMessage, "edit", side_effect=edit):
            ending = asyncio.create_task(modal.cards.close(modal.session_id))
            await asyncio.wait_for(entered.wait(), 1)
            answering = asyncio.create_task(
                modal.cards.decisions(modal.session_id, "Q12 yes", url="https://discord.com/channels/1/2/3")
            )
            release.set()
            await asyncio.gather(ending, answering)
        self.assertEqual(
            cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key)).state, QuestionState.ANSWERED
        )
        self.assertIn("yes", visible_view_text(message.view))
        self.assertNotIn("Recommendation", visible_view_text(message.view))

    async def test_reask_serializes_a_concurrent_decision_and_reopens_an_answered_revision(self) -> None:
        original = await self.card()
        fetch = self.fixture.thread.fetch_message
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_fetch(message_id: int) -> FakeReplyMessage:
            entered.set()
            await release.wait()
            return await fetch(message_id)

        with patch.object(FakeThread, "fetch_message", side_effect=held_fetch):
            reask = asyncio.create_task(
                self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, "**Q12.** Ship the revision?")
            )
            await asyncio.wait_for(entered.wait(), 1)
            answer = asyncio.create_task(
                original.cards.decisions(original.session_id, "Q12 yes", url="https://discord.com/channels/1/2/3")
            )
            release.set()
            await asyncio.gather(reask, answer)
        self.assertEqual(
            cast(StoredQuestion, original.cards.record(original.session_id, original.question.key)).state, QuestionState.ANSWERED
        )
        fresh = await self.card()
        self.assertEqual(fresh.question.state, QuestionState.WAITING)
        self.assertGreater(fresh.question.revision, original.question.revision)
        original.fields[0]._value = "Yes"
        await original.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(self.fixture.socket.sent_json, [])
        self.assertIn(fresh.question.source_url, visible_view_text(self.fixture.thread.sent_views[-1]))

    async def test_deleted_cards_recreate_and_transient_reads_do_not_drop_later_prose(self) -> None:
        modal = await self.card()
        self.fixture.thread._messages.pop(cast(int, modal.question.message_id))
        recreated = await self.card("**Q12.** Ship the revision?")
        self.assertNotEqual(recreated.question.message_id, modal.question.message_id)
        with patch.object(FakeThread, "fetch_message", new=AsyncMock(side_effect=TimeoutError)):
            await self.fixture.bridge.status.post_assistant_message(
                self.fixture.thread.id, "**Q12.** Ship again?\n\n## Later\nTail survives."
            )
        self.assertIn("Tail survives.", "\n".join(self.fixture.thread.sent_messages))

    async def test_reask_growing_past_attachment_limit_keeps_all_updated_context(self) -> None:
        modal = await self.card()
        original_edit = self.fixture.thread._messages[cast(int, modal.question.message_id)].edit

        async def denied_file(content: str | None = None, **kwargs: object) -> None:
            if any(isinstance(item, discord.File) for item in cast(list[object], kwargs.get("attachments", []))):
                raise discord.Forbidden(
                    response=type("Response", (), {"status": 403, "reason": "Forbidden"})(), message="No attachments"
                )
            await original_edit(content, **kwargs)

        with patch.object(FakeReplyMessage, "edit", side_effect=denied_file):
            grown = await self.card(
                "**Q12.** Ship it?\n\nI recommend testing.\n" + "New cost detail. " * 400 + "\nAlternative: keep waiting."
            )
        stored = cast(StoredQuestion, grown.cards.record(grown.session_id, grown.question.key))
        self.assertFalse(stored.has_attachment)
        self.assertGreater(len(stored.sources), 1)
        self.assertIn("Alternative: keep waiting.", "\n".join(self.fixture.thread.sent_messages))
        self.assertTrue(
            await self.fixture.bridge.session_threads.thread_has_assistant_message(cast(discord.Thread, self.fixture.thread))
        )

    async def test_repeated_question_within_one_answer_has_one_body_and_a_complete_reference(self) -> None:
        await self.fixture.bridge.status.post_assistant_message(
            self.fixture.thread.id,
            "**Q42.** Ship it?\nRecommendation: test first.\nCost: upkeep.\nAlternative: wait."
            "\n\n## More\nContext.\n\n**Q42.** Ship it?",
        )
        questions = self.fixture.bridge.store.records[self.fixture.session.session_id].questions
        self.assertEqual(len(questions), 1)
        for context in ("Recommendation: test first.", "Cost: upkeep.", "Alternative: wait."):
            self.assertIn(context, questions[0].questions[0][1])
        self.assertIn(questions[0].source_url, visible_view_text(self.fixture.thread.sent_views[-1]))
        self.assertTrue(
            await self.fixture.bridge.session_threads.thread_has_assistant_message(cast(discord.Thread, self.fixture.thread))
        )

    async def test_late_native_ack_cannot_change_a_reasked_card(self) -> None:
        modal = await self.card()
        modal.fields[0]._value = "Yes"
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        command = self.fixture.socket.sent_json[-1]
        reasked = await self.card()
        await self.fixture.bridge.controls.handle_command_ack(command)
        self.assertEqual(
            cast(StoredQuestion, reasked.cards.record(reasked.session_id, reasked.question.key)).state, QuestionState.WAITING
        )

    async def test_failed_reask_preserves_the_prior_card_state_and_invalidates_older_forms(self) -> None:
        modal = await self.card()
        old = modal.question
        with patch.object(FakeThread, "fetch_message", new=AsyncMock(side_effect=TimeoutError)):
            await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, "**Q12.** Ship the revision?")
        current = cast(StoredQuestion, modal.cards.record(modal.session_id, old.key))
        self.assertEqual(current.state, old.state)
        self.assertEqual(current.questions, old.questions)
        self.assertGreater(current.revision, old.revision)
        final_question = "**Q12.** Ship the final revision?"
        with patch.object(QuestionCards, "reference", new=AsyncMock(side_effect=OSError("link send failed"))):
            await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, final_question)
        published = cast(StoredQuestion, modal.cards.record(modal.session_id, old.key))
        self.assertEqual(published.state, QuestionState.WAITING)
        self.assertEqual(published.questions[0][1], final_question)
        modal.fields[0]._value = "Yes"
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        self.assertEqual(self.fixture.socket.sent_json, [])

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

    async def test_one_tap_choice_answers_the_card_and_a_changed_question_refuses_it(self) -> None:
        modal = await self.card()
        view = cast(discord.ui.LayoutView, self.fixture.thread._history[-1].view)
        choices = [item for item in view.walk_children() if isinstance(item, QuestionChoiceButton)]
        self.assertEqual([button.choice for button in choices], ["yes", "wait"])
        self.assertIn("recommended", str(choices[0].label))
        stale = choices[1]
        await self.card("**Q12.** Ship it now?\nRecommendation: yes.\nAlternative: no")  # Re-asked: the old buttons are stale.
        refused = FakeInteraction(self.fixture.thread, message=self.fixture.thread._history[0])
        await stale.callback(cast(Any, refused))
        self.assertEqual((self.fixture.socket.sent_json, refused.response.edits), ([], []))
        self.assertTrue(refused.response.messages)
        current = cast(discord.ui.LayoutView, self.fixture.thread._history[0].view)
        tap = next(item for item in current.walk_children() if isinstance(item, QuestionChoiceButton))
        await tap.callback(cast(Any, FakeInteraction(self.fixture.thread, message=self.fixture.thread._history[0])))
        self.assertEqual(self.fixture.socket.sent_json[-1]["text"], "Q12: yes")
        await self.fixture.bridge.controls.handle_command_ack(self.fixture.socket.sent_json[-1])
        stored = cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key))
        self.assertEqual((stored.state, stored.answer), (QuestionState.ANSWERED, "yes"))

    async def test_a_new_cards_choice_answers_it(self) -> None:
        modal = await self.card()
        message = self.fixture.thread._history[-1]
        view = cast(discord.ui.LayoutView, message.view)
        tap = next(item for item in view.walk_children() if isinstance(item, QuestionChoiceButton))
        interaction = FakeInteraction(self.fixture.thread, message=message)
        await tap.callback(cast(Any, interaction))
        self.assertEqual((self.fixture.socket.sent_json[-1]["text"], interaction.response.messages), ("Q12: yes", []))
        await self.fixture.bridge.controls.handle_command_ack(self.fixture.socket.sent_json[-1])
        stored = cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key))
        self.assertEqual(stored.state, QuestionState.ANSWERED)

    async def restarted_cards(self) -> QuestionCards:
        """Question cards as a restarted bot sees them, loaded from the saved store."""
        await self.fixture.bridge.store.flush()
        loaded = SessionStore(self.fixture.bridge.store.path)
        await loaded.start()
        self.addAsyncCleanup(loaded.close)
        self.fixture.bridge.store = loaded
        recovered = QuestionCards(self.fixture.bridge)
        recovered.restore()
        return recovered

    GROUP = "**Q1.** Which color?\nRecommendation: blue.\nAlternatives: green\n\n**Q2.** Which shape?\nRecommendation: square."

    def grouped(self, number: str) -> tuple[StoredQuestion, FakeReplyMessage]:
        cards: QuestionCards = self.fixture.bridge.questions
        question = cast(StoredQuestion, cards.record(self.fixture.session.session_id, cards.key(self.fixture.session, number)))
        return question, self.fixture.thread._messages[cast(int, question.message_id)]

    @staticmethod
    def accent(message: FakeReplyMessage) -> int | None:
        container = cast(discord.ui.LayoutView, message.view).children[0]
        assert isinstance(container, discord.ui.Container)
        return container.accent_color.value if isinstance(container.accent_color, discord.Colour) else container.accent_color

    async def hold_first_answer(self) -> None:
        await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, self.GROUP)
        _, message = self.grouped("Q1")
        self.assertEqual(self.accent(message), STATUS_LABELS["waiting"][1])
        tap = next(
            item for item in cast(discord.ui.LayoutView, message.view).walk_children() if isinstance(item, QuestionChoiceButton)
        )
        await tap.callback(cast(Any, FakeInteraction(self.fixture.thread, message=message)))

    async def answer_second(self, cards: QuestionCards, text: str) -> None:
        question, _ = self.grouped("Q2")
        modal = QuestionAnswerModal(cards, self.fixture.session.session_id, question, 123)
        modal.fields[0]._value = text
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))

    async def test_a_tap_changes_its_card_as_the_interaction_response(self) -> None:
        await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, self.GROUP)
        _, message = self.grouped("Q1")
        view = cast(discord.ui.LayoutView, message.view)
        tap = next(item for item in view.walk_children() if isinstance(item, QuestionChoiceButton))
        self.assertNotEqual(tap.style, discord.ButtonStyle.success)  # Green means only "sent".
        interaction = FakeInteraction(self.fixture.thread, message=message)
        await tap.callback(cast(Any, interaction))
        self.assertFalse(interaction.response.deferred)
        self.assertIn("sends when Q2 is answered", visible_view_text(interaction.response.edit_kwargs[0]["view"]))

    async def test_a_card_shown_early_is_redrawn_when_its_refusal_notice_fails(self) -> None:
        await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, self.GROUP)
        _, message = self.grouped("Q1")
        tap = next(
            item for item in cast(discord.ui.LayoutView, message.view).walk_children() if isinstance(item, QuestionChoiceButton)
        )
        interaction = FakeInteraction(self.fixture.thread, message=message)

        async def lost(*_args: object, **_kwargs: object) -> None:
            raise OSError("notice lost")

        interaction.followup = SimpleNamespace(send=lost)
        edits = len(message.edits)
        cards: QuestionCards = self.fixture.bridge.questions
        with patch.object(cards, "persist", AsyncMock(return_value=False)), self.assertRaises(OSError):
            await tap.callback(cast(Any, interaction))
        question, _ = self.grouped("Q1")
        self.assertEqual((question.state, interaction.response.deferred), (QuestionState.WAITING, False))
        self.assertGreater(len(message.edits), edits)  # The early answer view was replaced by the real card.

    async def test_answers_from_one_reply_go_to_the_agent_together(self) -> None:
        await self.hold_first_answer()
        held, message = self.grouped("Q1")
        self.assertEqual((self.fixture.socket.sent_json, held.state), ([], QuestionState.HELD))
        self.assertIn("sends when Q2 is answered", visible_view_text(message.view))
        self.assertEqual(self.accent(message), ANSWERED_STATES[QuestionState.HELD])
        await self.answer_second(self.fixture.bridge.questions, "round")
        self.assertEqual([command["text"] for command in self.fixture.socket.sent_json], ["Q1: blue\n\nQ2: round"])
        await self.fixture.bridge.controls.handle_command_ack(self.fixture.socket.sent_json[-1])
        for number in ("Q1", "Q2"):
            question, message = self.grouped(number)
            self.assertEqual((question.state, self.accent(message)), (QuestionState.ANSWERED, STATUS_LABELS["done"][1]))
            self.assertIn("✓ sent", visible_view_text(message.view))
            self.assertEqual(message.reactions, [])

    async def test_a_group_turns_sent_in_one_burst(self) -> None:
        await self.hold_first_answer()
        await self.answer_second(self.fixture.bridge.questions, "round")
        events: list[str] = []
        for number in ("Q1", "Q2"):
            _, message = self.grouped(number)
            original = message.edit

            async def edit(
                content: str | None = None,
                _original: Callable[..., Awaitable[None]] = original,
                _number: str = number,
                **kwargs: object,
            ) -> None:
                events.append(f"start {_number}")
                await asyncio.sleep(0.01)
                await _original(content, **kwargs)
                events.append(f"end {_number}")

            message.edit = edit  # type: ignore[method-assign,assignment]
        await self.fixture.bridge.controls.handle_command_ack(self.fixture.socket.sent_json[-1])
        # Both cards start changing before either finishes, so neither shows sent while the other says sending.
        self.assertEqual(sorted(events[:2]), ["start Q1", "start Q2"])

    async def test_a_rejected_answer_turns_its_card_red_without_reactions(self) -> None:
        modal = await self.card()
        modal.fields[0]._value = "Yes"
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        command = self.fixture.socket.sent_json[-1]
        await self.fixture.bridge.controls.handle_command_reject({**command, "reason": "closed"})
        stored = cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key))
        message = self.fixture.thread._messages[cast(int, stored.message_id)]
        self.assertEqual((stored.state, self.accent(message)), (QuestionState.NOT_DELIVERED, STATUS_LABELS["failed"][1]))
        self.assertIn("not sent", visible_view_text(message.view))
        self.assertEqual(message.reactions, [])

    async def test_an_ended_conversation_shows_held_answers_as_not_sent(self) -> None:
        await self.hold_first_answer()
        await self.fixture.bridge.questions.close(self.fixture.session.session_id, current_epoch="a-later-conversation")
        held, message = self.grouped("Q1")
        self.assertEqual((held.state, self.fixture.socket.sent_json), (QuestionState.NOT_DELIVERED, []))
        self.assertIn("not sent", visible_view_text(message.view))

    async def test_a_restart_keeps_held_answers_for_their_group(self) -> None:
        await self.hold_first_answer()
        recovered = await self.restarted_cards()
        self.fixture.bridge.questions = recovered
        await self.answer_second(recovered, "round")
        self.assertEqual([command["text"] for command in self.fixture.socket.sent_json], ["Q1: blue\n\nQ2: round"])

    async def test_an_answer_waits_for_a_card_still_being_posted(self) -> None:
        cards: QuestionCards = self.fixture.bridge.questions
        original = cards.post

        async def only_first(
            session: AgentSession, thread: discord.Thread, text: str, *, complete: bool, replay: bool, group: str
        ) -> None:
            if "**Q1.**" in text:  # Q2's card has not been posted yet.
                await original(session, thread, text, complete=complete, replay=replay, group=group)

        with patch.object(cards, "post", only_first):
            await self.fixture.bridge.status.post_assistant_message(self.fixture.thread.id, self.GROUP)
        _, message = self.grouped("Q1")
        tap = next(
            item for item in cast(discord.ui.LayoutView, message.view).walk_children() if isinstance(item, QuestionChoiceButton)
        )
        await tap.callback(cast(Any, FakeInteraction(self.fixture.thread, message=message)))
        self.assertEqual((self.grouped("Q1")[0].state, self.fixture.socket.sent_json), (QuestionState.HELD, []))
        self.assertIn("sends when Q2 is answered", visible_view_text(message.view))
        await self.fixture.bridge.controls.send_thread_reply(cast(Any, FakeReplyMessage(302, self.fixture.thread, "Q2 round")))
        self.assertEqual([command["text"] for command in self.fixture.socket.sent_json], ["Q1: blue\n\nQ2 round"])

    async def test_a_restart_while_sending_says_delivery_is_not_confirmed(self) -> None:
        modal = await self.card()
        modal.fields[0]._value = "Yes"
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        await self.restarted_cards()
        recovered = await self.restarted_cards()  # Restarted again before the session attached.
        stored = cast(StoredQuestion, recovered.record(modal.session_id, modal.question.key))
        self.assertEqual(stored.state, QuestionState.UNCONFIRMED)
        await recovered.close(modal.session_id, current_epoch=self.fixture.session.session_epoch)  # The session attaches.
        message = self.fixture.thread._messages[cast(int, stored.message_id)]
        self.assertIn("delivery not confirmed", visible_view_text(message.view))

    async def test_held_answers_stay_held_when_their_sending_mark_cannot_be_saved(self) -> None:
        await self.hold_first_answer()
        store: SessionStore = self.fixture.bridge.store
        with patch.object(store, "flush", AsyncMock(side_effect=OSError("disk unavailable"))):
            await self.fixture.bridge.controls.send_thread_reply(cast(Any, FakeReplyMessage(303, self.fixture.thread, "later")))
        self.assertEqual([command["text"] for command in self.fixture.socket.sent_json], ["later"])
        self.assertEqual(self.grouped("Q1")[0].state, QuestionState.HELD)

    async def test_a_typed_answer_carries_held_answers_with_it(self) -> None:
        await self.hold_first_answer()
        await self.fixture.bridge.controls.send_thread_reply(cast(Any, FakeReplyMessage(301, self.fixture.thread, "Q2 round")))
        self.assertEqual([command["text"] for command in self.fixture.socket.sent_json], ["Q1: blue\n\nQ2 round"])
        await self.fixture.bridge.controls.handle_command_ack(self.fixture.socket.sent_json[-1])
        self.assertEqual([self.grouped(number)[0].state for number in ("Q1", "Q2")], [QuestionState.ANSWERED] * 2)

    async def test_the_answer_form_shows_the_whole_question(self) -> None:
        body = "**Q12.** Ship it?\n" + "Context that must stay readable. " * 20 + "\nRecommendation: yes."
        modal = await self.card(body)
        shown = "\n".join(item.content for item in modal.walk_children() if isinstance(item, discord.ui.TextDisplay))
        self.assertIn("Recommendation: yes.", shown)
        self.assertIn("Context that must stay readable. " * 20, shown)

    async def test_a_long_form_answer_stays_readable_on_the_card(self) -> None:
        modal = await self.card()
        answer = "Proceed with the migration, " + "but only after the backup check passes and staff are told. " * 4
        modal.fields[0]._value = answer
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        card = self.fixture.thread._messages[cast(int, modal.question.message_id)]
        self.assertIn(discord.utils.escape_markdown(answer.strip()), visible_view_text(card.view))

    async def test_a_delivered_answer_shows_no_private_notice(self) -> None:
        modal = await self.card()
        modal.fields[0]._value = "Yes"
        interaction = FakeInteraction(self.fixture.thread)
        await modal.on_submit(cast(Any, interaction))
        self.assertEqual(self.fixture.socket.sent_json[-1]["text"], "Q12: Yes")
        self.assertEqual((interaction.response.messages, interaction.response.thinking), ([], False))

    async def test_recovery_deduplicates_each_number_and_preserves_answered_state(self) -> None:
        modal = await self.card()
        modal.fields[0]._value = "Yes"
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        await self.fixture.bridge.controls.handle_command_ack(self.fixture.socket.sent_json[-1])
        recovered = await self.restarted_cards()
        original_body = modal.question.questions[0][1]
        count = len(self.fixture.thread.sent_messages)
        await recovered.send_answer(self.fixture.session, cast(discord.Thread, self.fixture.thread), original_body, replay=True)
        self.assertEqual(len(self.fixture.thread.sent_messages), count + 1)
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
        self.assertEqual(stored.state, QuestionState.CLOSED)
        self.assertFalse(any(isinstance(item, discord.ui.Button) for item in view.walk_children()))
        self.assertEqual(
            cast(StoredQuestion, second.cards.record(second.session_id, second.question.key)).state, QuestionState.ANSWERED
        )

    async def test_closing_again_leaves_closed_cards_alone(self) -> None:
        modal = await self.card()
        await modal.cards.close(modal.session_id)
        stored = cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key))
        message = self.fixture.thread._messages[cast(int, stored.message_id)]
        edits = len(message.edits)
        for _ in range(3):  # Reconnects and session end each close the session again.
            await modal.cards.close(modal.session_id)
        self.assertEqual(len(message.edits), edits)

    async def test_a_reasked_card_is_drawn_closed_again_after_a_failed_close(self) -> None:
        modal = await self.card()
        await modal.cards.close(modal.session_id)
        await self.card("**Q12.** Ship it now?\nRecommendation: yes.")  # Re-asked: the card is drawn open again.
        stored = cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key))
        message = self.fixture.thread._messages[cast(int, stored.message_id)]
        failure = discord.HTTPException(cast(Any, SimpleNamespace(status=500, reason="")), "")
        with patch.object(message, "edit", AsyncMock(side_effect=failure)):
            await modal.cards.close(modal.session_id)
        edits = len(message.edits)
        await modal.cards.close(modal.session_id)
        self.assertEqual(len(message.edits), edits + 1)

    async def test_a_failed_closing_edit_is_drawn_again_on_the_next_close(self) -> None:
        modal = await self.card()
        stored = cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key))
        message = self.fixture.thread._messages[cast(int, stored.message_id)]
        failure = discord.HTTPException(cast(Any, SimpleNamespace(status=500, reason="")), "")
        with patch.object(message, "edit", AsyncMock(side_effect=failure)):
            await modal.cards.close(modal.session_id)
        edits = len(message.edits)
        await modal.cards.close(modal.session_id)
        self.assertEqual(len(message.edits), edits + 1)

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
        self.assertNotEqual(self.fixture.session.display_state, "working")  # No typing left running for a failed send.
        await modal.on_submit(cast(Any, FakeInteraction(self.fixture.thread)))
        stored = cast(StoredQuestion, modal.cards.record(modal.session_id, modal.question.key))
        self.assertEqual((stored.state, stored.answer), (QuestionState.UNCONFIRMED, "Yes"))
        self.assertEqual(self.fixture.socket.sent_json, [])
        self.assertTrue(self.fixture.thread.sent_messages[-1])
