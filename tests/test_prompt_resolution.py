from __future__ import annotations

import asyncio
import unittest
from typing import Any, cast
from unittest.mock import patch

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.protocol import RemoteApprovalRequest
from tests.fakes_agent_prompts import prompt_fixture
from tests import test_session_cleanup_transport as transport_tests
from tests.fakes_agent_session import FakeInteraction


class PromptResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = self.enterContext(prompt_fixture())
        self.bridge, self.session = self.fixture.bridge, self.fixture.session
        self.thread, self.socket = self.fixture.thread, self.fixture.socket

    def event(self, kind: str, **fields: object) -> dict[str, object]:
        return {"type": kind, "session_id": self.session.session_id, "session_epoch": self.session.session_epoch, **fields}

    async def approval(self) -> None:
        await self.bridge.handle_approval_request(
            RemoteApprovalRequest(
                session_id=self.session.session_id,
                session_epoch=self.session.session_epoch,
                approval_id="approval",
                call_id="call",
                turn_id="turn",
                command=["test"],
                cwd="/test",
                reason=None,
            )
        )

    async def test_external_resolution_neutral_and_duplicate_unknown_events_are_noops(self) -> None:
        view = await self.fixture.prompt()
        await self.approval()
        input_message = await self.thread.fetch_message(cast(int, view.message_id))
        approval_message = await self.thread.fetch_message(self.session.pending_approvals["approval"].message_id)
        for kind, fields in (
            ("approval_resolved", {"approval_id": "approval"}),
            ("request_user_input_resolved", {"call_id": "call-1", "turn_id": "turn-1"}),
        ):
            await self.bridge.handle_prompt_resolved(kind, self.event(kind, **fields))
            edits = len(input_message.edits) + len(approval_message.edits)
            await self.bridge.handle_prompt_resolved(kind, self.event(kind, **fields))
            self.assertEqual(len(input_message.edits) + len(approval_message.edits), edits)
        for message in (input_message, approval_message):
            self.assertEqual(message.content, "**Resolved**")
            self.assertIsNone(message.edit_kwargs[-1]["view"])
            self.assertEqual(message.reactions, [])
        self.assertEqual(self.socket.sent_json, [])
        self.assertEqual(self.session.pending_user_inputs, {})
        self.assertEqual(self.session.pending_approvals, {})

    async def test_wrong_identity_and_stale_epoch_preserve_current_prompts(self) -> None:
        await self.fixture.prompt()
        await self.approval()
        for kind, fields in (
            ("approval_resolved", {"approval_id": "approval"}),
            ("request_user_input_resolved", {"call_id": "call-1", "turn_id": "turn-1"}),
        ):
            for changed in (
                {"session_epoch": "stale"},
                {"session_id": "wrong"},
                {"approval_id": "wrong", "call_id": "wrong"},
                {"approval_id": 4, "call_id": 4},
            ):
                await self.bridge.handle_prompt_resolved(kind, {**self.event(kind, **fields), **changed})
        await self.bridge.handle_prompt_resolved(
            "request_user_input_resolved", self.event("request_user_input_resolved", call_id="call-1", turn_id="wrong")
        )
        self.assertIn("call-1", self.session.pending_user_inputs)
        self.assertIn("approval", self.session.pending_approvals)

    async def test_resolution_during_approval_send_wins_over_pending_edit_and_late_ack(self) -> None:
        await self.approval()
        message = await self.thread.fetch_message(self.session.pending_approvals["approval"].message_id)
        original = self.socket.send_json

        async def send(payload: dict[str, object]) -> None:
            await original(payload)
            await self.bridge.handle_prompt_resolved("approval_resolved", self.event("approval_resolved", approval_id="approval"))

        interaction = FakeInteraction(self.thread, message=message)
        with patch.object(self.socket, "send_json", new=send):
            await self.bridge.handle_approval_interaction(
                cast(Any, interaction), self.session.session_id, "approval", "approved", session_epoch=self.session.session_epoch
            )
        await self.bridge.handle_approval_decision_ack(self.event("approval_decision_ack", approval_id="approval"))
        self.assertEqual(message.content, "**Resolved**")
        self.assertEqual(interaction.response.edits, [])
        self.assertIn("Decision sent", interaction.response.messages[0][0])

    async def test_resolution_during_input_send_removes_pending_command_and_late_ack_reject_cannot_touch_replacement(self) -> None:
        view = await self.fixture.prompt()
        old_message = await self.thread.fetch_message(cast(int, view.message_id))
        original = self.socket.send_json

        async def send(payload: dict[str, object]) -> None:
            await original(payload)
            await self.bridge.handle_prompt_resolved(
                "request_user_input_resolved", self.event("request_user_input_resolved", call_id="call-1", turn_id="turn-1")
            )

        interaction = FakeInteraction(self.thread)
        with patch.object(self.socket, "send_json", new=send):
            await view.submit(cast(Any, interaction))
        self.assertEqual(old_message.content, "**Resolved**")
        self.assertEqual(interaction.response.edits, [])
        self.assertEqual(self.session.pending_commands, {})
        newer = await self.fixture.prompt("call-2")
        new_message = await self.thread.fetch_message(cast(int, newer.message_id))
        for handler in (self.bridge.handle_command_ack, self.bridge.handle_command_reject):
            await handler(self.event("command_ack", command_id=self.socket.sent_json[0]["command_id"]))
        self.assertEqual(new_message.reactions, [])
        self.assertEqual(new_message.edits, [])
        self.assertIn("call-2", self.session.pending_user_inputs)

    async def test_resolution_waits_for_inflight_modal_edit_then_removes_its_view(self) -> None:
        view = await self.fixture.prompt()
        message = await self.thread.fetch_message(cast(int, view.message_id))
        interaction = FakeInteraction(self.thread, message=message)
        editing, release = asyncio.Event(), asyncio.Event()

        async def edit(content: str, **kwargs: object) -> None:
            editing.set()
            await release.wait()
            await message.edit(content=content, **kwargs)

        with patch.object(interaction.response, "edit_message", new=edit):
            edit_task = asyncio.create_task(view.edit_answer(cast(Any, interaction), "answer", "new answer"))
            await asyncio.wait_for(editing.wait(), 1)
            pending = self.session.pending_user_inputs["call-1"]
            resolve = asyncio.create_task(
                self.bridge.handle_prompt_resolved(
                    "request_user_input_resolved", self.event("request_user_input_resolved", call_id="call-1", turn_id="turn-1")
                )
            )
            try:
                await asyncio.sleep(0)
                self.assertTrue(pending.retired)
                self.assertFalse(resolve.done())
            finally:
                release.set()
                await asyncio.gather(edit_task, resolve)
        self.assertEqual(message.content, "**Resolved**")
        self.assertIsNone(message.edit_kwargs[-1]["view"])
        stale = FakeInteraction(self.thread, message=message)
        await view.edit_answer(cast(Any, stale), "answer", "stale")
        self.assertEqual(stale.response.edits, [])
        self.assertEqual(view.answers["answer"], "new answer")

    async def test_resolution_waits_for_inflight_approval_pending_edit(self) -> None:
        await self.approval()
        pending = self.session.pending_approvals["approval"]
        message = await self.thread.fetch_message(pending.message_id)
        editing, release = asyncio.Event(), asyncio.Event()
        original = message.edit

        async def edit(content: str, **kwargs: object) -> None:
            if "Approval sent" in content:
                editing.set()
                await release.wait()
            await original(content=content, **kwargs)

        with patch.object(message, "edit", new=edit):
            click = asyncio.create_task(
                self.bridge.handle_approval_reaction(
                    self.session, cast(Any, self.thread), "approval", "✅", cast(Any, FakeInteraction(self.thread).user)
                )
            )
            await asyncio.wait_for(editing.wait(), 1)
            resolve = asyncio.create_task(
                self.bridge.handle_prompt_resolved("approval_resolved", self.event("approval_resolved", approval_id="approval"))
            )
            try:
                await asyncio.sleep(0)
                self.assertTrue(pending.retired)
                self.assertFalse(resolve.done())
            finally:
                release.set()
                await asyncio.gather(click, resolve)
        self.assertEqual(message.content, "**Resolved**")
        self.assertEqual(message.reactions, [])

    def test_ack_copy_describes_submission_without_asserting_winner(self) -> None:
        self.assertEqual(self.bridge.format_approval_finished("approved", 123), "**Submitted: approved**\nby: `123`")
        self.assertEqual(self.bridge.format_approval_finished(None, None), "**Decision acknowledged**")


class ResolutionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_serial_wire_request_then_resolution_and_stale_events(self) -> None:
        async with transport_tests.CleanupTransportTests().transport() as (bridge, thread, client, _finished):
            websocket = await client.ws_connect(
                bridge_module.AGENT_SESSION_CONNECT_PATH, headers={"Authorization": "Bearer cleanup-transport-test"}
            )
            identity = transport_tests.CleanupTransportTests.hello()
            await websocket.send_json(identity)
            await websocket.receive_json(timeout=2)
            done = asyncio.Event()
            original = bridge.handle_prompt_resolved

            async def resolved(kind: str, payload: dict[str, object]) -> None:
                await original(kind, payload)
                if payload.get("call_id") == "input":
                    done.set()

            with patch.object(bridge, "handle_prompt_resolved", new=resolved):
                await websocket.send_json(
                    {
                        **identity,
                        "type": "approval_request",
                        "approval_id": "approval",
                        "call_id": "call",
                        "turn_id": "turn",
                        "command": ["test"],
                    }
                )
                await websocket.send_json(
                    {**identity, "type": "approval_resolved", "approval_id": "approval", "session_epoch": "stale"}
                )
                await websocket.send_json({**identity, "type": "approval_resolved", "approval_id": "approval"})
                await websocket.send_json(
                    {**identity, "type": "request_user_input", "call_id": "input", "turn_id": "turn", "questions": []}
                )
                await websocket.send_json({**identity, "type": "request_user_input_resolved", "call_id": "input", "turn_id": "turn"})
                await asyncio.wait_for(done.wait(), 2)
            session = bridge.sessions.get("cleanup-session")
            assert session is not None
            self.assertEqual(session.pending_approvals, {})
            self.assertEqual(session.pending_user_inputs, {})
            messages = [await thread.fetch_message(901), await thread.fetch_message(902)]
            self.assertTrue(all(message.content == "**Resolved**" for message in messages))
            await websocket.close()
