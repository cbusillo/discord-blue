import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
import discord

from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.doodads.agent_session.threads import SessionThread
from tests.fakes_agent_prompts import prompt_fixture
from tests.fakes_agent_session import FakeWebSocket


class NativeShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_shutdown_budget_cannot_publish_even_an_empty_snapshot(self) -> None:
        with (
            TemporaryDirectory() as folder,
            patch("pathlib.Path.home", return_value=Path(folder)),
            prompt_fixture() as fixture,
        ):
            await fixture.bridge.stop(deadline=asyncio.get_running_loop().time() - 1)
            self.assertIsNone(fixture.bridge.handover.snapshot)
            fixture.bridge.threads.stop()

    async def test_expired_drain_still_waits_for_disconnect_and_store_close(self) -> None:
        with (
            TemporaryDirectory() as folder,
            patch("pathlib.Path.home", return_value=Path(folder)),
            prompt_fixture() as fixture,
        ):
            bridge = fixture.bridge
            await bridge.store.start()
            closed = asyncio.Event()
            original = bridge.store.close

            async def close() -> None:
                await asyncio.sleep(0.02)
                await original()
                closed.set()

            async def cleanup() -> None:
                await asyncio.sleep(0.1)

            bridge._runner = cast(web.AppRunner, cast(object, SimpleNamespace(cleanup=cleanup)))
            with (
                patch.object(bridge.store, "close", close),
                patch("discord_blue.doodads.agent_session.bridge.SESSION_STORE_WAIT_TIMEOUT_SECONDS", 0.05),
            ):
                try:
                    await bridge.stop(deadline=asyncio.get_running_loop().time() - 1)
                    self.assertTrue(closed.is_set())
                    self.assertIsNone(bridge.sessions.get(fixture.session.session_id))
                    saved = json.loads(bridge.store.path.read_text())
                    self.assertEqual(saved[fixture.session.session_id]["status"], "grace")
                    self.assertIsNone(bridge.handover.snapshot)
                finally:
                    if bridge.threads.background:
                        await asyncio.gather(*bridge.threads.background, return_exceptions=True)
                    await original()
                    bridge.threads.stop()

    async def test_failed_cleanup_cannot_reuse_or_publish_a_snapshot(self) -> None:
        with (
            TemporaryDirectory() as folder,
            patch("pathlib.Path.home", return_value=Path(folder)),
            prompt_fixture() as fixture,
        ):
            bridge = fixture.bridge
            bridge.handover.snapshot = {"agents": {"stale": {}}, "conversation": None}
            with patch.object(bridge.store, "close", AsyncMock(side_effect=OSError("inert writer failure"))):
                with self.assertRaises(OSError):
                    await bridge.stop()
            self.assertIsNone(bridge.handover.snapshot)
            bridge.threads.stop()

    async def test_deadline_does_not_cancel_admitted_ack_or_publish_a_snapshot(self) -> None:
        with (
            TemporaryDirectory() as folder,
            patch("pathlib.Path.home", return_value=Path(folder)),
            prompt_fixture() as fixture,
        ):
            bridge = fixture.bridge
            entered, release = asyncio.Event(), asyncio.Event()

            async def acknowledge(_payload: dict[str, object]) -> None:
                entered.set()
                await release.wait()
                fixture.session.active_command_id = "inert-command"

            with patch.object(bridge.controls, "handle_command_ack", side_effect=acknowledge):
                incoming = asyncio.create_task(bridge.handle_event(fixture.session, "command_ack", {}))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    await asyncio.wait_for(bridge.stop(deadline=asyncio.get_running_loop().time() + 0.02), 1)
                    self.assertIsNone(bridge.handover.snapshot)
                    self.assertFalse(incoming.done())
                    release.set()
                    await incoming
                    self.assertEqual(fixture.session.active_command_id, "inert-command")
                finally:
                    release.set()
                    await incoming
                    bridge.threads.stop()

    async def test_shutdown_waits_for_admitted_hello(self) -> None:
        with (
            TemporaryDirectory() as folder,
            patch("pathlib.Path.home", return_value=Path(folder)),
            prompt_fixture() as fixture,
        ):
            bridge = fixture.bridge
            bridge.sessions.remove(fixture.session.session_id)
            socket = FakeWebSocket()
            session = AgentSession(fixture.session.hello, cast(web.WebSocketResponse, cast(object, socket)))
            bridge.sessions.register(session)
            entered, release = asyncio.Event(), asyncio.Event()

            async def discover(_hello: object) -> SessionThread:
                entered.set()
                await release.wait()
                return SessionThread(cast(discord.Thread, cast(object, fixture.thread)), None)

            with patch.object(bridge.session_threads, "find_or_create_session_thread", side_effect=discover):
                hello = asyncio.create_task(bridge.accept_hello(session, None))
                stopping: asyncio.Task[None] | None = None
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    stopping = asyncio.create_task(bridge.stop())
                    await asyncio.sleep(0)
                    self.assertFalse(stopping.done(), "Shutdown crossed an admitted HELLO before its attach settled")
                    release.set()
                    await asyncio.wait_for(stopping, 2)
                    self.assertFalse(await hello)
                    self.assertFalse(any(payload.get("type") == "hello_ack" for payload in socket.sent_json))
                finally:
                    release.set()
                    await asyncio.gather(hello, return_exceptions=True)
                    if stopping is not None:
                        await stopping
                    bridge.threads.stop()

    async def test_shutdown_closes_admission_and_cleans_up_after_drain_timeout(self) -> None:
        with (
            TemporaryDirectory() as folder,
            patch("pathlib.Path.home", return_value=Path(folder)),
            prompt_fixture() as fixture,
        ):
            bridge = fixture.bridge
            entered, release = asyncio.Event(), asyncio.Event()
            cleaned = asyncio.Event()

            async def cleanup() -> None:
                cleaned.set()

            bridge._runner = cast(web.AppRunner, cast(object, SimpleNamespace(cleanup=cleanup)))

            async def acknowledge(_payload: dict[str, object]) -> None:
                entered.set()
                await release.wait()

            with patch.object(bridge.controls, "handle_command_ack", side_effect=acknowledge):
                incoming = asyncio.create_task(bridge.handle_event(fixture.session, "command_ack", {}))
                await asyncio.wait_for(entered.wait(), 1)
                stopping = asyncio.create_task(bridge.stop(deadline=asyncio.get_running_loop().time() + 0.02))
                try:
                    await asyncio.sleep(0)
                    extra = AgentSession(fixture.session.hello, cast(web.WebSocketResponse, cast(object, FakeWebSocket())))
                    self.assertFalse(await bridge.accept_hello(extra, None))
                    await asyncio.wait_for(stopping, 1)
                    self.assertIsNone(bridge.handover.snapshot)
                    self.assertTrue(fixture.socket.closed)
                    self.assertTrue(cleaned.is_set())
                finally:
                    release.set()
                    await incoming
                    await stopping
                    bridge.threads.stop()

    async def test_native_frame_already_read_is_handled_before_shutdown_snapshot(self) -> None:
        with (
            TemporaryDirectory() as folder,
            patch("pathlib.Path.home", return_value=Path(folder)),
            prompt_fixture() as fixture,
        ):
            bridge = fixture.bridge
            closing, release = asyncio.Event(), asyncio.Event()
            original = fixture.socket.close
            original_export = bridge.handover.export
            exported = []

            def export() -> dict[str, object]:
                exported.append(fixture.session.active_command_id)
                return original_export()

            async def close(*, message: bytes = b"", drain: bool = True) -> None:
                closing.set()
                await release.wait()
                await original(message=message, drain=drain)

            async def acknowledge(_payload: dict[str, object]) -> None:
                fixture.session.active_command_id = "late-native-ack"

            with (
                patch.object(fixture.socket, "close", close),
                patch.object(bridge.controls, "handle_command_ack", acknowledge),
                patch.object(bridge.handover, "export", export),
            ):
                stopping = asyncio.create_task(bridge.stop())
                try:
                    await asyncio.wait_for(closing.wait(), 1)
                    await bridge.handle_event(fixture.session, "command_ack", {})
                    self.assertEqual(fixture.session.active_command_id, "late-native-ack")
                finally:
                    release.set()
                    await stopping
                    bridge.threads.stop()
            self.assertEqual(exported, ["late-native-ack"])
            self.assertIsNotNone(bridge.handover.snapshot)
