from __future__ import annotations

import asyncio
import ast
import inspect
import textwrap
import unittest
from dataclasses import replace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from aiohttp import WSMsgType

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.protocol import REMOTE_ACTIONS, RemoteApprovalRequest, RemoteRequestUserInput, SessionHello
from discord_blue.doodads.agent_session.sessions import PendingRemoteApproval
from tests import test_prompt_identity as identity_tests
from tests import test_session_cleanup_transport as transport_tests
from tests.fakes_agent_session import FakeInteraction, FakeReplyMessage


class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = identity_tests.PromptIdentityTests()
        await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bridge, self.session = self.fixture.bridge, self.fixture.session
        self.thread, self.socket = self.fixture.thread, self.fixture.socket
        self.user = cast(Any, FakeInteraction(self.thread).user)

    def restrict(self, *actions: str) -> None:
        self.session.hello = replace(self.session.hello, capabilities=frozenset(actions))

    def test_parse_legacy_empty_unknown_and_bounded_capabilities(self) -> None:
        payload: dict[str, object] = {"session_id": "session", "session_epoch": "epoch"}
        self.assertIsNone(SessionHello.from_payload(payload).capabilities)
        self.assertTrue(all(SessionHello.from_payload(payload).supports(action) for action in REMOTE_ACTIONS))
        self.assertFalse(SessionHello.from_payload(payload).supports("unknown"))
        parsed = SessionHello.from_payload({**payload, "capabilities": ["reply", "reply", "future-action"]})
        self.assertEqual(parsed.capabilities, frozenset({"reply"}))
        self.assertEqual(SessionHello.from_payload({**payload, "capabilities": []}).capabilities, frozenset())
        invalid_values: tuple[object, ...] = (None, "reply", {}, [1], [False], [None], ["reply"] * 33, ["x" * 65])
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                SessionHello.from_payload({**payload, "capabilities": invalid})

    async def test_empty_capabilities_gate_every_command_without_queue_or_reactions(self) -> None:
        self.restrict()
        for send in (
            self.bridge.send_continue_autonomously,
            self.bridge.send_pause_current_turn,
            self.bridge.send_new_session,
            self.bridge.send_end_session,
        ):
            self.assertIn("does not support", await send(self.thread, self.user))
        message = FakeReplyMessage(2000, self.thread, "please continue")
        self.thread.add_message(message)
        self.assertTrue(await self.bridge.send_thread_reply(cast(Any, message)))
        self.assertIn("does not support", message.replies[0])
        self.assertEqual(message.reactions, [])
        self.assertIsNone(self.session.control_message_id)
        self.assertEqual(self.socket.sent_json, [])
        self.assertEqual(self.session.pending_commands, {})
        self.assertIn("online", self.bridge.session_status_summary(self.thread, self.user))

    async def test_stale_input_and_approval_controls_cannot_bypass_empty_capabilities(self) -> None:
        view = await self.fixture.prompt()
        self.session.pending_approvals["approval"] = PendingRemoteApproval(thread_id=555, message_id=950)
        approval_message = FakeReplyMessage(950, self.thread)
        self.thread.add_message(approval_message)
        self.restrict()
        interaction = FakeInteraction(self.thread)
        await view.submit(cast(Any, interaction))
        self.assertIn("does not support", interaction.response.messages[0][0])
        self.assertFalse(self.session.pending_user_inputs["call-1"].submitted)
        button = FakeInteraction(self.thread, message=approval_message)
        await self.bridge.handle_approval_interaction(
            cast(Any, button), self.session.session_id, "approval", "approved", session_epoch=self.session.session_epoch
        )
        await self.bridge.handle_approval_reaction(self.session, cast(Any, self.thread), "approval", "✅", self.user)
        self.assertIsNone(self.session.pending_approvals["approval"].decision)
        self.assertEqual(self.socket.sent_json, [])
        self.assertEqual(self.session.pending_commands, {})

    async def test_unsupported_inbound_requests_do_not_render_prompts(self) -> None:
        self.restrict()
        await self.bridge.handle_approval_request(
            RemoteApprovalRequest(
                session_id=self.session.session_id,
                session_epoch=self.session.session_epoch,
                approval_id="approval",
                call_id="call",
                turn_id="turn",
                command=["private-command"],
                cwd="/secret",
                reason=None,
            )
        )
        await self.bridge.handle_request_user_input(
            RemoteRequestUserInput(
                session_id=self.session.session_id,
                session_epoch=self.session.session_epoch,
                call_id="input",
                turn_id="turn",
                questions=[],
            )
        )
        self.assertEqual(self.session.pending_user_inputs, {})
        self.assertEqual(self.session.pending_approvals, {})
        self.assertTrue(all(view is None for view in self.thread.sent_views))
        self.assertNotIn("private-command", " ".join(self.thread.sent_messages))

    async def test_end_confirmation_is_unreachable_and_stale_confirm_is_gated(self) -> None:
        self.restrict()
        control = FakeReplyMessage(950, self.thread)
        self.thread.add_message(control)
        self.session.control_message_id = control.id
        await self.bridge.handle_session_control_reaction(self.session, cast(Any, self.thread), control.id, "⏹️", self.user)
        self.assertIsNone(self.session.pending_control_confirmation)
        self.session.pending_control_confirmation = "end_session"
        await self.bridge.handle_pending_control_confirmation(self.session, cast(Any, self.thread), control.id, "✅", self.user)
        self.assertEqual(self.socket.sent_json, [])
        self.assertEqual(self.session.pending_commands, {})
        self.assertNotIn("⏳", control.reactions)

    def test_capabilities_filter_actions_preserve_local_status_and_status_indicator(self) -> None:
        self.assertEqual(self.bridge.session_control_reactions(self.session), ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"])
        self.restrict("reply", "status_request", "pause_current_turn", "end_session")
        self.assertEqual(self.bridge.session_control_reactions(self.session), [bridge_module.REACTION_CONTROL_STATUS, "⏹️"])
        self.session.control_status_reaction = "🔄"
        self.session.control_interruptions_enabled = True
        self.assertEqual(self.bridge.session_control_reactions(self.session), ["🔄", "⏸️", "⏹️"])
        self.restrict()
        self.assertEqual(self.bridge.session_control_reactions(self.session), ["🔄"])
        self.session.control_status_reaction = None
        self.assertEqual(self.bridge.session_control_reactions(self.session), [bridge_module.REACTION_CONTROL_STATUS])

    async def test_input_send_failure_clears_reservation_and_reports_delivery_uncertainty(self) -> None:
        view = await self.fixture.prompt()
        interaction = FakeInteraction(self.thread)
        with patch.object(self.socket, "send_json", new=AsyncMock(side_effect=ConnectionResetError)):
            await view.submit(cast(Any, interaction))
        self.assertFalse(self.session.pending_user_inputs["call-1"].submitted)
        self.assertEqual(self.session.pending_commands, {})
        self.assertIn("could not be confirmed", interaction.response.messages[0][0])

    async def test_approval_send_failure_clears_reservation(self) -> None:
        pending = PendingRemoteApproval(thread_id=555, message_id=950)
        self.session.pending_approvals["approval"] = pending
        interaction = FakeInteraction(self.thread, message=FakeReplyMessage(950, self.thread))
        with patch.object(self.socket, "send_json", new=AsyncMock(side_effect=ConnectionResetError)):
            await self.bridge.handle_approval_interaction(
                cast(Any, interaction), self.session.session_id, "approval", "approved", session_epoch=self.session.session_epoch
            )
        self.assertIsNone(pending.decision)
        self.assertIsNone(pending.decided_by)
        self.assertIn("could not be confirmed", interaction.response.messages[0][0])

    async def test_approval_button_rejects_wrong_epoch_or_message(self) -> None:
        self.session.pending_approvals["approval"] = PendingRemoteApproval(thread_id=555, message_id=950)
        for epoch, message_id in (("old", 950), (self.session.session_epoch, 951)):
            interaction = FakeInteraction(self.thread, message=FakeReplyMessage(message_id, self.thread))
            await self.bridge.handle_approval_interaction(
                cast(Any, interaction), self.session.session_id, "approval", "approved", session_epoch=epoch
            )
            self.assertEqual(interaction.response.messages, [("This approval is no longer active.", True)])
        self.assertEqual(self.socket.sent_json, [])

    def test_outbound_wire_sends_have_one_command_and_one_approval_dispatch_gate(self) -> None:
        tree = ast.parse(textwrap.dedent(inspect.getsource(bridge_module.AgentSessionBridge)))
        senders = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and any(
                isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "send_json"
                for child in ast.walk(node)
            )
        }
        self.assertEqual(senders, {"handle_connect", "dispatch_command", "dispatch_approval"})


class CapabilityTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_hello_closes_before_thread_lookup(self) -> None:
        for invalid in (None, "reply", [1], ["reply"] * 33, ["x" * 65]):
            with self.subTest(invalid=invalid):
                async with transport_tests.CleanupTransportTests().transport() as (bridge, _thread, client, finished):
                    websocket = await client.ws_connect(
                        bridge_module.AGENT_SESSION_CONNECT_PATH, headers={"Authorization": "Bearer cleanup-transport-test"}
                    )
                    await websocket.send_json({**transport_tests.CleanupTransportTests.hello(), "capabilities": invalid})
                    self.assertEqual((await websocket.receive(timeout=2)).type, WSMsgType.CLOSE)
                    await websocket.close()
                    await asyncio.wait_for(finished.wait(), 2)
                    cast(AsyncMock, bridge.find_or_create_session_thread).assert_not_awaited()
                    self.assertEqual(bridge.sessions.by_session, {})

    async def test_reconnect_narrows_controls_and_rejects_prior_session_dispatch(self) -> None:
        async with transport_tests.CleanupTransportTests().transport() as (bridge, thread, client, _finished):
            bridge.bot.config.discord.employee_role_name = ""
            old = await client.ws_connect(
                bridge_module.AGENT_SESSION_CONNECT_PATH, headers={"Authorization": "Bearer cleanup-transport-test"}
            )
            hello = transport_tests.CleanupTransportTests.hello()
            await old.send_json(hello)
            await old.receive_json(timeout=2)
            prior = bridge.sessions.get("cleanup-session")
            assert prior is not None
            await bridge.post_session_controls(prior)
            self.assertIn("▶️", bridge.session_control_reactions(prior))
            current = await client.ws_connect(
                bridge_module.AGENT_SESSION_CONNECT_PATH, headers={"Authorization": "Bearer cleanup-transport-test"}
            )
            await current.send_json({**hello, "capabilities": []})
            self.assertEqual((await current.receive_json(timeout=2))["capabilities"], [])
            attached = bridge.sessions.get("cleanup-session")
            assert attached is not None
            await bridge.post_session_controls(attached)
            self.assertEqual(bridge.session_control_reactions(attached), [bridge_module.REACTION_CONTROL_STATUS])
            self.assertIn("offline", cast(str, bridge.dispatch_error(prior, "reply")))
            response = await bridge.send_continue_autonomously(thread, cast(Any, FakeInteraction(thread).user))
            self.assertIn("does not support", response)
            self.assertEqual(attached.pending_commands, {})
            await old.close()
            await current.close()

    async def test_ack_echoes_only_negotiated_capabilities(self) -> None:
        async with transport_tests.CleanupTransportTests().transport() as (bridge, _thread, client, _finished):
            websocket = await client.ws_connect(
                bridge_module.AGENT_SESSION_CONNECT_PATH, headers={"Authorization": "Bearer cleanup-transport-test"}
            )
            await websocket.send_json(
                {**transport_tests.CleanupTransportTests.hello(), "capabilities": ["reply", "unknown", "reply"]}
            )
            self.assertEqual(
                await websocket.receive_json(timeout=2), {"type": "hello_ack", "thread_id": 555, "capabilities": ["reply"]}
            )
            self.assertEqual(bridge.sessions.get("cleanup-session").hello.capabilities, frozenset({"reply"}))  # type: ignore[union-attr]
            await websocket.close()
