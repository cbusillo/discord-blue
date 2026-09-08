from __future__ import annotations

import asyncio
import json
import time
import unittest
from contextlib import suppress
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from aiohttp import web

from discord_blue.config import AgentSessionConfig, Config, DiscordConfig
from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from discord_blue.doodads.agent_session.sessions import AgentSession, PendingSessionCleanup
from discord_blue.plugs.discord_plug import BlueBot
from tests.fakes_agent_session import FakeBot, FakeThread, FakeWebSocket, make_hello


def make_bridge() -> AgentSessionBridge:
    config = cast(Config, SimpleNamespace(agent_session=AgentSessionConfig(), discord=DiscordConfig()))
    config.agent_session.enabled = True
    config.agent_session.heartbeat_timeout_seconds = 1
    return AgentSessionBridge(cast(BlueBot, FakeBot(config)))


def register_stale(bridge: AgentSessionBridge, session_id: str, *, closed: bool = False) -> AgentSession:
    hello = make_hello()
    hello.session_id = session_id
    session = AgentSession(hello=hello, websocket=cast(web.WebSocketResponse, FakeWebSocket(closed=closed)))
    session.last_seen -= timedelta(seconds=2)
    bridge.sessions.register(session)
    return session


class CleanupFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_exception_does_not_skip_later_session_or_lose_retry(self) -> None:
        bridge = make_bridge()
        first = register_stale(bridge, "first")
        second = register_stale(bridge, "second")
        bridge.sessions.bind_thread(first.session_id, 501)
        calls: list[str] = []

        async def cleanup(session: AgentSession, _cleanup: PendingSessionCleanup | None = None) -> None:
            self.assertTrue(bridge.session_lifecycle_lock(session.session_id).locked())
            calls.append(session.session_id)
            if session is first:
                raise RuntimeError("Discord cleanup failed")

        with patch.object(bridge, "close_session_thread", new=cleanup), self.assertLogs(bridge_module.logger, level="WARNING"):
            await bridge.close_timed_out_sessions()
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(bridge.sessions.by_session, {})
        self.assertTrue(first.websocket.closed)
        self.assertTrue(second.websocket.closed)
        self.assertTrue(bridge._pending_cleanups)

    async def test_monitor_keeps_sweeping_after_iteration_exception(self) -> None:
        bridge = make_bridge()
        bridge.bot.config.agent_session.heartbeat_check_interval_seconds = 0
        second_iteration = asyncio.Event()
        block = asyncio.Event()
        calls = 0

        async def sweep() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("one failed iteration")
            second_iteration.set()
            await block.wait()

        with patch.object(bridge, "close_timed_out_sessions", new=sweep), self.assertLogs(bridge_module.logger, level="ERROR"):
            monitor = asyncio.create_task(bridge.monitor_heartbeats())
            try:
                await asyncio.wait_for(second_iteration.wait(), timeout=1)
                self.assertFalse(monitor.done())
                self.assertEqual(calls, 2)
            finally:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor

    async def test_closed_socket_is_excluded_from_live_counts_before_finalization(self) -> None:
        bridge = make_bridge()
        session = register_stale(bridge, "already-disconnected", closed=True)
        # The registry may briefly retain a closed connection; reports must not
        # present it as live during that finalization window.
        self.assertIs(bridge.sessions.get(session.session_id), session)
        response = await bridge.handle_health(cast(web.Request, SimpleNamespace()))
        payload = json.loads(cast(bytes, response.body))
        self.assertEqual(payload["components"]["agent_session"]["active_sessions"], 0)
        self.assertEqual(bridge.active_sessions_summary(), "No live agent sessions.")

    async def test_stop_completes_after_background_tasks_already_failed(self) -> None:
        bridge = make_bridge()
        runner = SimpleNamespace(cleanup=AsyncMock())
        bridge._runner = cast(Any, runner)

        async def failed_task() -> None:
            raise RuntimeError("background task failed")

        heartbeat = asyncio.create_task(failed_task())
        maintenance = asyncio.create_task(failed_task())
        await asyncio.gather(heartbeat, maintenance, return_exceptions=True)
        bridge._heartbeat_task = heartbeat
        bridge._cleanup_task = maintenance
        with self.assertLogs(bridge_module.logger, level="WARNING"):
            await bridge.stop()
        runner.cleanup.assert_awaited_once()
        self.assertIsNone(bridge._runner)
        self.assertIsNone(bridge._heartbeat_task)
        self.assertIsNone(bridge._cleanup_task)

    async def test_health_reports_dead_monitor_even_when_discord_ready(self) -> None:
        bridge = make_bridge()

        async def fail() -> None:
            raise RuntimeError("monitor died")

        bridge._heartbeat_task = asyncio.create_task(fail())
        await asyncio.gather(bridge._heartbeat_task, return_exceptions=True)
        response = await bridge.handle_health(cast(web.Request, SimpleNamespace()))
        payload = json.loads(cast(bytes, response.body))
        self.assertEqual(response.status, 503)
        self.assertEqual(payload["status"], "unhealthy")
        self.assertEqual(payload["components"]["discord"]["status"], "ok")
        self.assertEqual(payload["components"]["agent_session"]["monitor"]["status"], "dead")

    async def test_health_detects_each_stalled_task_including_first_iteration(self) -> None:
        for component, first_iteration in (("heartbeat", False), ("maintenance", False), ("heartbeat", True)):
            with self.subTest(component=component, first_iteration=first_iteration):
                bridge = make_bridge()
                bridge._monitor_has_run = not first_iteration
                bridge._maintenance_has_run = True
                bridge._monitor_last_progress = time.monotonic()
                bridge._maintenance_last_progress = time.monotonic()
                if component == "heartbeat":
                    bridge._monitor_last_progress -= 10000
                else:
                    bridge._maintenance_last_progress -= 10000
                response = await bridge.handle_health(cast(web.Request, SimpleNamespace()))
                payload = json.loads(cast(bytes, response.body))
                self.assertEqual(response.status, 503)
                self.assertEqual(payload["status"], "unhealthy")
                self.assertEqual(payload["components"]["agent_session"]["monitor"][component]["status"], "stalled")

    async def test_startup_health_allows_full_maintenance_discovery_budget(self) -> None:
        bridge = make_bridge()
        with patch.object(bridge_module.time, "monotonic", return_value=100):
            bridge._monitor_last_progress = 100
            bridge._maintenance_last_progress = 100 - (
                bridge_module.STARTUP_RECONNECT_GRACE_SECONDS
                + bridge_module.MAINTENANCE_DISCOVERY_TIMEOUT_SECONDS
                + bridge_module.SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS
            )
            self.assertEqual(bridge.agent_session_monitor_health()["status"], "starting")
            bridge._maintenance_last_progress -= 10
            self.assertEqual(bridge.agent_session_monitor_health()["status"], "stalled")

    async def test_socket_or_notification_timeout_still_archives_thread(self) -> None:
        for phase in ("socket", "notification"):
            with self.subTest(phase=phase):
                bridge = make_bridge()
                thread = FakeThread(501)
                session = register_stale(bridge, "timed-out-phase")
                bridge.sessions.bind_thread(session.session_id, thread.id, 101)
                cancelled = asyncio.Event()

                async def hang(*_args: object, _cancelled: asyncio.Event = cancelled, **_kwargs: object) -> bool:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        _cancelled.set()
                    return True

                target = session.websocket if phase == "socket" else bridge
                method = "close" if phase == "socket" else "delete_session_notification"
                with (
                    patch.object(bridge_module.discord, "Thread", FakeThread),
                    patch.object(bridge, "get_thread_for_cleanup", new=AsyncMock(return_value=(thread, True))),
                    patch.object(bridge_module, "SESSION_WEBSOCKET_CLOSE_TIMEOUT_SECONDS", 0.01),
                    patch.object(bridge_module, "SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS", 0.01),
                    patch.object(target, method, new=hang),
                    self.assertLogs(bridge_module.logger, level="WARNING"),
                ):
                    await bridge.finalize_session(session)
                self.assertTrue(cancelled.is_set())
                self.assertTrue(thread.archived)
                self.assertTrue(thread.left)
                self.assertEqual(bridge.sessions.by_session, {})
                if phase == "notification":
                    self.assertTrue(bridge._pending_cleanups)
