from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch

from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.protocol import RemoteRequestUserInput, RequestUserInputQuestion
from discord_blue.doodads.agent_session.sessions import AgentSession, PendingRemoteApproval
from tests import test_session_cleanup as cleanup_tests
from tests.fakes_agent_session import FakeInteraction, FakeReplyMessage, FakeThread, FakeWebSocket, make_hello


class PromptIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        patcher = patch.object(bridge_module.discord, "Thread", FakeThread)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.thread = FakeThread(555)
        self.bridge = cleanup_tests.SessionCleanupTests.make_bridge(self.thread)
        self.bridge.bot.config.discord.employee_role_name = ""
        self.socket = FakeWebSocket()
        self.session = AgentSession(hello=make_hello(), websocket=cast(Any, self.socket), thread_id=555)
        self.bridge.sessions.register(self.session)

    async def prompt(self, call_id: str = "call-1") -> bridge_module.RequestUserInputView:
        request = RemoteRequestUserInput(
            session_id=self.session.session_id,
            session_epoch=self.session.session_epoch,
            call_id=call_id,
            turn_id="turn-1",
            questions=[
                RequestUserInputQuestion(id="answer", header="Answer", question="Choose", is_other=True, is_secret=False, options=[])
            ],
        )
        await self.bridge.handle_request_user_input(request)
        view = cast(bridge_module.RequestUserInputView, self.thread.sent_views[-1])
        view.set_answer("answer", "yes")
        return view

    async def test_replacement_same_turn_rejects_old_submit_and_cancel(self) -> None:
        old = await self.prompt()
        current = await self.prompt("call-2")
        for action in (old.submit, old.cancel):
            interaction = FakeInteraction(self.thread)
            await action(cast(Any, interaction))
            self.assertEqual(interaction.response.messages, [("This prompt is no longer active.", True)])
            self.assertEqual(interaction.response.edits, [])
        self.assertEqual(self.socket.sent_json, [])
        await current.submit(cast(Any, FakeInteraction(self.thread)))
        self.assertEqual(self.socket.sent_json[0]["call_id"], "call-2")

    async def test_reused_call_id_still_requires_original_message(self) -> None:
        old = await self.prompt()
        await self.prompt()
        await old.submit(cast(Any, FakeInteraction(self.thread)))
        self.assertEqual(self.socket.sent_json, [])
        self.assertEqual(self.session.pending_commands, {})

    async def test_reconnect_rejects_old_epoch_even_when_identifiers_match(self) -> None:
        old = await self.prompt()
        self.session = AgentSession(
            hello=replace(make_hello(), session_epoch="epoch-2"), websocket=cast(Any, self.socket), thread_id=555
        )
        self.bridge.sessions.register(self.session)
        current = await self.prompt()
        old.message_id = current.message_id  # Epoch must independently reject the stale callback.
        await old.submit(cast(Any, FakeInteraction(self.thread)))
        self.assertEqual(self.socket.sent_json, [])
        await current.cancel(cast(Any, FakeInteraction(self.thread)))
        self.assertEqual(self.socket.sent_json[0]["session_epoch"], "epoch-2")

    async def test_wrong_message_channel_or_actor_cannot_answer(self) -> None:
        view = await self.prompt()
        wrong_message = FakeReplyMessage(9999, self.thread)
        interactions = [FakeInteraction(self.thread, message=wrong_message), FakeInteraction(FakeThread(777))]
        for interaction in interactions:
            await view.submit(cast(Any, interaction))
        with patch.object(self.bridge, "is_operator", return_value=False):
            await view.submit(cast(Any, FakeInteraction(self.thread)))
        self.assertEqual(self.socket.sent_json, [])
        self.assertEqual(self.session.pending_commands, {})

    async def test_stale_modal_cannot_restore_retired_view_or_edit_answers(self) -> None:
        old = await self.prompt()
        modal = bridge_module.RequestUserInputAnswerModal(old, old.request.questions[0])
        await self.prompt("call-2")
        interaction = FakeInteraction(self.thread)
        await modal.on_submit(cast(Any, interaction))
        self.assertEqual(old.answers, {"answer": "yes"})
        self.assertEqual(interaction.response.edits, [])
        self.assertTrue(interaction.response.messages)

    async def test_simultaneous_submit_cancel_sends_once(self) -> None:
        view = await self.prompt()
        started, release = asyncio.Event(), asyncio.Event()
        original = self.socket.send_json

        async def send(payload: dict[str, object]) -> None:
            await original(payload)
            started.set()
            await release.wait()

        with patch.object(self.socket, "send_json", new=send):
            first = asyncio.create_task(view.submit(cast(Any, FakeInteraction(self.thread))))
            try:
                await asyncio.wait_for(started.wait(), 1)
                second = FakeInteraction(self.thread)
                await view.cancel(cast(Any, second))
                self.assertTrue(second.response.messages)
                self.assertEqual(len(self.socket.sent_json), 1)
                self.assertEqual(len(self.session.pending_commands), 1)
            finally:
                release.set()
                await first

    async def test_new_prompt_during_send_is_not_repainted_pending(self) -> None:
        view = await self.prompt()
        original = self.socket.send_json

        async def send(payload: dict[str, object]) -> None:
            await original(payload)
            await self.prompt("call-2")

        interaction = FakeInteraction(self.thread)
        with patch.object(self.socket, "send_json", new=send):
            await view.submit(cast(Any, interaction))
        self.assertEqual(interaction.response.edits, [])
        self.assertIn("call-2", self.session.pending_user_inputs)
        self.assertFalse(self.session.pending_user_inputs["call-2"].submitted)

    async def test_approval_button_and_reaction_reserve_one_decision(self) -> None:
        for second_kind in ("button", "reaction"):
            with self.subTest(second_kind=second_kind):
                self.socket.sent_json.clear()
                self.session.pending_approvals["approval"] = PendingRemoteApproval(thread_id=555, message_id=901)
                self.thread.add_message(FakeReplyMessage(901, self.thread))
                started, release = asyncio.Event(), asyncio.Event()
                original = self.socket.send_json

                async def send(
                    payload: dict[str, object],
                    original: Callable[[dict[str, object]], Awaitable[None]] = original,
                    started: asyncio.Event = started,
                    release: asyncio.Event = release,
                ) -> None:
                    await original(payload)
                    started.set()
                    await release.wait()

                first_interaction = FakeInteraction(self.thread, message=FakeReplyMessage(901, self.thread))
                with patch.object(self.socket, "send_json", new=send):
                    first = asyncio.create_task(
                        self.bridge.handle_approval_interaction(
                            cast(Any, first_interaction),
                            self.session.session_id,
                            "approval",
                            "approved",
                            session_epoch=self.session.session_epoch,
                        )
                    )
                    try:
                        await asyncio.wait_for(started.wait(), 1)
                        second = FakeInteraction(self.thread, user_id=456, message=FakeReplyMessage(901, self.thread))
                        if second_kind == "button":
                            await self.bridge.handle_approval_interaction(
                                cast(Any, second),
                                self.session.session_id,
                                "approval",
                                "denied",
                                session_epoch=self.session.session_epoch,
                            )
                        else:
                            await self.bridge.handle_approval_reaction(
                                self.session, cast(Any, self.thread), "approval", "✖️", cast(Any, second.user)
                            )
                        self.assertEqual(len(self.socket.sent_json), 1)
                        self.assertEqual(self.session.pending_approvals["approval"].decided_by, 123)
                    finally:
                        release.set()
                        await first
