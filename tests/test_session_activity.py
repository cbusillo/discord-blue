from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import discord
from aiohttp import ClientResponse

from discord_blue.agent_client import AgentSessionClient
from discord_blue.doodads.agent_session import controls as controls_module
from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.protocol import LAUNCH_BRIEF_ORIGIN, SessionStatus, UserMessage
from tests.fakes_agent_prompts import PromptFixture, prompt_fixture
from tests.fakes_agent_session import FakeReplyMessage, FakeThread


class SessionActivityTests(unittest.IsolatedAsyncioTestCase):
    """While a session works, its thread shows Discord's typing indicator; nothing else reports its state."""

    async def asyncSetUp(self) -> None:
        self.enterContext(patch.object(controls_module, "TYPING_REFRESH_SECONDS", 0.01))
        self.fixture: PromptFixture = self.enterContext(prompt_fixture())
        self.bridge = self.fixture.bridge
        self.session = self.fixture.session
        self.thread = self.fixture.thread

    async def status(self, event: str, message: str, epoch: str | None = None, answer: str | None = None) -> None:
        await self.bridge.status.handle_session_status(
            event,
            SessionStatus(
                session_id=self.session.session_id,
                session_epoch=epoch or self.session.session_epoch,
                message=message,
                assistant_message=answer,
            ),
        )

    async def typing_continues(self) -> bool:
        """Whether the thread is still asked to show typing a few refreshes from now."""
        await asyncio.sleep(0.03)
        before = self.thread.typing_requests
        await asyncio.sleep(0.05)
        return self.thread.typing_requests > before

    async def test_typing_shows_only_while_either_harness_works(self) -> None:
        for harness in ("codex", "claude"):
            self.session.hello.harness = harness
            for event, detail, working in (
                ("status_changed", "Turn started", True),
                ("status_changed", "Claude Code is waiting for approval in the terminal (Bash)", False),
                ("status_changed", "Turn started", True),
                ("turn_complete", "Waiting for direction", False),
                ("status_changed", "Turn started", True),
                ("error", "Connection refused; check the terminal.", False),
                ("status_changed", "Turn started", True),
                ("status_changed", "Turn aborted", False),
            ):
                with self.subTest(harness=harness, event=event, detail=detail):
                    await self.status(event, detail)
                    self.assertEqual(await self.typing_continues(), working)
        self.assertFalse(any(isinstance(view, discord.ui.LayoutView) for view in self.thread.sent_views))

    async def test_a_typing_request_in_flight_lands_before_the_answer(self) -> None:
        landed: list[int] = []
        gate = asyncio.Event()

        async def slow_typing(thread: FakeThread) -> None:
            await gate.wait()
            landed.append(len(thread.sent_messages))

        with patch.object(FakeThread, "typing", new=slow_typing):
            await self.status("status_changed", "Turn started")
            await asyncio.sleep(0)
            completing = asyncio.create_task(self.status("turn_complete", "Waiting", answer="Useful answer"))
            await asyncio.sleep(0.02)
            self.assertEqual(self.thread.sent_messages, [])
            gate.set()
            await completing
        self.assertEqual(landed, [0])
        self.assertTrue(any("Useful answer" in message for message in self.thread.sent_messages))

    async def test_work_that_restarts_during_a_typing_request_keeps_one_task(self) -> None:
        gate = asyncio.Event()
        in_flight = 0
        most = 0

        async def slow_typing(_thread: FakeThread) -> None:
            nonlocal in_flight, most
            in_flight += 1
            most = max(most, in_flight)
            await gate.wait()
            in_flight -= 1

        with patch.object(FakeThread, "typing", new=slow_typing):
            await self.status("status_changed", "Turn started")
            await asyncio.sleep(0)
            task = self.session.typing_task
            await self.status("status_changed", "Waiting on a decision in the Codex TUI")
            await self.status("status_changed", "Turn started")
            self.assertIs(self.session.typing_task, task)
            gate.set()
            await asyncio.sleep(0.03)
        self.assertEqual(most, 1)
        self.assertTrue(await self.typing_continues())

    async def test_an_approval_that_stays_native_waits_and_stops_typing(self) -> None:
        from discord_blue.doodads.agent_session.protocol import RemoteApprovalRequest

        await self.status("status_changed", "Turn started")
        self.session.hello.capabilities = frozenset({"reply"})
        await self.bridge.interactions.handle_approval_request(
            RemoteApprovalRequest(
                session_id=self.session.session_id,
                session_epoch=self.session.session_epoch,
                approval_id="approval",
                call_id="call",
                turn_id="turn",
                command=["make", "test"],
                cwd="/work",
                reason=None,
            )
        )
        self.assertEqual(self.session.display_state, "waiting")
        self.assertIn("native TUI", self.thread.sent_messages[-1])
        self.assertFalse(await self.typing_continues())

    async def test_stale_epoch_cannot_change_state(self) -> None:
        await self.status("status_changed", "Turn started")
        await self.status("error", "Stale failure", epoch="old-epoch")
        self.assertEqual(self.session.display_state, "working")
        self.assertTrue(await self.typing_continues())

    async def test_terminal_prompt_restarts_typing_after_done(self) -> None:
        await self.status("turn_complete", "Waiting for direction")
        await self.bridge.status.handle_user_message(
            UserMessage(session_id=self.session.session_id, session_epoch=self.session.session_epoch, message="Try again")
        )
        self.assertTrue(await self.typing_continues())

    async def test_disconnect_and_removal_stop_typing(self) -> None:
        await self.status("status_changed", "Turn started")
        self.bridge.set_disconnected_status(self.session)
        self.assertFalse(await self.typing_continues())
        await self.status("status_changed", "Turn started")
        self.bridge.sessions.remove_if_current(self.session)
        self.assertFalse(await self.typing_continues())

    async def test_a_failed_reconnect_restores_typing_for_the_previous_connection(self) -> None:
        from discord_blue.doodads.agent_session.sessions import AgentSession

        await self.status("status_changed", "Turn started")
        replacement = AgentSession(hello=self.session.hello, websocket=self.session.websocket, thread_id=self.session.thread_id)
        self.bridge.sessions.register(replacement)
        self.assertFalse(await self.typing_continues())
        self.bridge.sessions.remove_if_current(replacement)
        self.bridge.restore_replaced(self.session)
        self.assertTrue(await self.typing_continues())

    async def test_a_failed_typing_request_keeps_the_session_and_retries(self) -> None:
        error = discord.HTTPException(cast(ClientResponse, cast(object, SimpleNamespace(status=503, reason="Unavailable"))), "")
        with patch.object(FakeThread, "typing", side_effect=error) as typing:
            await self.status("status_changed", "Turn started")
            await asyncio.sleep(0.05)
        self.assertGreater(typing.await_count, 1)
        self.assertIs(self.bridge.sessions.get(self.session.session_id), self.session)
        self.assertFalse(self.fixture.socket.closed)

    async def test_spawned_launch_brief_is_hidden_but_later_terminal_input_is_shown(self) -> None:
        for harness in ("codex", "claude"):
            self.session.hello.harness = harness
            self.session.hello.spawned = True
            await self.bridge.status.handle_user_message(
                UserMessage(self.session.session_id, self.session.session_epoch, "Automated task brief", LAUNCH_BRIEF_ORIGIN)
            )
            self.assertFalse(any("Automated task brief" in message for message in self.thread.sent_messages))
            self.assertTrue(await self.typing_continues())
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

    async def test_claude_terminal_request_has_one_notice_and_unknown_activity_is_quiet(self) -> None:
        from pathlib import Path
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
        self.assertEqual(self.thread.sent_messages, [waiting_message({"tool_name": "Bash"})])
        self.assertEqual(self.session.display_state, "unknown")
        self.assertFalse(await self.typing_continues())

    async def test_long_and_unpostable_error_notices_keep_the_session(self) -> None:
        from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT

        await self.status("error", "provider error " * DISCORD_MESSAGE_LIMIT)
        self.assertEqual(len(self.thread.sent_messages[-1]), DISCORD_MESSAGE_LIMIT)
        error = discord.HTTPException(cast(ClientResponse, cast(object, SimpleNamespace(status=503, reason="Unavailable"))), "")
        with patch.object(AgentSessionBridge, "post_thread_notice", new=AsyncMock(side_effect=error)):
            await self.status("error", "Another provider failure")
        self.assertEqual(self.session.display_state, "failed")
        self.assertFalse(self.fixture.socket.closed)
        self.assertIs(self.bridge.sessions.get(self.session.session_id), self.session)

    async def test_failure_is_new_and_routine_unknown_status_is_quiet(self) -> None:
        await self.status("status_changed", "Turn started")
        await self.status("status_changed", "Approval status unconfirmed; tools are active")
        self.assertEqual(self.thread.sent_messages, [])
        await self.status("error", "Connection failed")
        self.assertEqual(self.thread.sent_messages, ["Connection failed"])

    async def test_rejected_and_undelivered_replies_do_not_look_queued(self) -> None:
        from tests.fakes_agent_session import FakeWebSocket
        from discord_blue.doodads.agent_session.sessions import PendingRemoteCommand

        await self.status("turn_complete", "Waiting for direction")
        self.session.pending_commands["reply"] = PendingRemoteCommand(self.thread.id, 123, "reply")
        await self.bridge.controls.handle_command_reject(
            {"session_id": self.session.session_id, "command_id": "reply", "reason": "Session busy"}
        )
        self.assertEqual(self.session.display_state, "done")
        self.assertIn("Reply not delivered: Session busy", self.thread.sent_messages)
        self.session.acknowledged = True
        reply = FakeReplyMessage(124, self.thread, "Try again")
        self.thread.add_message(reply)
        with patch.object(FakeWebSocket, "send_json", side_effect=OSError("lost socket")):
            await self.bridge.controls.send_thread_reply(cast(Any, reply))
        self.assertIn("delivery could not be confirmed", reply.replies[0])
        self.assertNotIn(controls_module.REACTION_QUEUED, reply.reactions)
        self.assertEqual(self.session.display_state, "done")
        self.assertFalse(await self.typing_continues())

    async def test_question_stops_typing_and_waits(self) -> None:
        await self.status("status_changed", "Turn started")
        await self.fixture.prompt()
        self.assertEqual(self.session.display_state, "waiting")
        self.assertFalse(await self.typing_continues())

    async def test_rejected_pause_preserves_work_and_a_newer_completion(self) -> None:
        user = cast(Any, SimpleNamespace(id=123))
        await self.bridge.status.handle_user_message(
            UserMessage(session_id=self.session.session_id, session_epoch=self.session.session_epoch, message="Work on this")
        )
        await self.bridge.controls.send_pause_current_turn(self.thread, user)
        command = self.fixture.socket.sent_json[-1]
        await self.bridge.controls.handle_command_reject({**command, "reason": "Pause rejected"})
        self.assertEqual(self.session.display_state, "working")
        self.assertTrue(await self.typing_continues())
        await self.bridge.controls.send_pause_current_turn(self.thread, user)
        command = self.fixture.socket.sent_json[-1]
        await self.status("turn_complete", "Waiting for direction")
        await self.bridge.controls.handle_command_reject({**command, "reason": "No running turn"})
        self.assertEqual(self.session.display_state, "done")
        self.assertFalse(await self.typing_continues())

    async def test_uncorrelated_claude_activity_does_not_claim_working_or_still_waiting(self) -> None:
        await self.status("status_changed", "Claude Code is waiting for approval in the terminal (Bash)")
        await self.status("status_changed", "Approval status unconfirmed; tools are active. Check the Claude Code terminal.")
        self.assertEqual(self.session.display_state, "unknown")
        self.assertFalse(await self.typing_continues())

    async def test_reply_ack_updates_queued_detail_and_preserves_newer_native_status(self) -> None:
        self.session.acknowledged = True
        reply = FakeReplyMessage(123, self.thread, "Work on this")
        self.thread.add_message(reply)
        await self.bridge.controls.send_thread_reply(cast(Any, reply))
        self.assertTrue(await self.typing_continues())
        command = self.fixture.socket.sent_json[-1]
        await self.bridge.controls.handle_command_ack(command)
        self.assertEqual(self.session.last_status_message, "Request delivered to the native session.")
        await self.status("status_changed", "Waiting on a decision in the Codex TUI")
        await self.bridge.controls.handle_command_ack(command)
        self.assertEqual(self.session.display_state, "waiting")

    async def test_replayed_question_waits_without_turn_start(self) -> None:
        await self.fixture.prompt()
        self.assertEqual(self.session.display_state, "waiting")
        self.assertFalse(await self.typing_continues())
