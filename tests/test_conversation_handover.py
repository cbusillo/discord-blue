import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

from aiohttp import ClientWebSocketResponse, web
from aiohttp.test_utils import TestClient, TestServer
import discord

from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.gateway_handover import GatewayContinuation
from tests.fakes_agent_session import FakeBot, FakeThread
from tests.test_external_conversation import CHANNEL_ID, FOREIGN, channel, config, hello, message


class ConversationHandoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch("pathlib.Path.home", return_value=self.folder))
        self.destination = channel()
        self.bot = FakeBot(config(), FakeThread(555), channel=cast(Any, self.destination))

    async def endpoint(self, bridge: AgentSessionBridge) -> TestClient[Any, Any]:
        app = web.Application()
        bridge.register_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        self.addAsyncCleanup(bridge.conversations.stop)
        return client

    async def connect(self, client: TestClient[Any, Any], **overrides: object) -> ClientWebSocketResponse:
        socket = await client.ws_connect(
            "/agent-session/conversation", headers={"Authorization": f"Bearer {self.bot.config.agent_session.token}"}
        )
        await socket.send_json(hello(**overrides))
        ack = await socket.receive_json(timeout=2)
        self.assertEqual(ack["type"], "hello_ack")
        return socket

    async def test_resume_keeps_unacked_input_reply_dedup_and_old_message_deletion(self) -> None:
        old = AgentSessionBridge(cast(Any, self.bot))
        old_socket = await self.connect(await self.endpoint(old))
        original = message()
        self.assertTrue(await old.conversations.receive_message(original))
        command = await old_socket.receive_json(timeout=2)
        reply = {**hello(), "type": "conversation_reply", "reply_id": "inert-reply", "text": "An answer"}
        await old_socket.send_json(reply)
        await old_socket.receive_json(timeout=2)
        await old.conversations.stop()
        path = self.folder / "continuation.json"
        GatewayContinuation(self.bot.user.id, "inert-session", 1, "wss://gateway.discord.gg/", {}, old.conversations.export()).save(
            path
        )
        saved = GatewayContinuation.read(path, self.bot.user.id)
        successor = AgentSessionBridge(cast(Any, self.bot))
        successor.conversations.restore(saved.conversation)
        client = await self.endpoint(successor)
        replay: set[asyncio.Task[Any]] = set()
        self.bot.handover.replay = replay
        task = asyncio.create_task(successor.conversations.receive_message(message(message_id=201)))
        replay.add(task)
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        socket = await self.connect(client)
        self.assertEqual(await socket.receive_json(timeout=2), command)
        self.assertTrue(await asyncio.wait_for(task, 2))
        replay.clear()
        gap = await socket.receive_json(timeout=2)
        self.assertNotEqual(gap["command_id"], command["command_id"])
        self.assertTrue(await successor.conversations.receive_message(original))
        await socket.send_json(reply)
        await socket.receive_json(timeout=2)
        self.assertEqual(self.destination.send.await_count, 1)
        payload = cast(
            discord.RawMessageDeleteEvent,
            cast(object, SimpleNamespace(message_id=original.id, guild_id=FOREIGN, channel_id=CHANNEL_ID)),
        )
        self.assertTrue(await successor.conversations.receive_delete(payload))
        deletion = await socket.receive_json(timeout=2)
        self.assertEqual(deletion["envelope"]["event"], "deletion")
        self.assertEqual(deletion["envelope"]["message_id"], original.id)

    async def test_changed_binding_does_not_inherit_pending_external_input(self) -> None:
        bridge = AgentSessionBridge(cast(Any, self.bot))
        saved = json.loads(json.dumps(bridge.conversations.export()))
        self.bot.config.conversation.allowed_user_ids = []
        with self.assertRaises(ValueError):
            bridge.conversations.restore(saved)

    async def test_unadopted_conversation_survives_another_handover(self) -> None:
        original = AgentSessionBridge(cast(Any, self.bot))
        socket = await self.connect(await self.endpoint(original))
        await original.conversations.receive_message(message())
        command = await socket.receive_json(timeout=2)
        saved = json.loads(json.dumps(original.conversations.export()))
        successor = AgentSessionBridge(cast(Any, self.bot))
        successor.conversations.restore(saved)
        saved_again = json.loads(json.dumps(successor.conversations.export()))
        third = AgentSessionBridge(cast(Any, self.bot))
        third.conversations.restore(saved_again)
        resumed = await self.connect(await self.endpoint(third))
        self.assertEqual(await resumed.receive_json(timeout=2), command)

    async def test_shutdown_waits_for_conversation_hello_and_keeps_pending_input(self) -> None:
        bridge = AgentSessionBridge(cast(Any, self.bot))
        client = await self.endpoint(bridge)
        old = await self.connect(client)
        await bridge.conversations.receive_message(message())
        command = await old.receive_json(timeout=2)
        entered, release = asyncio.Event(), asyncio.Event()
        send_json = web.WebSocketResponse.send_json

        async def gated_ack(socket: web.WebSocketResponse, payload: object) -> None:
            if isinstance(payload, dict) and payload.get("type") == "hello_ack":
                entered.set()
                await release.wait()
            await send_json(socket, payload)

        with patch.object(web.WebSocketResponse, "send_json", gated_ack):
            replacement = await client.ws_connect(
                "/agent-session/conversation", headers={"Authorization": f"Bearer {self.bot.config.agent_session.token}"}
            )
            await replacement.send_json(hello())
            stopping: asyncio.Task[None] | None = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                stopping = asyncio.create_task(bridge.stop())
                await asyncio.sleep(0)
                self.assertFalse(stopping.done())
                release.set()
                self.assertEqual((await replacement.receive_json(timeout=2))["type"], "hello_ack")
                self.assertEqual(await replacement.receive_json(timeout=2), command)
                await asyncio.wait_for(stopping, 2)
                assert bridge.handover.snapshot is not None
                saved = bridge.handover.snapshot["conversation"]
                self.assertEqual(dict(saved["pending"])[command["command_id"]], command)
                self.assertEqual(saved["epoch"], command["session_epoch"])
            finally:
                release.set()
                if stopping is not None:
                    await stopping

    async def test_reply_in_flight_does_not_block_reconnect_or_duplicate_discord_output(self) -> None:
        bridge = AgentSessionBridge(cast(Any, self.bot))
        client = await self.endpoint(bridge)
        old = await self.connect(client)
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(_text: str, **_kwargs: object) -> None:
            entered.set()
            await release.wait()

        self.destination.send.side_effect = send
        reply = {**hello(), "type": "conversation_reply", "reply_id": "inert-retry", "text": "One answer"}
        await old.send_json(reply)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            replacement = await asyncio.wait_for(self.connect(client), 2)
            await replacement.send_json(reply)
            release.set()
            ack = await replacement.receive_json(timeout=2)
            self.assertEqual(ack["type"], "conversation_reply_ack")
            self.assertEqual(ack["reply_id"], reply["reply_id"])
            self.assertEqual(self.destination.send.await_count, 1)
        finally:
            release.set()
