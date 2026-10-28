from __future__ import annotations

import unittest
from typing import cast
from types import SimpleNamespace
from aiohttp import ClientResponse
from tests.fakes_agent_session import FakeReplyMessage, FakeThread
from unittest.mock import patch

import discord
from discord.http import handle_message_parameters

from discord_blue.doodads.agent_session import controls as controls_module
from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.cards import session_status_card, STATUS_LABELS
from discord_blue.doodads.agent_session.formatting import is_assistant_message
from discord_blue.agent_client import AgentSessionClient
from discord_blue.doodads.agent_session.protocol import LAUNCH_BRIEF_ORIGIN, SessionStatus, UserMessage
from discord_blue.doodads.agent_session.sessions import AgentSession
from tests.fakes_agent_prompts import prompt_fixture, PromptFixture


def visible_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


class StatusCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture: PromptFixture = self.enterContext(prompt_fixture())
        self.bridge = self.fixture.bridge
        self.session = self.fixture.session
        self.thread = self.fixture.thread

    async def status(self, event: str, message: str, epoch: str | None = None) -> None:
        await self.bridge.status.handle_session_status(
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
                ("status_changed", "Turn started", STATUS_LABELS["working"][0]),
                ("status_changed", "Claude Code is waiting for approval in the terminal (Bash)", STATUS_LABELS["waiting"][0]),
                ("turn_complete", "Waiting for direction", STATUS_LABELS["done"][0]),
                ("error", "Connection refused; check the terminal.", STATUS_LABELS["failed"][0]),
            ):
                with self.subTest(harness=harness, event=event, detail=detail):
                    await self.status(event, detail)
                    text = visible_text(await self.current_card())
                    self.assertIn(state_word, text)
                    self.assertIn(detail, text)
                    self.assertFalse(is_assistant_message(text))
            self.assertEqual(sum(isinstance(view, discord.ui.LayoutView) for view in self.thread.sent_views), 1)

    async def test_stale_epoch_cannot_change_card_or_state(self) -> None:
        await self.status("status_changed", "Turn started")
        before = visible_text(await self.current_card())
        await self.status("error", "Stale failure", epoch="old-epoch")
        self.assertEqual(visible_text(await self.current_card()), before)
        self.assertEqual(self.session.display_state, "working")

    async def test_tui_prompt_restarts_work_after_done(self) -> None:
        await self.status("turn_complete", "Waiting for direction")
        await self.bridge.status.handle_user_message(
            UserMessage(session_id=self.session.session_id, session_epoch=self.session.session_epoch, message="Try again")
        )
        self.assertIn(STATUS_LABELS["working"][0], visible_text(await self.current_card()))
        self.assertNotIn("Waiting for direction", visible_text(await self.current_card()))

    async def test_existing_text_anchor_converts_without_legacy_content_or_embeds(self) -> None:
        await self.bridge.controls.post_session_controls(self.session)
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
        self.assertIn(STATUS_LABELS["working"][0], visible_text(view))

    async def test_v2_send_needs_no_manage_messages_and_does_not_ping(self) -> None:
        self.thread._manage_messages = False
        await self.bridge.controls.post_session_controls(self.session)
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
        card = session_status_card(self.session, self.bridge.controls.session_control_reactions(self.session))
        text = visible_text(card)
        self.assertNotIn("pause", text)
        self.assertNotIn("continue", text)
        self.assertNotIn("end", text)

    async def test_edit_failure_keeps_anchor_and_retry_can_refresh(self) -> None:
        await self.bridge.controls.post_session_controls(self.session)
        anchor = self.session.control_message_id
        error = discord.Forbidden(cast(ClientResponse, cast(object, SimpleNamespace(status=403, reason="Forbidden"))), "denied")
        with patch.object(FakeReplyMessage, "edit", side_effect=error):
            await self.status("error", "Try the terminal")
        self.assertEqual(self.session.control_message_id, anchor)
        self.assertEqual(len(self.thread.sent_messages), 2)
        await self.status("error", "Try the terminal")
        self.assertIn(STATUS_LABELS["failed"][0], visible_text(await self.current_card()))

    async def test_pause_is_waiting_and_continue_refreshes_card(self) -> None:
        await self.status("status_changed", "Turn aborted")
        card = visible_text(await self.current_card())
        self.assertIn(STATUS_LABELS["waiting"][0], card)
        self.assertNotIn(STATUS_LABELS["failed"][0], card)
        await self.bridge.controls.send_continue_autonomously(self.thread, cast(discord.User, SimpleNamespace(id=123)))
        self.assertIn("queued", visible_text(await self.current_card()))
        self.assertIn(STATUS_LABELS["working"][0], visible_text(await self.current_card()))

    async def test_edit_failure_still_updates_confirmation_reactions(self) -> None:
        await self.bridge.controls.post_session_controls(self.session)
        message = await self.thread.fetch_message(cast(int, self.session.control_message_id))
        self.session.pending_control_confirmation = "end_session"
        error = discord.Forbidden(cast(ClientResponse, cast(object, SimpleNamespace(status=403, reason="Forbidden"))), "denied")
        with patch.object(FakeReplyMessage, "edit", side_effect=error):
            await self.bridge.controls.refresh_session_controls(self.session, cast(discord.Thread, self.thread))
        self.assertEqual(message.reactions, [controls_module.REACTION_APPROVAL_APPROVE, controls_module.REACTION_APPROVAL_DENY])

    async def test_reconnect_recovers_and_edits_existing_card(self) -> None:
        await self.status("status_changed", "Turn started")
        old = await self.thread.fetch_message(cast(int, self.session.control_message_id))
        self.session.control_message_id = None
        await self.status("status_changed", "Waiting on a decision in the Codex TUI")
        self.assertFalse(old.deleted)
        self.assertEqual(self.session.control_message_id, old.id)
        self.assertIn(STATUS_LABELS["waiting"][0], visible_text(await self.current_card()))

    async def test_spawned_launch_brief_is_hidden_but_later_terminal_input_is_shown(self) -> None:
        for harness in ("codex", "claude"):
            self.session.hello.harness = harness
            self.session.hello.spawned = True
            await self.bridge.status.handle_user_message(
                UserMessage(self.session.session_id, self.session.session_epoch, "Automated task brief", LAUNCH_BRIEF_ORIGIN)
            )
            self.assertFalse(any("Automated task brief" in message for message in self.thread.sent_messages))
            await self.bridge.status.handle_user_message(
                UserMessage(self.session.session_id, self.session.session_epoch, "Human correction")
            )
            self.assertTrue(any("Human correction" in message for message in self.thread.sent_messages))

    async def test_director_launch_and_uncertain_reused_background_input_stay_visible(self) -> None:
        for spawned, origin in ((False, LAUNCH_BRIEF_ORIGIN), (True, None)):
            self.session.hello.spawned = spawned
            await self.bridge.status.handle_user_message(
                UserMessage(self.session.session_id, self.session.session_epoch, "Keep this input", origin)
            )
            self.assertEqual(sum("Keep this input" in message for message in self.thread.sent_messages), 1 if not spawned else 2)

    async def test_missing_anchor_is_recreated_once_and_pending_commands_follow_it(self) -> None:
        from discord_blue.doodads.agent_session.sessions import PendingRemoteCommand

        await self.status("status_changed", "Turn started")
        old = cast(int, self.session.control_message_id)
        self.thread.delete_message(old)
        self.session.pending_commands["continue"] = PendingRemoteCommand(self.thread.id, old, "continue_autonomously")
        import asyncio

        await asyncio.gather(
            *(self.bridge.controls.refresh_session_controls(self.session, cast(discord.Thread, self.thread)) for _ in range(2))
        )
        self.assertNotEqual(self.session.control_message_id, old)
        self.assertEqual(self.session.pending_commands["continue"].message_id, self.session.control_message_id)
        self.assertEqual(len(self.thread.sent_messages), 2)

    async def test_replaced_connection_cannot_edit_the_shared_anchor_after_fetch(self) -> None:
        await self.status("status_changed", "Turn started")
        anchor = cast(int, self.session.control_message_id)
        message = await self.thread.fetch_message(anchor)
        before = len(message.edits)
        original = self.thread.fetch_message

        async def replace_during_fetch(_thread: FakeThread, message_id: int) -> FakeReplyMessage:
            fetched = await original(message_id)
            replacement = AgentSession(
                self.session.hello, self.session.websocket, thread_id=self.thread.id, control_message_id=anchor
            )
            self.bridge.sessions.register(replacement)
            return fetched

        with patch.object(FakeThread, "fetch_message", new=replace_during_fetch):
            await self.bridge.controls.refresh_session_controls(self.session, cast(discord.Thread, self.thread))
        self.assertEqual(len(message.edits), before)
        self.assertFalse(message.deleted)

    async def test_unreadable_history_and_failed_replacement_keep_the_connection(self) -> None:
        error = discord.Forbidden(cast(ClientResponse, cast(object, SimpleNamespace(status=403, reason="Forbidden"))), "denied")

        with patch.object(FakeThread, "history", side_effect=error):
            await self.status("status_changed", "Turn started")
        self.assertFalse(self.fixture.socket.closed)
        self.assertIsNone(self.session.control_message_id)
        self.assertEqual(self.thread.sent_messages, [])
        await self.status("status_changed", "Turn started")
        anchor = cast(int, self.session.control_message_id)
        self.thread.delete_message(anchor)
        self.thread.send_raises = True
        await self.status("status_changed", "Still working")
        self.assertFalse(self.fixture.socket.closed)
        self.assertEqual(self.session.control_message_id, anchor)
        self.thread.send_raises = False
        await self.status("status_changed", "Still working")
        self.assertNotEqual(self.session.control_message_id, anchor)

    async def test_legacy_card_discovery_reaches_past_recent_messages(self) -> None:
        await self.status("status_changed", "Turn started")
        anchor = self.session.control_message_id
        from tests.fakes_agent_session import add_bot_message

        for index in range(60):
            add_bot_message(self.thread, 2000 + index, "Later conversation")
        self.session.control_message_id = None
        await self.status("turn_complete", "Waiting")
        self.assertEqual(self.session.control_message_id, anchor)
        self.assertEqual(len(self.thread.sent_messages), 1)

    async def test_claude_terminal_request_has_one_notice_and_unknown_activity_is_quiet(self) -> None:
        from pathlib import Path
        from unittest.mock import AsyncMock
        from discord_blue.claude_channel.session import ClaudeSession, Identity, waiting_message
        from discord_blue.codex_bridge.config import BridgeConfig

        client = ClaudeSession(
            BridgeConfig("ws://localhost/agent-session/connect", "fake", Path("/unused"), "test"),
            Identity(self.session.session_id, "/work", None, 1),
            AsyncMock(),
        )
        with patch.object(AgentSessionClient, "publish") as publish:
            await client.on_permission_request({"tool_name": "Bash"})
            await client.on_hook({"event": "PreToolUse"})
            for call in publish.call_args_list:
                await self.bridge.handle_event(
                    self.session,
                    call.args[0],
                    {"session_id": self.session.session_id, "session_epoch": self.session.session_epoch, **call.kwargs},
                )
        self.assertEqual(self.thread.sent_messages.count(waiting_message({"tool_name": "Bash"})), 1)
        self.assertEqual(len(self.thread.sent_messages), 2)

    async def test_updates_during_initial_send_coalesce_and_render_latest_state(self) -> None:
        for final_state in ("done", "disconnected"):
            with self.subTest(final_state=final_state):
                self.fixture = self.enterContext(prompt_fixture())
                self.bridge = self.fixture.bridge
                self.session = self.fixture.session
                self.thread = self.fixture.thread
                try:
                    await self.initial_send_receives_update(final_state)
                finally:
                    if self.session.grace_task is not None:
                        self.session.grace_task.cancel()

    async def initial_send_receives_update(self, final_state: str) -> None:
        import asyncio

        sending, release = asyncio.Event(), asyncio.Event()
        original = self.thread.send

        async def blocked_send(_thread: FakeThread, content: str | None = None, **kwargs: object) -> FakeReplyMessage:
            sending.set()
            await release.wait()
            return await original(content, **kwargs)

        with patch.object(FakeThread, "send", new=blocked_send), patch.object(controls_module, "CONTROL_CARD_WAIT_SECONDS", 0.01):
            initial = asyncio.create_task(self.status("status_changed", "Turn started"))
            try:
                await asyncio.wait_for(sending.wait(), 1)
                recovery = self.session.control_update_task
                if final_state == "done":
                    await self.status("turn_complete", "Turn complete")
                else:
                    self.bridge.start_grace(self.session)
                self.assertIs(self.session.control_update_task, recovery)
                await asyncio.wait_for(initial, 0.5)
            finally:
                release.set()
            assert recovery is not None
            await asyncio.wait_for(recovery, 1)
        self.assertIn(STATUS_LABELS[final_state][0], visible_text(await self.current_card()))
        self.assertEqual(len(self.thread.sent_messages), 1)

    async def test_ordinary_turn_keeps_anchor_and_inline_answer(self) -> None:
        await self.status("status_changed", "Turn started")
        anchor = self.session.control_message_id
        await self.bridge.status.handle_user_message(UserMessage(self.session.session_id, self.session.session_epoch, "Try again"))
        await self.bridge.status.handle_session_status(
            "turn_complete", SessionStatus(self.session.session_id, self.session.session_epoch, "Waiting", "Useful answer")
        )
        self.assertEqual(self.session.control_message_id, anchor)
        self.assertTrue(any("Useful answer" in message for message in self.thread.sent_messages))
        self.assertIn(STATUS_LABELS["done"][0], visible_text(await self.current_card()))

    async def test_long_and_rejected_error_notices_keep_the_failed_card(self) -> None:
        from unittest.mock import AsyncMock
        from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT

        detail = "provider error " * DISCORD_MESSAGE_LIMIT
        await self.status("error", detail)
        self.assertEqual(len(self.thread.sent_messages[-1]), DISCORD_MESSAGE_LIMIT)
        error = discord.HTTPException(
            cast(ClientResponse, cast(object, SimpleNamespace(status=503, reason="Unavailable"))), "failed"
        )
        with patch.object(AgentSessionBridge, "post_thread_notice", new=AsyncMock(side_effect=error)):
            await self.status("error", "Another provider failure")
        self.assertEqual(self.session.display_state, "failed")
        self.assertIn(STATUS_LABELS["failed"][0], visible_text(await self.current_card()))
        self.assertFalse(self.fixture.socket.closed)
        self.assertIs(self.bridge.sessions.get(self.session.session_id), self.session)

    async def test_shutdown_updates_the_saved_card_outside_the_lifecycle_lock(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from discord_blue.doodads.agent_session.store import SessionStore

        with TemporaryDirectory() as directory:
            store = self.bridge.store = SessionStore(Path(directory) / "sessions.json")
            await store.start()
            try:
                self.bridge.save_session(self.session, "live")
                await self.status("status_changed", "Turn started")
                anchor = cast(int, self.session.control_message_id)
                self.fixture.socket.closed = True
                anchor_message: FakeReplyMessage = await self.thread.fetch_message(anchor)
                original = anchor_message.edit

                async def observe_edit(content: str | None = None, **kwargs: object) -> None:
                    self.assertFalse(self.bridge.session_lifecycle_lock(self.session.session_id).locked())
                    self.assertEqual(store.records[self.session.session_id].status, "grace")
                    await original(content, **kwargs)

                with patch.object(anchor_message, "edit", new=observe_edit):
                    await self.bridge.disconnect_active_session(self.session.session_id, self.session)
                message = await self.thread.fetch_message(anchor)
                self.assertIn(STATUS_LABELS["disconnected"][0], visible_text(cast(discord.ui.LayoutView, message.view)))
                self.assertIsNone(self.bridge.sessions.get(self.session.session_id))
            finally:
                await store.close()

    async def test_failure_is_new_and_routine_unknown_status_is_quiet(self) -> None:
        await self.status("status_changed", "Turn started")
        await self.status("status_changed", "Approval status unconfirmed; tools are active")
        self.assertEqual(len(self.thread.sent_messages), 1)
        await self.status("error", "Connection failed")
        self.assertIn("Connection failed", self.thread.sent_messages)

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
        await self.bridge.controls.handle_command_reject(
            {"session_id": self.session.session_id, "command_id": "reply", "reason": "Session busy"}
        )
        text = visible_text(await self.current_card())
        self.assertIn(STATUS_LABELS["done"][0], text)
        self.assertIn("Reply not delivered: Session busy", self.thread.sent_messages)
        self.session.acknowledged = True
        reply = FakeReplyMessage(123, self.thread, "Try again")
        self.thread.add_message(reply)
        with patch.object(FakeWebSocket, "send_json", side_effect=OSError("lost socket")):
            await self.bridge.controls.send_thread_reply(cast(discord.Message, reply))
        self.assertIn(STATUS_LABELS["done"][0], visible_text(await self.current_card()))
        self.assertIn("delivery could not be confirmed", reply.replies[0])
        self.assertNotIn("queued", visible_text(await self.current_card()))

    async def test_question_changes_existing_card_to_waiting(self) -> None:
        await self.status("status_changed", "Turn started")
        await self.fixture.prompt()
        text = visible_text(await self.current_card())
        self.assertIn(STATUS_LABELS["waiting"][0], text)
        self.assertIn("question controls", text)

    async def test_failed_fetch_keeps_connection_and_anchor(self) -> None:
        await self.status("status_changed", "Turn started")
        anchor = self.session.control_message_id
        error = discord.Forbidden(cast(ClientResponse, cast(object, SimpleNamespace(status=403, reason="Forbidden"))), "denied")
        with patch.object(type(self.thread), "fetch_message", side_effect=error):
            await self.status("error", "Server error")
        self.assertIs(self.bridge.sessions.get(self.session.session_id), self.session)
        self.assertFalse(self.fixture.socket.closed)
        self.assertEqual(self.session.control_message_id, anchor)
        self.assertEqual(len(self.thread.sent_messages), 2)

    async def test_rejected_pause_preserves_running_controls_and_newer_completion(self) -> None:
        user = cast(discord.User, SimpleNamespace(id=123))
        await self.bridge.status.handle_user_message(
            UserMessage(session_id=self.session.session_id, session_epoch=self.session.session_epoch, message="Work on this")
        )
        await self.bridge.controls.send_pause_current_turn(self.thread, user)
        command = self.fixture.socket.sent_json[-1]
        await self.bridge.controls.handle_command_reject({**command, "reason": "Pause rejected"})
        message = await self.thread.fetch_message(cast(int, self.session.control_message_id))
        self.assertIn(STATUS_LABELS["working"][0], visible_text(await self.current_card()))
        self.assertIn(controls_module.REACTION_CONTROL_PAUSE, message.reactions)
        await self.bridge.controls.send_pause_current_turn(self.thread, user)
        command = self.fixture.socket.sent_json[-1]
        await self.status("turn_complete", "Waiting for direction")
        await self.bridge.controls.handle_command_reject({**command, "reason": "No running turn"})
        self.assertIn(STATUS_LABELS["done"][0], visible_text(await self.current_card()))
        self.assertNotIn(STATUS_LABELS["failed"][0], visible_text(await self.current_card()))

    async def test_uncorrelated_claude_activity_does_not_claim_working_or_still_waiting(self) -> None:
        await self.status("status_changed", "Claude Code is waiting for approval in the terminal (Bash)")
        await self.status("status_changed", "Approval status unconfirmed; tools are active. Check the Claude Code terminal.")
        text = visible_text(await self.current_card())
        self.assertIn("Check terminal", text)
        self.assertNotIn(STATUS_LABELS["working"][0], text)
        self.assertNotIn(STATUS_LABELS["waiting"][0], text)

    async def test_pause_tap_does_not_overwrite_ack_arriving_during_card_update(self) -> None:
        await self.bridge.status.handle_user_message(
            UserMessage(session_id=self.session.session_id, session_epoch=self.session.session_epoch, message="Work on this")
        )
        anchor = cast(int, self.session.control_message_id)
        original = self.bridge.controls.show_pending_control

        async def update_and_ack(session: AgentSession, thread: discord.Thread, detail: str) -> None:
            await original(session, thread, detail)
            await self.bridge.controls.handle_command_ack(self.fixture.socket.sent_json[-1])

        with patch.object(self.bridge.controls, "show_pending_control", new=update_and_ack):
            await self.bridge.handle_thread_reaction(
                cast(discord.Thread, self.thread),
                anchor,
                controls_module.REACTION_CONTROL_PAUSE,
                cast(discord.User, SimpleNamespace(id=123)),
            )
        message = await self.thread.fetch_message(anchor)
        self.assertIn(controls_module.REACTION_DELIVERED, message.reactions)
        self.assertNotIn(controls_module.REACTION_QUEUED, message.reactions)

    async def test_reply_ack_updates_queued_detail_and_preserves_newer_native_status(self) -> None:
        self.session.acknowledged = True
        reply = FakeReplyMessage(123, self.thread, "Work on this")
        self.thread.add_message(reply)
        await self.bridge.controls.send_thread_reply(cast(discord.Message, reply))
        command = self.fixture.socket.sent_json[-1]
        await self.bridge.controls.handle_command_ack(command)
        self.assertIn("delivered", visible_text(await self.current_card()))
        self.assertNotIn("queued", visible_text(await self.current_card()))
        await self.status("status_changed", "Waiting on a decision in the Codex TUI")
        await self.bridge.controls.handle_command_ack(command)
        self.assertIn(STATUS_LABELS["waiting"][0], visible_text(await self.current_card()))

    async def test_refresh_cannot_forget_a_new_anchor_published_during_fetch(self) -> None:
        await self.status("status_changed", "Turn started")
        old_id = cast(int, self.session.control_message_id)
        original = self.thread.fetch_message
        new_card = None

        async def fetch(message_id: int) -> FakeReplyMessage:
            nonlocal new_card
            if message_id == old_id:
                new_card = await self.thread.send(view=session_status_card(self.session, []))
                self.session.control_message_id = new_card.id
                self.thread.delete_message(old_id)
            return await original(message_id)

        with patch.object(self.thread, "fetch_message", new=fetch):
            await self.bridge.controls.refresh_session_controls(self.session, cast(discord.Thread, self.thread))
        assert new_card is not None
        self.assertEqual(self.session.control_message_id, new_card.id)
        self.assertEqual(len(self.thread.sent_messages), 2)

    async def test_replayed_question_creates_current_waiting_card_without_turn_start(self) -> None:
        await self.fixture.prompt()
        self.assertIn(STATUS_LABELS["waiting"][0], visible_text(await self.current_card()))
