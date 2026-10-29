import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

from aiohttp import web
import discord
from discord.ui.view import ViewStore

from discord_blue.doodads.agent_session.controls import REACTION_APPROVAL_APPROVE, REPLY_BEFORE_RECONNECT
from discord_blue.doodads.agent_session.handover import SessionContinuation
from discord_blue.doodads.agent_session.protocol import RemoteApprovalRequest
from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.doodads.agent_session.session_threads import SessionThread
from tests.fakes_agent_prompts import prompt_fixture
from tests.fakes_agent_session import FakeInteraction, FakeReplyMessage, FakeWebSocket


class SessionHandoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(patch("pathlib.Path.home", return_value=Path(folder)))
        self.fixture = self.enterContext(prompt_fixture())
        self.bridge = self.fixture.bridge
        self.addCleanup(self.bridge.threads.stop)
        self.fixture.session.attached_at = datetime.now(UTC) - timedelta(seconds=30)
        self.bridge.handover.load(self.bridge.handover.export())
        self.bridge.sessions.remove(self.fixture.session.session_id)
        self.replay: set[asyncio.Task[Any]] = set()
        self.enterContext(patch.object(self.bridge.bot, "handover", SimpleNamespace(replay=self.replay), create=True))

    async def reattach(self, *, epoch: str) -> tuple[AgentSession, FakeWebSocket]:
        socket = FakeWebSocket()
        hello = replace(self.fixture.session.hello, session_epoch=epoch)
        session = AgentSession(hello, cast(web.WebSocketResponse, socket))
        self.bridge.sessions.register(session)

        async def attached() -> SessionThread:
            return SessionThread(cast(discord.Thread, self.fixture.thread), None)

        task = asyncio.create_task(attached())
        with patch.object(self.bridge, "attach_task", return_value=task):
            self.assertTrue(await self.bridge.accept_hello(session, None))
        return session, socket

    async def gap_reply(self) -> tuple[asyncio.Task[bool], FakeReplyMessage]:
        message = FakeReplyMessage(
            777,
            self.fixture.thread,
            "inert gap reply",
            created_at=self.fixture.session.attached_at + timedelta(seconds=10),
        )
        self.fixture.thread.add_message(message)
        task = asyncio.create_task(self.bridge.controls.send_thread_reply(cast(discord.Message, message)))
        self.replay.add(task)
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        return task, message

    async def test_gap_reply_waits_for_same_epoch_and_delivers_once_after_ack(self) -> None:
        task, message = await self.gap_reply()
        session, socket = await self.reattach(epoch=self.fixture.session.session_epoch)
        self.assertTrue(await asyncio.wait_for(task, 2))
        commands = [payload for payload in socket.sent_json if payload.get("kind") == "reply"]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["text"], message.content)
        self.assertEqual(commands[0]["session_epoch"], session.session_epoch)
        self.assertEqual(session.attached_at, self.fixture.session.attached_at)

    async def test_gap_reply_is_not_delivered_into_a_replaced_epoch(self) -> None:
        task, message = await self.gap_reply()
        _session, socket = await self.reattach(epoch="replacement-epoch")
        self.assertTrue(await asyncio.wait_for(task, 2))
        self.assertFalse(any(payload.get("kind") == "reply" for payload in socket.sent_json))
        self.assertIn(REPLY_BEFORE_RECONNECT, message.replies)

    def install_view_store(self) -> None:
        store = ViewStore(cast(Any, SimpleNamespace()))
        self.enterContext(
            patch.object(self.bridge.bot, "_connection", SimpleNamespace(_view_store=store, store_view=store.add_view), create=True)
        )

        def stop_views() -> None:
            for view in store.persistent_views:
                view.stop()

        self.addCleanup(stop_views)

    async def test_gap_reaction_waits_for_exact_native_request_and_keeps_original_message(self) -> None:
        self.install_view_store()
        old = self.fixture.session
        self.bridge.sessions.register(old)
        request = RemoteApprovalRequest("approval", "call", "turn", old.session_id, old.session_epoch, ["echo", "inert"], "/", None)
        await self.bridge.interactions.handle_approval_request(request)
        message_id = old.pending_approvals[request.approval_id].message_id
        self.bridge.handover.load(self.bridge.handover.export())
        self.bridge.sessions.remove(old.session_id)
        reaction = asyncio.create_task(
            self.bridge.handle_thread_reaction(
                cast(discord.Thread, self.fixture.thread),
                message_id,
                REACTION_APPROVAL_APPROVE,
                cast(discord.User, FakeInteraction(self.fixture.thread).user),
            )
        )
        self.replay.add(reaction)
        session, socket = await self.reattach(epoch=old.session_epoch)
        await asyncio.sleep(0)
        self.assertFalse(reaction.done())
        self.assertFalse(socket.sent_json[1:])
        await self.bridge.handle_event(session, "approval_request", asdict(request))
        self.assertTrue(await asyncio.wait_for(reaction, 2))
        decisions = [payload for payload in socket.sent_json if payload.get("type") == "approval_decision"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["approval_id"], request.approval_id)
        self.assertEqual(session.pending_approvals[request.approval_id].message_id, message_id)
        self.assertEqual(len(self.fixture.thread.sent_views), 1)

    async def test_reconnect_restores_input_answers_and_custom_ids_without_an_extra_card(self) -> None:
        self.install_view_store()
        self.bridge.sessions.register(self.fixture.session)
        view = await self.fixture.prompt()
        assert view.message_id is not None
        self.bridge.bot._connection.store_view(view, view.message_id)
        previous_ids = SessionContinuation.control_ids(view)
        self.bridge.handover.load(self.bridge.handover.export())
        self.bridge.sessions.remove(self.fixture.session.session_id)
        session, _socket = await self.reattach(epoch=self.fixture.session.session_epoch)
        await self.bridge.handle_event(session, "request_user_input", asdict(view.request))
        message = await self.fixture.thread.fetch_message(view.message_id)
        restored = cast(Any, message.view)
        self.addCleanup(restored.stop)
        self.assertEqual(restored.answers, view.answers)
        self.assertEqual(SessionContinuation.control_ids(restored), previous_ids)
        self.assertEqual(len(self.fixture.thread.sent_views), 1)

    async def test_native_command_ack_after_reconnect_settles_original_reply(self) -> None:
        task, message = await self.gap_reply()
        session, socket = await self.reattach(epoch=self.fixture.session.session_epoch)
        self.assertTrue(await asyncio.wait_for(task, 2))
        command = next(payload for payload in socket.sent_json if payload.get("kind") == "reply")
        self.bridge.handover.load(self.bridge.handover.export())
        self.bridge.handover.restored_sessions.clear()
        self.bridge.sessions.remove(session.session_id)
        restored, _socket = await self.reattach(epoch=session.session_epoch)
        await self.bridge.handle_event(restored, "command_ack", command)
        self.assertEqual(restored.active_command_id, command["command_id"])
        self.assertEqual(message.reactions, [])
