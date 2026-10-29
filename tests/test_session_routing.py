from __future__ import annotations

import dataclasses
import asyncio
import os
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from discord_blue.claude_channel.__main__ import run_channel
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.doodads.agent_session.protocol import SessionHello
from discord_blue.claude_channel.session import Identity
from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.discovery import DiscoveryIndex
from discord_blue.doodads.agent_session.session_threads import DiscoveryIncomplete, SessionThreads
from discord_blue.doodads.agent_session.store import StoredSession
from discord_blue.doodads.agent_session.threads import create_session_thread, session_start_message
from discord_blue.config import AgentSessionConfig, Config, ConversationConfig, DiscordConfig
from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.doodads.agent_session_doodad import AgentSessionDoodad
import discord
from tests.fakes_agent_session import (
    FakeBot,
    FakeReplyMessage,
    FakeTextChannel,
    FakeThread,
    FakeWebSocket,
    add_bot_message,
    every_user_is_operator,
    make_hello,
)
from tests.test_claude_channel import IDENTITY, FakeClaudeCode, running_channel
from tests.test_codex_bridge import FakeRpc, running_bridge, thread


class SessionRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch("discord_blue.doodads.agent_session.threads.discord.TextChannel", FakeTextChannel))
        self.enterContext(patch("discord_blue.doodads.agent_session.threads.discord.Thread", FakeThread))

    async def test_new_claude_channel_attaches_while_other_parent_is_rate_limited(self) -> None:
        for limited_spawned in (True, False):
            with self.subTest(limited_spawned=limited_spawned):
                config = SimpleNamespace(
                    agent_session=AgentSessionConfig(), discord=DiscordConfig(), conversation=ConversationConfig()
                )
                config.agent_session.channel_id = 321
                config.agent_session.spawned_channel_id = 654
                config.agent_session.token = "routing-test"
                main, spawned = FakeTextChannel(321, []), FakeTextChannel(654, [])
                bot = FakeBot(cast(Any, config))
                bot.get_channel = {321: main, 654: spawned}.get  # type: ignore[method-assign, assignment]
                state_home = self.enterContext(tempfile.TemporaryDirectory())
                bridge = AgentSessionBridge(cast(Any, bot), store_path=Path(state_home) / "sessions.json")
                limited = spawned if limited_spawned else main
                creating = asyncio.Event()

                async def rate_limited(*, started: asyncio.Event = creating, **_kwargs: object) -> FakeThread:
                    started.set()
                    raise discord.RateLimited(300)

                app = web.Application()
                bridge.register_routes(app)
                with (
                    patch.object(limited, "create_thread", rate_limited),
                    patch.object(SessionThreads, "delete_creation_duplicates", AsyncMock()),
                ):
                    async with TestServer(app) as server, aiohttp.ClientSession() as http:
                        url = str(server.make_url("/agent-session/connect")).replace("http:", "ws:")
                        waiting = await http.ws_connect(url, headers={"Authorization": f"Bearer {config.agent_session.token}"})
                        await waiting.send_json(
                            {
                                "type": "hello",
                                "session_id": "limited",
                                "session_epoch": "limited-epoch",
                                "cwd": "/work/limited",
                                "session_kind": "spawned" if limited_spawned else "director",
                            }
                        )
                        await asyncio.wait_for(creating.wait(), 2)
                        claude = FakeClaudeCode()
                        identity = dataclasses.replace(IDENTITY, spawned=not limited_spawned)
                        channel_config = BridgeConfig(url, config.agent_session.token, Path("/unused"), "test")
                        channel = asyncio.create_task(run_channel(claude.stdin, claude, channel_config, identity))
                        try:
                            await claude.initialize()
                            async with asyncio.timeout(1):
                                while not (session := bridge.sessions.get(identity.session_id)) or not session.acknowledged:
                                    await asyncio.sleep(0.01)
                            self.assertEqual(session.thread_id, (main if limited_spawned else spawned).threads[0].id)
                            self.assertEqual(limited.threads, [])
                        finally:
                            bridge.session_threads.creation_stopping.set()
                            claude.stdin.feed_eof()
                            await asyncio.wait_for(channel, 5)
                            await waiting.close()
                            await asyncio.gather(*bridge._attach_tasks.values(), return_exceptions=True)
                            for task in list(bridge._grace_tasks):
                                task.cancel()
                            await asyncio.gather(*bridge._grace_tasks, return_exceptions=True)
                            bridge.threads.stop()

    def test_claude_reads_only_the_explicit_launcher_label(self) -> None:
        for label in ("spawned", "director", ""):
            with (
                self.subTest(label=label),
                patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "session", "DISCORD_BLUE_SESSION_KIND": label}),
                patch("discord_blue.claude_channel.session.git_branch", return_value=None),
                patch("discord_blue.claude_channel.session.ancestry", return_value=[]),
            ):
                identity = Identity.from_environment()
            self.assertIsNotNone(identity)
            assert identity is not None
            self.assertEqual(identity.spawned, label == "spawned")

    async def test_recovery_keeps_existing_threads_in_either_parent_despite_the_new_label(self) -> None:
        for parent_id, spawned in ((321, True), (654, False)):
            with self.subTest(parent_id=parent_id):
                config = cast(
                    Config,
                    SimpleNamespace(agent_session=AgentSessionConfig(), discord=DiscordConfig(), conversation=ConversationConfig()),
                )
                config.agent_session.channel_id = 321
                config.agent_session.spawned_channel_id = 654
                config.agent_session.auto_join_user_ids = [123]
                hello = dataclasses.replace(make_hello(), spawned=spawned)
                existing = FakeThread(9999, archived=True)
                existing.parent_id = parent_id
                add_bot_message(existing, 1, session_start_message(hello))
                main = FakeTextChannel(321, [existing] if parent_id == 321 else [])
                second = FakeTextChannel(654, [existing] if parent_id == 654 else [])
                bot = FakeBot(config)
                bot.get_channel = {321: main, 654: second, existing.id: existing}.get  # type: ignore[method-assign, assignment]
                bridge = AgentSessionBridge(cast(Any, bot))
                discovered = await bridge.session_threads.find_existing_session_thread(hello)
                self.assertIs(discovered, existing)
                bridge.store.records[hello.session_id] = StoredSession(existing.id, None, session_start_message(hello), "live", 0, 0)
                recovered = await bridge.session_threads.resume_stored_thread(hello)
                self.assertEqual(existing._member_ids, config.agent_session.auto_join_user_ids if parent_id == 321 else [])
                self.assertIsNotNone(recovered)
                assert recovered is not None
                self.assertIs(recovered.thread, existing)
                self.assertEqual(main.sent_messages, [])
                self.assertEqual(second.sent_messages, [])
                self.assertTrue(await bridge.cleanups.delete_session_notification(recovered.notification_message_id or 0))
                self.assertFalse((main if parent_id == main.id else second)._history)

    async def test_attach_waits_until_both_parent_scans_complete(self) -> None:
        index = DiscoveryIndex(lambda: 999)
        main, second = FakeTextChannel(321, []), FakeTextChannel(654, [])
        await index.fresh(cast(Any, main), additional_channels=(cast(Any, second),))
        started, release = asyncio.Event(), asyncio.Event()
        original = second.archived_threads

        async def slow_archive(**kwargs: object) -> AsyncIterator[FakeThread]:
            started.set()
            await release.wait()
            async for item in original(**kwargs):
                yield item

        with patch.object(second, "archived_threads", new=slow_archive):
            scanning = asyncio.create_task(index.fresh(cast(Any, main), additional_channels=(cast(Any, second),), force=True))
            await started.wait()
            joining = asyncio.create_task(index.fresh(cast(Any, main), additional_channels=(cast(Any, second),)))
            try:
                await asyncio.sleep(0)
                self.assertFalse(joining.done())
            finally:
                release.set()
                await asyncio.gather(scanning, joining)

    async def test_second_parent_failure_leaves_the_whole_scan_incomplete(self) -> None:
        index = DiscoveryIndex(lambda: 999)
        main, second = FakeTextChannel(321, []), FakeTextChannel(654, [])
        await index.fresh(cast(Any, main), additional_channels=(cast(Any, second),))
        original = DiscoveryIndex.list_threads

        async def fail_second(index: DiscoveryIndex, channel: discord.TextChannel) -> tuple[list[discord.Thread], bool]:
            if channel is second:
                raise OSError("Channel listing failed")
            return await original(index, channel)

        with patch.object(DiscoveryIndex, "list_threads", new=fail_second):
            for force in (True, False):
                with self.subTest(force=force), self.assertRaises(OSError):
                    await index.fresh(cast(Any, main), additional_channels=(cast(Any, second),), force=force)
                self.assertFalse(index.complete)

    async def test_thread_replies_keep_the_same_controls_in_both_channels(self) -> None:
        for parent_id in (321, 654):
            with self.subTest(parent_id=parent_id), every_user_is_operator():
                config = cast(
                    Config,
                    SimpleNamespace(agent_session=AgentSessionConfig(), discord=DiscordConfig(), conversation=ConversationConfig()),
                )
                config.agent_session.channel_id = 321
                config.agent_session.spawned_channel_id = 654
                existing = FakeThread(9999)
                existing.parent_id = parent_id
                bot = FakeBot(config, existing)
                config.agent_session.enabled = True
                doodad = AgentSessionDoodad(cast(Any, bot))
                bridge = doodad.bridge
                websocket = FakeWebSocket()
                hello = dataclasses.replace(make_hello(), spawned=parent_id == 654)
                session = AgentSession(hello=hello, websocket=cast(Any, websocket), thread_id=existing.id, acknowledged=True)
                bridge.sessions.register(session)
                bridge.sessions.bind_thread(hello.session_id, existing.id)
                reply = FakeReplyMessage(802, existing, "Continue with the fix")
                reply.author.bot = False
                existing.add_message(reply)
                await doodad.route_thread_reply(cast(Any, reply))
                self.assertEqual(websocket.sent_json[0]["kind"], "reply")
                self.assertEqual(websocket.sent_json[0]["session_id"], hello.session_id)
                self.assertEqual(session.display_state, "working")

    async def test_codex_preserves_explicit_native_source_without_prompt_inference(self) -> None:
        rpc = FakeRpc(thread("spawned", threadSource="spawned"), thread("manual", threadSource="user"), thread("legacy"))
        async with running_bridge(rpc) as (_, discord):
            hellos = {
                hello["session_id"]: SessionHello.from_payload(hello) for hello in [await discord.next("hello") for _ in range(3)]
            }
        self.assertTrue(hellos["spawned"].spawned)
        self.assertFalse(hellos["manual"].spawned)
        self.assertFalse(hellos["legacy"].spawned)

    async def test_claude_preserves_spawned_label_on_reconnect(self) -> None:
        identity = dataclasses.replace(IDENTITY, spawned=True)
        async with running_channel(identity=identity) as (_, discord):
            first = SessionHello.from_payload(await discord.next("hello"))
            await discord.close()
            second = SessionHello.from_payload(await discord.next("hello"))
        self.assertTrue(first.spawned)
        self.assertTrue(second.spawned)

    async def test_routes_both_harnesses_and_unlabelled_sessions_with_correct_membership(self) -> None:
        for harness in ("codex", "claude"):
            for label in ("spawned", "director", None):
                with self.subTest(harness=harness, label=label):
                    config = cast(
                        Config,
                        SimpleNamespace(
                            agent_session=AgentSessionConfig(), discord=DiscordConfig(), conversation=ConversationConfig()
                        ),
                    )
                    config.agent_session.channel_id = 321
                    config.agent_session.spawned_channel_id = 654
                    config.agent_session.auto_join_user_ids = [123]
                    main, spawned = FakeTextChannel(321, []), FakeTextChannel(654, [])
                    bot = FakeBot(config)
                    bot.get_channel = {321: main, 654: spawned}.get  # type: ignore[method-assign, assignment]
                    hello = SessionHello.from_payload(
                        {
                            **{key: value for key, value in dataclasses.asdict(make_hello()).items() if key != "capabilities"},
                            "harness": harness,
                            "session_kind": label,
                        }
                    )
                    created = await create_session_thread(cast(Any, bot), hello)
                    self.assertEqual(spawned.sent_messages, [])
                    self.assertEqual(main.sent_messages, [])
                    self.assertEqual(created.thread.parent_id, spawned.id if label == "spawned" else main.id)
                    self.assertEqual(
                        cast(Any, created.thread)._member_ids, [] if label == "spawned" else config.agent_session.auto_join_user_ids
                    )

    async def test_spawned_session_never_falls_back_to_main_without_configured_second_channel(self) -> None:
        config = cast(
            Config, SimpleNamespace(agent_session=AgentSessionConfig(), discord=DiscordConfig(), conversation=ConversationConfig())
        )
        config.agent_session.channel_id = 321
        main = FakeTextChannel(321, [])
        hello = SessionHello.from_payload(
            {
                **{key: value for key, value in dataclasses.asdict(make_hello()).items() if key != "capabilities"},
                "session_kind": "spawned",
            }
        )
        with self.assertRaises(ValueError):
            await create_session_thread(cast(Any, FakeBot(config, channel=main)), hello)
        self.assertFalse(main.sent_messages)

    async def test_unavailable_second_parent_cannot_be_mistaken_for_no_existing_thread(self) -> None:
        config = cast(
            Config, SimpleNamespace(agent_session=AgentSessionConfig(), discord=DiscordConfig(), conversation=ConversationConfig())
        )
        config.agent_session.channel_id = 321
        config.agent_session.spawned_channel_id = 654
        main = FakeTextChannel(321, [])
        bridge = AgentSessionBridge(cast(Any, FakeBot(config, channel=main)))
        with self.assertRaises(DiscoveryIncomplete):
            await bridge.session_threads.find_or_create_session_thread(make_hello())
        self.assertFalse(main.threads)
