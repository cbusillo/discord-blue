from __future__ import annotations

import unittest
from typing import cast
from types import SimpleNamespace
from aiohttp import ClientResponse
from tests.fakes_agent_session import FakeReplyMessage
from unittest.mock import patch

import discord
from discord.http import handle_message_parameters

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.cards import session_status_card
from discord_blue.doodads.agent_session.formatting import is_assistant_message
from discord_blue.doodads.agent_session.protocol import SessionStatus, UserMessage
from discord_blue.doodads.agent_session.sessions import AgentSession
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
        view = message.view
        assert isinstance(view, discord.ui.LayoutView)
        return view

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
            assert params.payload is not None
            payload = params.payload
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
            assert params.payload is not None
            payload = params.payload
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
        error = discord.Forbidden(cast(ClientResponse, SimpleNamespace(status=403, reason="Forbidden")), "denied")
        with patch.object(FakeReplyMessage, "edit", side_effect=error):
            await self.status("error", "Try the terminal")
        self.assertEqual(self.session.control_message_id, anchor)
        self.assertEqual(len(self.thread.sent_messages), 1)
        await self.status("error", "Try the terminal")
        self.assertIn("Failed", visible_text(await self.current_card()))

    async def test_pause_is_waiting_and_continue_refreshes_card(self) -> None:
        await self.status("status_changed", "Turn aborted")
        card = visible_text(await self.current_card())
        self.assertIn("Waiting on you", card)
        self.assertNotIn("Failed", card)
        await self.bridge.send_continue_autonomously(self.thread, cast(discord.User, SimpleNamespace(id=123)))
        self.assertIn("queued", visible_text(await self.current_card()))
        self.assertIn("Working", visible_text(await self.current_card()))

    async def test_edit_failure_still_updates_confirmation_reactions(self) -> None:
        await self.bridge.post_session_controls(self.session)
        message = await self.thread.fetch_message(cast(int, self.session.control_message_id))
        self.session.pending_control_confirmation = "end_session"
        error = discord.Forbidden(cast(ClientResponse, SimpleNamespace(status=403, reason="Forbidden")), "denied")
        with patch.object(FakeReplyMessage, "edit", side_effect=error):
            await self.bridge.refresh_session_controls(self.session, cast(discord.Thread, self.thread))
        self.assertEqual(message.reactions, [bridge_module.REACTION_APPROVAL_APPROVE, bridge_module.REACTION_APPROVAL_DENY])

    async def test_reconnect_replaces_old_card_without_reactivating_old_controls(self) -> None:
        await self.status("status_changed", "Turn started")
        old = await self.thread.fetch_message(cast(int, self.session.control_message_id))
        self.session.control_message_id = None  # The replacement connection has no in-memory anchor.
        await self.status("status_changed", "Waiting on a decision in the Codex TUI")
        self.assertTrue(old.deleted)
        self.assertNotEqual(self.session.control_message_id, old.id)
        self.assertIn("Waiting on you", visible_text(await self.current_card()))
        self.assertFalse(
            await self.bridge.handle_thread_reaction(
                cast(discord.Thread, self.thread), old.id, "⏸️", cast(discord.User, SimpleNamespace(id=123))
            )
        )

    async def test_foreign_authored_card_is_never_replaced(self) -> None:
        await self.status("status_changed", "Turn started")
        old = await self.thread.fetch_message(cast(int, self.session.control_message_id))
        old.author.id = 123
        self.session.control_message_id = None
        await self.status("status_changed", "Turn started")
        self.assertFalse(old.deleted)
        self.assertNotEqual(self.session.control_message_id, old.id)

    async def test_rejected_and_undelivered_replies_do_not_look_queued(self) -> None:
        from tests.fakes_agent_session import FakeWebSocket
        from discord_blue.doodads.agent_session.sessions import PendingRemoteCommand

        await self.status("turn_complete", "Waiting for direction")
        self.session.pending_commands["reply"] = PendingRemoteCommand(self.thread.id, 123, "reply")
        await self.bridge.handle_command_reject(
            {"session_id": self.session.session_id, "command_id": "reply", "reason": "Session busy"}
        )
        text = visible_text(await self.current_card())
        self.assertIn("Done", text)
        self.assertIn("Reply not delivered: Session busy", self.thread.sent_messages)
        self.session.acknowledged = True
        reply = FakeReplyMessage(123, self.thread, "Try again")
        self.thread.add_message(reply)
        with patch.object(FakeWebSocket, "send_json", side_effect=OSError("lost socket")):
            await self.bridge.send_thread_reply(cast(discord.Message, reply))
        self.assertIn("Done", visible_text(await self.current_card()))
        self.assertIn("delivery could not be confirmed", reply.replies[0])
        self.assertNotIn("queued", visible_text(await self.current_card()))

    async def test_question_changes_existing_card_to_waiting(self) -> None:
        await self.status("status_changed", "Turn started")
        await self.fixture.prompt()
        text = visible_text(await self.current_card())
        self.assertIn("Waiting on you", text)
        self.assertIn("question controls", text)

    async def test_failed_fetch_keeps_connection_and_anchor(self) -> None:
        await self.status("status_changed", "Turn started")
        anchor = self.session.control_message_id
        error = discord.Forbidden(cast(ClientResponse, SimpleNamespace(status=403, reason="Forbidden")), "denied")
        with patch.object(type(self.thread), "fetch_message", side_effect=error):
            await self.status("error", "Server error")
        self.assertIs(self.bridge.sessions.get(self.session.session_id), self.session)
        self.assertFalse(self.fixture.socket.closed)
        self.assertEqual(self.session.control_message_id, anchor)
        self.assertEqual(len(self.thread.sent_messages), 1)

    async def test_rejected_pause_preserves_running_controls_and_newer_completion(self) -> None:
        user = cast(discord.User, SimpleNamespace(id=123))
        await self.bridge.handle_user_message(
            UserMessage(session_id=self.session.session_id, session_epoch=self.session.session_epoch, message="Work on this")
        )
        await self.bridge.send_pause_current_turn(self.thread, user)
        command = self.fixture.socket.sent_json[-1]
        await self.bridge.handle_command_reject({**command, "reason": "Pause rejected"})
        message = await self.thread.fetch_message(cast(int, self.session.control_message_id))
        self.assertIn("Working", visible_text(await self.current_card()))
        self.assertIn(bridge_module.REACTION_CONTROL_PAUSE, message.reactions)
        await self.bridge.send_pause_current_turn(self.thread, user)
        command = self.fixture.socket.sent_json[-1]
        await self.status("turn_complete", "Waiting for direction")
        await self.bridge.handle_command_reject({**command, "reason": "No running turn"})
        self.assertIn("Done", visible_text(await self.current_card()))
        self.assertNotIn("Failed", visible_text(await self.current_card()))

    async def test_uncorrelated_claude_activity_does_not_claim_working_or_still_waiting(self) -> None:
        await self.status("status_changed", "Claude Code is waiting for approval in the terminal (Bash)")
        await self.status("status_changed", "Approval status unconfirmed; tools are active. Check the Claude Code terminal.")
        text = visible_text(await self.current_card())
        self.assertIn("Check terminal", text)
        self.assertNotIn("Working", text)
        self.assertNotIn("Waiting on you", text)

    async def test_pause_tap_does_not_overwrite_ack_arriving_during_card_update(self) -> None:
        await self.bridge.handle_user_message(
            UserMessage(session_id=self.session.session_id, session_epoch=self.session.session_epoch, message="Work on this")
        )
        anchor = cast(int, self.session.control_message_id)
        original = self.bridge.show_pending_control

        async def update_and_ack(session: AgentSession, thread: discord.Thread, detail: str) -> None:
            await original(session, thread, detail)
            await self.bridge.handle_command_ack(self.fixture.socket.sent_json[-1])

        with patch.object(self.bridge, "show_pending_control", new=update_and_ack):
            await self.bridge.handle_thread_reaction(
                cast(discord.Thread, self.thread),
                anchor,
                bridge_module.REACTION_CONTROL_PAUSE,
                cast(discord.User, SimpleNamespace(id=123)),
            )
        message = await self.thread.fetch_message(anchor)
        self.assertIn(bridge_module.REACTION_DELIVERED, message.reactions)
        self.assertNotIn(bridge_module.REACTION_QUEUED, message.reactions)
