from __future__ import annotations

import asyncio
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import discord

from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.cleanup import SessionCleanups
from discord_blue.doodads.agent_session.director_questions import (
    QuestionAnswerButton,
    QuestionAnswerModal,
    QuestionCards,
    detect_questions,
)
from discord_blue.doodads.agent_session.sessions import PendingSessionCleanup
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
        self.assertNotIn("Ship it?", visible_view_text(message.view))

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
