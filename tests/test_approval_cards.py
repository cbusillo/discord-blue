from __future__ import annotations

import asyncio
import dataclasses
import unittest
from typing import Any, cast
from unittest.mock import patch

import discord

from discord_blue.doodads.agent_session.bridge import AgentSessionBridge, ApprovalButton, ApprovalView
from discord_blue.doodads.agent_session.protocol import RemoteApprovalDecision, RemoteApprovalRequest
from tests.fakes_agent_prompts import prompt_fixture
from tests.fakes_agent_session import FakeInteraction, FakeReplyMessage, FakeThread, visible_view_text


class ApprovalCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = self.enterContext(prompt_fixture())
        self.request = RemoteApprovalRequest(
            approval_id="approve-1",
            call_id="call-1",
            turn_id="turn-1",
            session_id=self.fixture.session.session_id,
            session_epoch=self.fixture.session.session_epoch,
            command=["git", "status"],
            command_text='printf "%s" "hello world"',
            cwd="/project",
            reason="Inspect the change",
        )

    async def offer(self) -> tuple[ApprovalView, FakeReplyMessage]:
        await self.fixture.bridge.handle_approval_request(self.request)
        pending = self.fixture.session.pending_approvals[self.request.approval_id]
        message = await self.fixture.thread.fetch_message(pending.message_id)
        assert isinstance(message.view, ApprovalView)
        return message.view, message

    @staticmethod
    def button(view: ApprovalView, decision: str) -> ApprovalButton:
        return next(item for item in view.walk_children() if isinstance(item, ApprovalButton) and item.decision == decision)

    async def test_buttons_send_bound_decisions_and_retire_v2_controls(self) -> None:
        for decision in ("approved", "denied"):
            with self.subTest(decision=decision):
                self.fixture.socket.sent_json.clear()
                view, message = await self.offer()
                self.assertIn(self.request.command_text, visible_view_text(view))
                interaction = FakeInteraction(self.fixture.thread, message=message)
                await self.button(view, decision).callback(cast(Any, interaction))
                [sent] = self.fixture.socket.sent_json
                self.assertEqual(
                    sent,
                    RemoteApprovalDecision(
                        approval_id=self.request.approval_id,
                        session_id=self.request.session_id,
                        session_epoch=self.request.session_epoch,
                        decision=decision,
                    ).to_message(),
                )
                self.assertTrue(interaction.response.deferred)
                self.assertTrue(view.is_finished())
                self.assertEqual(interaction.response.edits[0][0], "")
                replacement = cast(discord.ui.LayoutView, interaction.response.edit_kwargs[0]["view"])
                self.assertFalse(any(isinstance(item, discord.ui.Button) for item in replacement.walk_children()))
                self.assertTrue(visible_view_text(replacement))
                self.assertEqual(self.fixture.session.pending_approvals[self.request.approval_id].decision, decision)
                self.assertEqual(message.reactions, [])
                await self.fixture.bridge.handle_approval_decision_ack(sent)
                self.assertTrue(visible_view_text(message.view))
                self.assertFalse(
                    any(isinstance(item, discord.ui.Button) for item in cast(discord.ui.LayoutView, message.view).walk_children())
                )
                # A queued second click never sends another decision after acknowledgement.
                await self.button(view, decision).callback(cast(Any, FakeInteraction(self.fixture.thread, message=message)))
                self.assertEqual(len(self.fixture.socket.sent_json), 1)

    async def test_actual_buttons_preserve_actor_message_channel_epoch_and_offline_gates(self) -> None:
        view, message = await self.offer()
        button = self.button(view, "approved")
        for interaction in (
            FakeInteraction(self.fixture.thread, message=FakeReplyMessage(12345, self.fixture.thread)),
            FakeInteraction(FakeThread(999), message=message),
        ):
            await button.callback(cast(Any, interaction))
            self.assertTrue(interaction.response.messages)
        with patch.object(AgentSessionBridge, "is_operator", return_value=False):
            await button.callback(cast(Any, FakeInteraction(self.fixture.thread, message=message)))
        self.fixture.session.hello.session_epoch = "new-epoch"
        await button.callback(cast(Any, FakeInteraction(self.fixture.thread, message=message)))
        self.fixture.session.hello.session_epoch = self.request.session_epoch
        self.fixture.socket.closed = True
        await button.callback(cast(Any, FakeInteraction(self.fixture.thread, message=message)))
        self.assertEqual(self.fixture.socket.sent_json, [])

    async def test_full_file_and_permission_context_survives_card_and_native_resolution(self) -> None:
        for kind in ("file_change", "permissions"):
            with self.subTest(kind=kind):
                context = '{"scope": "this turn", "changes": ["full diff"]}'
                self.request = dataclasses.replace(self.request, approval_kind=kind, content_text=context, command_text=None)
                view, message = await self.offer()
                self.assertIn(context, visible_view_text(view))
                await self.fixture.bridge.handle_prompt_resolved(
                    "approval_resolved",
                    {
                        "session_id": self.request.session_id,
                        "session_epoch": self.request.session_epoch,
                        "approval_id": self.request.approval_id,
                    },
                )
                self.assertFalse(
                    any(isinstance(item, discord.ui.Button) for item in cast(discord.ui.LayoutView, message.view).walk_children())
                )
                await self.button(view, "approved").callback(cast(Any, FakeInteraction(self.fixture.thread, message=message)))
                self.assertEqual(self.fixture.socket.sent_json, [])

    async def test_slow_delivery_is_acknowledged_before_native_resolution_waits(self) -> None:
        view, message = await self.offer()
        interaction = FakeInteraction(self.fixture.thread, message=message)
        sending, release = asyncio.Event(), asyncio.Event()
        original = self.fixture.socket.send_json

        async def send(payload: dict[str, object]) -> None:
            sending.set()
            await release.wait()
            await original(payload)

        with patch.object(self.fixture.socket, "send_json", new=send):
            click = asyncio.create_task(self.button(view, "approved").callback(cast(Any, interaction)))
            try:
                await asyncio.wait_for(sending.wait(), 1)
                self.assertTrue(interaction.response.deferred)
                await self.fixture.bridge.handle_prompt_resolved(
                    "approval_resolved",
                    {
                        "session_id": self.request.session_id,
                        "session_epoch": self.request.session_epoch,
                        "approval_id": self.request.approval_id,
                    },
                )
                self.assertTrue(view.is_finished())
            finally:
                release.set()
                await click
        self.assertTrue(interaction.response.messages)
        self.assertEqual(interaction.response.edits, [])
        self.assertEqual(len(self.fixture.socket.sent_json), 1)

    async def test_replaced_requests_and_removed_sessions_release_their_button_views(self) -> None:
        old, _ = await self.offer()
        current, _ = await self.offer()
        self.assertTrue(old.is_finished())
        self.assertFalse(current.is_finished())
        self.fixture.bridge.sessions.remove(self.fixture.session.session_id)
        self.assertTrue(current.is_finished())

    async def test_native_resolution_during_defer_prevents_dispatch(self) -> None:
        view, message = await self.offer()
        interaction = FakeInteraction(self.fixture.thread, message=message)
        original = interaction.response.defer

        async def defer() -> None:
            await original()
            await self.fixture.bridge.handle_prompt_resolved(
                "approval_resolved",
                {
                    "session_id": self.request.session_id,
                    "session_epoch": self.request.session_epoch,
                    "approval_id": self.request.approval_id,
                },
            )

        with patch.object(interaction.response, "defer", new=defer):
            await self.button(view, "approved").callback(cast(Any, interaction))
        self.assertEqual(self.fixture.socket.sent_json, [])
        self.assertTrue(interaction.response.messages)
        self.assertTrue(view.is_finished())

    async def test_failed_replacement_restores_answerable_approval_buttons(self) -> None:
        view, message = await self.offer()
        previous = self.fixture.session
        replacement = dataclasses.replace(
            previous, hello=dataclasses.replace(previous.hello, session_epoch="failed-epoch"), pending_approvals={}
        )
        self.fixture.bridge.sessions.register(replacement)
        self.assertFalse(view.is_finished())
        self.fixture.bridge.sessions.remove_if_current(replacement)
        self.fixture.bridge.restore_replaced(previous)
        self.assertIs(self.fixture.bridge.sessions.get(previous.session_id), previous)
        self.assertFalse(view.is_finished())
        await self.button(view, "approved").callback(cast(Any, FakeInteraction(self.fixture.thread, message=message)))
        self.assertEqual(len(self.fixture.socket.sent_json), 1)
        self.assertTrue(view.is_finished())
