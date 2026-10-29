import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

from aiohttp import web
import discord

from discord_blue.doodads.agent_session.controls import REPLY_BEFORE_RECONNECT
from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.doodads.agent_session.session_threads import SessionThread
from tests.fakes_agent_prompts import prompt_fixture
from tests.fakes_agent_session import FakeReplyMessage, FakeWebSocket


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
