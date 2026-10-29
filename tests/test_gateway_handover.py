import asyncio
from collections.abc import AsyncIterator
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientSession, ClientWebSocketResponse, web, WSMsgType, WSCloseCode
from aiohttp.test_utils import TestServer
import discord
from discord.gateway import DiscordWebSocket

from discord_blue.gateway_handover import GatewayContinuation, GatewayHandover
from discord_blue.plugs.discord_plug import BlueBot
from discord_blue.doodads.agent_session_doodad import AgentSessionDoodad
from tests.fakes_agent_prompts import prompt_fixture
from tests.test_session_cleanup_progress import wait_until


class GatewayHandoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch("pathlib.Path.home", return_value=self.folder))
        self.bot = BlueBot(cast(Any, SimpleNamespace(discord=SimpleNamespace(guild_id=1, bot_channel_id=2))))
        await self.bot._async_setup_hook()
        self.addAsyncCleanup(self.bot.close)
        self.continuation = GatewayContinuation(99, "inert-session", 40, "wss://gateway.discord.gg/", {})
        self.continuation.save(self.bot.handover.path)
        self.guild = discord.Guild(
            state=self.bot._connection,
            data=cast(Any, {"id": "1", "name": "inert", "roles": [], "emojis": [], "features": [], "channels": []}),
        )
        self.channel = discord.TextChannel(
            state=self.bot._connection,
            guild=self.guild,
            data=cast(Any, {"id": "2", "type": 0, "name": "inert", "position": 0, "permission_overwrites": []}),
        )

        async def guilds(**_kwargs: object) -> AsyncIterator[discord.Guild]:
            yield self.guild

        async def login(_token: str) -> None:
            self.bot._connection.user = discord.ClientUser(
                state=self.bot._connection,
                data={
                    "id": str(self.continuation.bot_id),
                    "username": "inert",
                    "discriminator": "0",
                    "avatar": None,
                    "global_name": None,
                },
            )
            self.bot.http.token = "inert-token"

        self.enterContext(patch.object(self.bot, "login", side_effect=login))
        self.enterContext(patch.object(self.bot, "before_identify_hook", return_value=None))
        self.enterContext(patch.object(self.bot, "fetch_guilds", guilds))
        self.enterContext(patch.object(self.bot, "fetch_guild", return_value=self.guild))
        self.enterContext(patch.object(discord.Guild, "fetch_channels", return_value=[self.channel]))
        self.enterContext(patch.object(discord.Guild, "active_threads", return_value=[]))
        member = discord.Member(
            state=self.bot._connection,
            guild=self.guild,
            data=cast(
                Any,
                {
                    "user": {"id": str(self.continuation.bot_id), "username": "inert", "discriminator": "0", "avatar": None},
                    "roles": [],
                    "flags": 0,
                },
            ),
        )
        self.enterContext(patch.object(discord.Guild, "fetch_member", return_value=member))
        self.received = asyncio.Event()
        self.resume_payload: dict[str, Any] | None = None
        self.identified = False
        self.identified_event = asyncio.Event()
        self.reject_resume = False
        self.peer: web.WebSocketResponse | None = None
        self.last_sequence = self.continuation.sequence
        self.closed_code: int | None = None
        app = web.Application()
        app.router.add_get("/gateway", self.gateway)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.http = ClientSession()
        self.addAsyncCleanup(self.http.close)

        async def connect(_url: str, **_kwargs: object) -> ClientWebSocketResponse:
            return await self.http.ws_connect(self.server.make_url("/gateway"))

        self.enterContext(patch.object(self.bot.http, "ws_connect", side_effect=connect))
        self.enterContext(patch.object(self.bot.http, "request", side_effect=AssertionError("External HTTP forbidden")))

    async def gateway(self, request: web.Request) -> web.WebSocketResponse:
        peer = self.peer = web.WebSocketResponse()
        await peer.prepare(request)
        await peer.send_json({"op": DiscordWebSocket.HELLO, "d": {"heartbeat_interval": 100000}})
        async for frame in peer:
            if frame.type != WSMsgType.TEXT:
                continue
            payload = json.loads(frame.data)
            if payload["op"] == DiscordWebSocket.HEARTBEAT:
                await peer.send_json({"op": DiscordWebSocket.HEARTBEAT_ACK})
            elif payload["op"] == DiscordWebSocket.RESUME:
                self.resume_payload = payload["d"]
                if self.reject_resume:
                    await peer.send_json({"op": DiscordWebSocket.INVALIDATE_SESSION, "d": False})
                    continue
                await self.message()
                self.last_sequence += 1
                await peer.send_json({"op": DiscordWebSocket.DISPATCH, "s": self.last_sequence, "t": "RESUMED", "d": {}})
                self.received.set()
            elif payload["op"] == DiscordWebSocket.IDENTIFY:
                self.identified = True
                self.identified_event.set()
                self.received.set()
        self.closed_code = peer.close_code
        return peer

    async def message(self) -> None:
        assert self.peer is not None
        self.last_sequence += 1
        await self.peer.send_json(
            {
                "op": DiscordWebSocket.DISPATCH,
                "s": self.last_sequence,
                "t": "MESSAGE_CREATE",
                "d": self.message_payload(),
            }
        )

    def message_payload(self) -> dict[str, Any]:
        return {
            "id": str(123456789012345678 + self.last_sequence),
            "guild_id": str(self.guild.id),
            "channel_id": str(self.channel.id),
            "author": {"id": "3", "username": "operator", "discriminator": "0", "avatar": None},
            "content": "inert gap reply",
            "timestamp": "2026-10-29T16:00:00+00:00",
            "edited_timestamp": None,
            "tts": False,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
            "embeds": [],
            "pinned": False,
            "type": 0,
        }

    async def start_bot(self) -> asyncio.Task[None]:
        task = self.begin()
        await asyncio.wait_for(self.received.wait(), 5)
        self.assertFalse(self.identified)
        assert self.resume_payload is not None
        self.assertEqual(self.resume_payload["session_id"], self.continuation.session_id)
        self.assertEqual(self.resume_payload["seq"], self.continuation.sequence)
        return task

    def begin(self) -> asyncio.Task[None]:
        task = asyncio.create_task(self.bot.start("inert-token"))
        self.addAsyncCleanup(self.finish, task)
        return task

    async def finish(self, task: asyncio.Task[None]) -> None:
        await self.bot.close()
        await asyncio.wait_for(task, 5)

    async def test_resume_restores_cache_and_replays_once_then_keeps_live_dispatch(self) -> None:
        messages: list[discord.Message] = []
        changed = asyncio.Event()

        async def on_message(message: discord.Message) -> None:
            messages.append(message)
            changed.set()

        self.bot.add_listener(on_message)
        task = await self.start_bot()
        await asyncio.wait_for(changed.wait(), 5)
        self.assertEqual(len(messages), 1)
        self.assertIs(messages[0].guild, self.guild)
        self.assertTrue(self.bot.is_ready())
        self.assertIsNotNone(self.guild.me)
        changed.clear()
        await self.message()
        await asyncio.wait_for(changed.wait(), 5)
        self.assertEqual(len(messages), 2)
        self.assertNotEqual(messages[0].id, messages[1].id)
        await self.bot.close()
        await asyncio.wait_for(task, 5)

    async def test_shutdown_finishes_received_work_before_saving_resumable_checkpoint(self) -> None:
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def on_message(_message: discord.Message) -> None:
            started.set()
            await release.wait()
            finished.set()

        self.bot.add_listener(on_message)
        task = await self.start_bot()
        await asyncio.wait_for(started.wait(), 5)
        self.assertFalse(self.bot.handover.healthy())
        shutdown = asyncio.create_task(self.bot.close())
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        self.assertFalse(self.bot.handover.path.exists())
        release.set()
        await asyncio.wait_for(shutdown, 5)
        await asyncio.wait_for(task, 5)
        self.assertTrue(finished.is_set())
        saved = GatewayContinuation.read(self.bot.handover.path, self.continuation.bot_id)
        self.assertEqual(saved.session_id, self.continuation.session_id)
        self.assertEqual(saved.sequence, self.last_sequence)
        self.assertIsNotNone(self.closed_code)
        self.assertNotIn(self.closed_code, (WSCloseCode.OK, WSCloseCode.GOING_AWAY))

    async def test_rejected_resume_does_not_report_fresh_identify_as_catch_up(self) -> None:
        self.reject_resume = True
        task = self.begin()
        await asyncio.wait_for(self.identified_event.wait(), 5)
        self.assertIsNotNone(self.resume_payload)
        self.assertFalse(self.bot.handover.healthy())
        self.assertEqual(self.bot.handover.evidence()["status"], "failed")
        await self.bot.close()
        await asyncio.wait_for(task, 5)

    async def test_event_handler_can_close_without_waiting_for_itself(self) -> None:
        closed = asyncio.Event()

        async def on_message(_message: discord.Message) -> None:
            await self.bot.close()
            closed.set()

        self.bot.add_listener(on_message)
        task = await self.start_bot()
        await asyncio.wait_for(closed.wait(), 5)
        await asyncio.wait_for(task, 5)

    async def test_uncached_foreign_edit_reaches_conversation_listener(self) -> None:
        foreign = discord.Guild(
            state=self.bot._connection,
            data=cast(
                Any,
                {
                    "id": "20",
                    "name": "foreign",
                    "roles": [],
                    "emojis": [],
                    "features": [],
                    "channels": [{"id": "21", "type": 0, "name": "inert", "position": 0, "permission_overwrites": []}],
                },
            ),
        )
        self.bot._connection._add_guild(foreign)
        changed = asyncio.Event()
        edits: list[discord.Message] = []

        async def amended(_before: discord.Message | None, after: discord.Message) -> None:
            edits.append(after)
            changed.set()

        self.bot.add_listener(amended, "on_conversation_message_edit")
        payload = {
            **self.message_payload(),
            "guild_id": str(foreign.id),
            "channel_id": "21",
            "content": "inert amendment",
            "edited_timestamp": "2026-10-29T16:00:01+00:00",
        }
        self.assertIsNone(self.bot._connection._get_message(int(payload["id"])))
        self.bot._connection.parse_message_update(cast(Any, payload))
        await asyncio.wait_for(changed.wait(), 1)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].content, payload["content"])
        self.assertEqual(edits[0].guild, foreign)

    async def test_first_connect_failure_retries_without_consuming_checkpoint(self) -> None:
        attempts = 0

        async def connect(_url: str, **_kwargs: object) -> ClientWebSocketResponse:
            nonlocal attempts
            attempts += 1
            self.assertTrue(self.bot.handover.path.exists())
            if attempts == 1:
                raise OSError("inert transient network failure")
            return await self.http.ws_connect(self.server.make_url("/gateway"))

        with (
            patch.object(self.bot.http, "ws_connect", side_effect=connect),
            patch("discord_blue.gateway_handover.ExponentialBackoff.delay", return_value=0),
        ):
            task = await self.start_bot()
            await wait_until(lambda: self.bot.handover.healthy())
            self.assertEqual(attempts, 2)
            self.assertFalse(self.bot.handover.path.exists())
            await self.finish(task)

    async def test_failed_attempt_does_not_poison_next_clean_process_handover(self) -> None:
        task = await self.start_bot()
        await wait_until(lambda: self.bot.handover.healthy())
        self.bot.handover.status = "failed"
        self.assertFalse(self.bot.handover.healthy())
        await self.finish(task)
        self.continuation = GatewayContinuation.read(self.bot.handover.path, self.continuation.bot_id)
        self.bot.clear()
        await self.bot._async_setup_hook()
        self.bot.handover = GatewayHandover(self.bot, super(BlueBot, self.bot).close)
        self.received.clear()
        self.resume_payload = None
        successor = await self.start_bot()
        await wait_until(lambda: self.bot.handover.healthy())
        self.assertNotIn(self.continuation.session_id, self.bot.handover.active.read_text())
        await self.finish(successor)

    async def test_repeated_resume_does_not_restart_startup_callbacks(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def on_ready() -> None:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()

        self.bot.add_listener(on_ready)
        task = await self.start_bot()
        await asyncio.wait_for(entered.wait(), 2)
        assert self.peer is not None
        self.last_sequence += 1
        await self.peer.send_json({"op": DiscordWebSocket.DISPATCH, "s": self.last_sequence, "t": "RESUMED", "d": {}})
        await asyncio.sleep(0.01)
        self.assertEqual(calls, 1)
        release.set()
        await wait_until(lambda: self.bot.handover.healthy())
        await self.finish(task)

    async def test_failed_replay_callback_cannot_turn_green_when_its_task_finishes(self) -> None:
        async def on_message(_message: discord.Message) -> None:
            raise RuntimeError("inert replay failure")

        self.bot.add_listener(on_message)
        task = await self.start_bot()
        await wait_until(lambda: self.bot.handover.status == "failed")
        self.assertFalse(self.bot.handover.healthy())
        await self.finish(task)

    async def test_incompatible_native_continuation_does_not_prevent_gateway_startup(self) -> None:
        with prompt_fixture() as fixture:
            cog = Mock(spec=AgentSessionDoodad)
            cog.bridge = fixture.bridge
            cog.start_bridge = AsyncMock()
            self.bot.handover.agents = {fixture.session.session_id: "inert invalid record"}
            await self.bot.login("inert-token")
            with patch.object(self.bot, "get_cog", return_value=cog):
                await self.bot.handover.warm_cache()
            cog.start_bridge.assert_awaited_once()
            self.assertFalse(self.bot.handover.healthy())
            self.assertFalse(fixture.bridge.handover.saved)
