from __future__ import annotations

import unittest
from typing import Any, cast
from unittest.mock import patch

import discord
from discord.http import handle_message_parameters

from discord_blue.doodads.agent_session.cards import session_status_card
from discord_blue.doodads.agent_session.formatting import is_assistant_message
from discord_blue.doodads.agent_session.protocol import SessionStatus, UserMessage
from tests.fakes_agent_prompts import prompt_fixture


def visible_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


class StatusCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = self.enterContext(prompt_fixture())
        self.bridge, self.session, self.thread = self.fixture.bridge, self.fixture.session, self.fixture.thread

    async def status(self, event: str, message: str, epoch: str | None = None) -> None:
        await self.bridge.handle_session_status(
            event,
            SessionStatus(
                session_id=self.session.session_id,
                session_epoch=epoch or self.session.session_epoch,
                message=message,
                assistant_message=None,
            ),
        )

    async def current_card(self) -> discord.ui.LayoutView:
        message = await self.thread.fetch_message(cast(int, self.session.control_message_id))
        self.assertEqual(message.content, "")
        self.assertIsInstance(message.view, discord.ui.LayoutView)
        return cast(discord.ui.LayoutView, message.view)

    async def test_status_transitions_update_one_anchor_for_both_harnesses(self) -> None:
        for harness in ("codex", "claude"):
            self.session.hello.harness = harness
            for event, detail, state_word in (
                ("status_changed", "Turn started", "Working"),
                ("status_changed", "Claude Code is waiting for approval in the terminal (Bash)", "Waiting on you"),
                ("turn_complete", "Waiting for direction", "Done"),
                ("error", "Connection refused; check the terminal.", "Failed"),
            ):
                with self.subTest(harness=harness, event=event, detail=detail):
                    await self.status(event, detail)
                    text = visible_text(await self.current_card())
                    self.assertIn(state_word, text)
                    self.assertIn(detail, text)
                    self.assertFalse(is_assistant_message(text))
            self.assertEqual(len(self.thread.sent_messages), 1)

    async def test_stale_epoch_cannot_change_card_or_state(self) -> None:
        await self.status("status_changed", "Turn started")
        before = visible_text(await self.current_card())
        await self.status("error", "Stale failure", epoch="old-epoch")
        self.assertEqual(visible_text(await self.current_card()), before)
        self.assertEqual(self.session.display_state, "working")

    async def test_tui_prompt_restarts_work_after_done(self) -> None:
        await self.status("turn_complete", "Waiting for direction")
        await self.bridge.handle_user_message(
            UserMessage(session_id=self.session.session_id, session_epoch=self.session.session_epoch, message="Try again")
        )
        self.assertIn("Working", visible_text(await self.current_card()))
        self.assertNotIn("Waiting for direction", visible_text(await self.current_card()))

    async def test_existing_text_anchor_converts_without_legacy_content_or_embeds(self) -> None:
        await self.bridge.post_session_controls(self.session)
        message = await self.thread.fetch_message(cast(int, self.session.control_message_id))
        message.content = "old plain controls"
        await self.status("status_changed", "Turn started")
        edit = message.edit_kwargs[-1]
        self.assertEqual(edit["embeds"], [])
        self.assertEqual(message.content, "")
        view = cast(discord.ui.LayoutView, edit["view"])
        with handle_message_parameters(content=None, embeds=[], view=view) as params:
            payload = cast(dict[str, Any], params.payload)
            self.assertIsNone(payload["content"])
            self.assertEqual(payload["embeds"], [])
            self.assertTrue(payload["flags"] & discord.MessageFlags(components_v2=True).value)
        self.assertIn("Working", visible_text(view))

    async def test_v2_send_needs_no_manage_messages_and_does_not_ping(self) -> None:
        self.thread._manage_messages = False
        await self.bridge.post_session_controls(self.session)
        self.assertEqual(len(self.thread.sent_messages), 1)  # No obsolete link-preview permission notice.
        kwargs = self.thread.sent_kwargs[-1]
        self.assertTrue(kwargs["silent"])
        mentions = cast(discord.AllowedMentions, kwargs["allowed_mentions"])
        self.assertEqual(mentions.to_dict()["parse"], [])
        with handle_message_parameters(view=await self.current_card(), allowed_mentions=mentions) as params:
            payload = cast(dict[str, Any], params.payload)
            self.assertNotIn("content", payload)
            self.assertNotIn("embeds", payload)
            self.assertTrue(payload["flags"] & discord.MessageFlags(components_v2=True).value)

    async def test_confirmation_and_supported_controls_are_literal(self) -> None:
        self.session.pending_control_confirmation = "end_session"
        text = visible_text(session_status_card(self.session, ["✅", "✖️"]))
        self.assertIn("End this session?", text)
        self.assertIn("confirm", text)
        self.assertIn("keep the session open", text)
        self.session.pending_control_confirmation = None
        self.session.hello.capabilities = frozenset()
        card = session_status_card(self.session, self.bridge.session_control_reactions(self.session))
        text = visible_text(card)
        self.assertNotIn("pause", text)
        self.assertNotIn("continue", text)
        self.assertNotIn("end", text)

    async def test_edit_failure_keeps_anchor_and_retry_can_refresh(self) -> None:
        await self.bridge.post_session_controls(self.session)
        anchor = self.session.control_message_id
        message = await self.thread.fetch_message(cast(int, anchor))
        error = discord.Forbidden(cast(Any, type("Response", (), {"status": 403, "reason": "Forbidden"})()), "denied")
        with patch.object(message, "edit", side_effect=error):
            await self.status("error", "Try the terminal")
        self.assertEqual(self.session.control_message_id, anchor)
        self.assertEqual(len(self.thread.sent_messages), 1)
        await self.status("error", "Try the terminal")
        self.assertIn("Failed", visible_text(await self.current_card()))
