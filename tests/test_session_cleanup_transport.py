from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from discord_blue.config import AgentSessionConfig, Config, DiscordConfig
from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.plugs.discord_plug import BlueBot
from tests.fakes_agent_session import FakeBot, FakeThread


class ProductionCleanupTestServer(TestServer):
    async def _make_runner(self, **_kwargs: object) -> web.AppRunner:
        # Match production: a disconnected client does not cancel its handler.
        return web.AppRunner(self.app, handler_cancellation=False)


class CleanupTransportTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def transport(self) -> AsyncIterator[tuple[AgentSessionBridge, FakeThread, TestClient, asyncio.Event]]:
        config = cast(Config, SimpleNamespace(agent_session=AgentSessionConfig(), discord=DiscordConfig()))
        config.agent_session.token = "cleanup-transport-test"
        thread = FakeThread(555)
        bridge = AgentSessionBridge(cast(BlueBot, FakeBot(config, thread)))
        app = web.Application()
        # Signal handler completion regardless of whether its exception propagates
        # to aiohttp; asserting registry state then catches missing finally cleanup.
        finished = asyncio.Event()

        async def connect(request: web.Request) -> web.WebSocketResponse:
            try:
                return await bridge.handle_connect(request)
            finally:
                finished.set()

        app.router.add_get(bridge_module.AGENT_SESSION_CONNECT_PATH, connect)
        attachment = bridge_module.SessionThread(thread=cast(Any, thread), notification_message_id=None)
        with (
            patch.object(bridge_module.discord, "Thread", FakeThread),
            patch.object(bridge, "find_or_create_session_thread", new=AsyncMock(return_value=attachment)),
        ):
            async with TestClient(ProductionCleanupTestServer(app)) as client:
                yield bridge, thread, client, finished

    @staticmethod
    def hello() -> dict[str, str]:
        return {"type": "hello", "session_id": "cleanup-session", "session_epoch": "epoch-1", "cwd": "/test/project"}

    async def test_late_hello_ack_failure_unregisters_and_archives(self) -> None:
        async with self.transport() as (bridge, thread, client, finished):

            async def failed_ack(_websocket: web.WebSocketResponse, _payload: object, **_kwargs: object) -> None:
                # Fail only at the server acknowledgement after actual registration
                # and thread binding, as observed during the slow live handshake.
                self.assertIsNotNone(bridge.sessions.get("cleanup-session"))
                raise ConnectionResetError("client disconnected before hello acknowledgement")

            with patch.object(web.WebSocketResponse, "send_json", new=failed_ack):
                websocket = await client.ws_connect(
                    bridge_module.AGENT_SESSION_CONNECT_PATH,
                    headers={"Authorization": "Bearer cleanup-transport-test"},
                )
                await websocket.send_json(self.hello())
                await websocket.receive(timeout=2)
                await websocket.close()
                await asyncio.wait_for(finished.wait(), timeout=2)
                self.assertIsNone(bridge.sessions.get("cleanup-session"))
                self.assertEqual(bridge.sessions.by_thread, {})
                self.assertTrue(thread.archived)
                self.assertTrue(thread.locked)
                self.assertTrue(thread.left)
                await websocket.close()

    async def test_event_handler_failure_unregisters_and_archives(self) -> None:
        async with self.transport() as (bridge, thread, client, finished):
            websocket = await client.ws_connect(
                bridge_module.AGENT_SESSION_CONNECT_PATH,
                headers={"Authorization": "Bearer cleanup-transport-test"},
            )
            await websocket.send_json(self.hello())
            self.assertEqual((await websocket.receive_json(timeout=2))["thread_id"], thread.id)
            session = bridge.sessions.get("cleanup-session")
            self.assertIsInstance(session, AgentSession)
            with patch.object(bridge, "handle_command_ack", new=AsyncMock(side_effect=RuntimeError("handler failed"))):
                await websocket.send_json({**self.hello(), "type": "command_ack", "command_id": "failure"})
                await websocket.receive(timeout=2)
                await websocket.close()
                await asyncio.wait_for(finished.wait(), timeout=2)
            self.assertIsNone(bridge.sessions.get("cleanup-session"))
            self.assertEqual(bridge.sessions.by_thread, {})
            self.assertTrue(thread.archived)
            self.assertTrue(thread.left)
            await websocket.close()

    async def test_non_object_json_does_not_abandon_live_session(self) -> None:
        async with self.transport() as (bridge, thread, client, finished):
            websocket = await client.ws_connect(
                bridge_module.AGENT_SESSION_CONNECT_PATH,
                headers={"Authorization": "Bearer cleanup-transport-test"},
            )
            await websocket.send_json(self.hello())
            await websocket.receive_json(timeout=2)
            session = bridge.sessions.get("cleanup-session")
            handled = asyncio.Event()

            async def barrier(_payload: dict[str, object]) -> None:
                handled.set()

            with patch.object(bridge, "handle_command_ack", new=barrier):
                payloads: tuple[object, ...] = ([], None, 12, "text")
                for payload in payloads:
                    await websocket.send_json(payload)
                await websocket.send_json({**self.hello(), "type": "command_ack", "command_id": "barrier"})
                await asyncio.wait_for(handled.wait(), timeout=2)
            self.assertIs(bridge.sessions.get("cleanup-session"), session)
            self.assertFalse(finished.is_set())
            self.assertFalse(thread.archived)
            await websocket.close()
            await asyncio.wait_for(finished.wait(), timeout=2)
            self.assertIsNone(bridge.sessions.get("cleanup-session"))
            self.assertTrue(thread.archived)

    async def test_second_hello_cannot_orphan_first_registration(self) -> None:
        async with self.transport() as (bridge, thread, client, finished):
            websocket = await client.ws_connect(
                bridge_module.AGENT_SESSION_CONNECT_PATH,
                headers={"Authorization": "Bearer cleanup-transport-test"},
            )
            await websocket.send_json(self.hello())
            await websocket.receive_json(timeout=2)
            await websocket.send_json({**self.hello(), "session_id": "different-session"})
            # Drain the server close handshake without cancelling its cleanup.
            await websocket.receive(timeout=2)
            await websocket.close()
            await asyncio.wait_for(finished.wait(), timeout=2)
            self.assertEqual(bridge.sessions.by_session, {})
            self.assertEqual(bridge.sessions.by_thread, {})
            self.assertTrue(thread.archived)
