"""Attach incidents and review scenarios from #148, reproduced against the real bridge.

Each test runs the agent-session bridge over a real WebSocket, with its Discord
objects backed by the real discord.py 2.7.1 HTTP client talking to FakeDiscord.
Timings are scaled down from production (roughly 1:30) so the suite stays fast.
Tests marked expectedFailure describe behaviour #148 must deliver and that main
does not yet; each docstring says why main fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import unittest
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import aiohttp
from aiohttp import web

from discord_blue.claude_channel.session import ClaudeSession, Identity
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.config import AgentSessionConfig, Config, DiscordConfig
from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.protocol import SessionHello
from discord_blue.doodads.agent_session.threads import session_start_message
from tests.discord_http import HttpBot, discord_bot, scaled_discord_sleeps
from tests.fake_discord import BOT_ID, PARENT_ID, FakeDiscord, FakeThreadState, Fault
from tests.fakes_agent_session import FakeTextChannel, FakeThread

Json = dict[str, Any]
TOKEN = "scenario-token"
HOST = "Claude Code on test"


def hello_for(session_id: str, **fields: object) -> Json:
    return {
        "type": "hello",
        "session_id": session_id,
        "session_epoch": fields.pop("epoch", "e1"),
        "host_label": HOST,
        "cwd": f"/w/{session_id}",
        "branch": "main",
        "pid": 1,
        "harness": "claude",
        **fields,
    }


def marker(hello: Json) -> str:
    """The session-start message the bridge posts first in a session's thread."""
    return session_start_message(SessionHello.from_payload(hello))


class Scenario:
    """A running bridge over FakeDiscord, plus a record of every archive the fake applied."""

    def __init__(self, fake: FakeDiscord, bot: HttpBot, bridge: bridge_module.AgentSessionBridge, url: str) -> None:
        self.fake, self.bot, self.bridge, self.url = fake, bot, bridge, url
        self.archived: list[int] = []
        fake.listeners.append(lambda state, _deleted: self.archived.append(state.id) if state.archived else None)

    async def connect(self, http: aiohttp.ClientSession) -> aiohttp.ClientWebSocketResponse:
        return await http.ws_connect(self.url, headers={"Authorization": f"Bearer {TOKEN}"})

    def threads_marked_for(self, hello: Json) -> list[FakeThreadState]:
        return [t for t in self.fake.threads.values() if any(m.content == marker(hello) for m in t.messages)]


@contextlib.asynccontextmanager
async def scenario(fake: FakeDiscord, **agent_session: object) -> AsyncIterator[Scenario]:
    # No config file: Config() reads and writes one under HOME, which other test modules set up for themselves.
    config = cast(Config, SimpleNamespace(agent_session=AgentSessionConfig(), discord=DiscordConfig()))
    config.agent_session.token = TOKEN
    config.agent_session.channel_id = PARENT_ID
    config.discord.employee_role_name = ""
    for key, value in agent_session.items():
        setattr(config.agent_session, key, value)
    with (
        patch.object(bridge_module.discord, "Thread", FakeThread),
        patch.object(bridge_module.discord, "TextChannel", FakeTextChannel),
        scaled_discord_sleeps(0.01),
    ):
        async with discord_bot(fake, config) as bot:
            bridge = bridge_module.AgentSessionBridge(bot)  # type: ignore[arg-type]
            app = web.Application()
            bridge.register_routes(app)
            # Served as production serves it: aiohttp's TestServer cancels handlers when a client disconnects, which
            # production's AppRunner does not, and which would hide what a disconnect's teardown really does.
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            try:
                yield Scenario(fake, bot, bridge, f"ws://127.0.0.1:{runner.addresses[0][1]}/agent-session/connect")
            finally:
                for grace in list(bridge._grace_tasks):
                    grace.cancel()
                bridge.threads.stop()
                await runner.cleanup()


async def until(predicate: Callable[[], bool], timeout: float) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)
    except TimeoutError:
        return False
    return True


def fails_on_main(test: Callable[..., Any]) -> Callable[..., Any]:
    """Expected to fail until #148 lands; SHOW_SCENARIO_FAILURES=1 runs it as a plain test to see how."""
    return test if os.environ.get("SHOW_SCENARIO_FAILURES") else unittest.expectedFailure(test)


class AttachScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_restart_wave_of_seven_sessions_converges(self) -> None:
        """Main serializes attaches on one global lock, each reading every candidate thread's history (~30 s in
        production). Seven sessions need ~7 attach-times in sequence, longer than a client waits for its ack, so
        acks go to closed sockets and every retry rejoins the back of the queue."""
        fake = FakeDiscord(latency=0.008)  # One attach takes ~1 s here, as ~30 s does in production at 1:30.
        hellos = [hello_for(f"session-{n}") for n in range(7)]
        for hello in hellos:
            fake.add_thread(hello["session_id"], marker=marker(hello), archived=True, locked=True)
        for n in range(55):  # Other, older sessions' threads that discovery also reads.
            fake.add_thread(f"old-{n}", marker=marker(hello_for(f"old-{n}")), archived=True, locked=True)
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            config = BridgeConfig(
                server_url=running.url,
                token=TOKEN,
                socket_path=Path("/unused"),
                host_label=HOST,
                heartbeat_seconds=1,
                reconnect_seconds=0.15,
                hello_timeout_seconds=3,  # 90 s at 1:30
            )

            async def notify(_method: str, _params: Json) -> None:
                return None

            clients = [
                ClaudeSession(config, Identity(session_id=h["session_id"], cwd=h["cwd"], branch="main", pid=1), notify)
                for h in hellos
            ]
            tasks = [asyncio.create_task(client.run(http)) for client in clients]
            try:
                attached = await until(lambda: len(running.bridge.sessions.by_thread) == 7, timeout=4)  # ~2 min
            finally:
                for client in clients:
                    await client.stop()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        self.assertTrue(attached, "not every session attached after the restart")

    async def test_a_failed_ack_does_not_archive_a_live_sessions_thread(self) -> None:
        """Main treats a hello_ack written to a closed socket as the end of the session: its teardown archives and
        locks the thread, even though the client is already reconnecting (the 2026-09-29 16:55 and 17:00 incidents)."""
        fake = FakeDiscord(latency=0.02)
        hello = hello_for("codex-thread")
        thread = fake.add_thread("codex", marker=marker(hello), members={BOT_ID})
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            gave_up = await running.connect(http)
            await gave_up.send_json(hello)
            await gave_up.close()  # The client's ack timeout fired before the attach finished.
            await asyncio.sleep(1.0)  # Its reconnect delay; the abandoned attach finishes meanwhile.
            retry = await running.connect(http)
            await retry.send_json({**hello, "session_epoch": "e2"})
            ack = await retry.receive_json(timeout=10)
            await asyncio.sleep(0.5)
            await retry.close()

        self.assertEqual(ack["type"], "hello_ack")
        self.assertNotIn(thread.id, running.archived, "a live session's thread was archived after a failed ack")

    async def test_a_slow_ack_is_not_dropped_by_the_heartbeat_watchdog(self) -> None:
        """Main's watchdog counts from registration. When an attach takes longer than the heartbeat timeout, the
        next sweep after hello_ack closes the connection before the client's first heartbeat."""
        fake = FakeDiscord(latency=0.002)
        fake.route_latency[("GET", "/channels/{channel}/messages")] = 0.4  # A slow discovery read.
        hello = hello_for("slow")
        fake.add_thread("slow", marker=marker(hello), archived=True, locked=True)
        async with (
            scenario(fake, heartbeat_timeout_seconds=0.5, heartbeat_check_interval_seconds=0.1) as running,
            aiohttp.ClientSession() as http,
        ):
            monitor = asyncio.create_task(running.bridge.monitor_heartbeats())
            try:
                websocket = await running.connect(http)
                await websocket.send_json(hello)
                ack = await websocket.receive_json(timeout=10)
                closed = False
                for _ in range(4):
                    await asyncio.sleep(0.25)  # Heartbeats start after the ack, well inside the timeout.
                    try:
                        await websocket.send_json({"type": "heartbeat", "session_id": "slow", "session_epoch": "e1"})
                    except ConnectionResetError:
                        closed = True
                        break
                closed = closed or websocket.closed
                await websocket.close()
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

        self.assertEqual(ack["type"], "hello_ack")
        self.assertFalse(closed, "the watchdog dropped a connection that had just been acknowledged")

    async def test_a_late_archive_does_not_close_the_thread_of_the_next_attach(self) -> None:
        """Main's teardown bounds the archive edit with wait_for. Cancelling the wait does not stop the request:
        Discord applies it later, after the reconnected session has attached, and closes its thread."""
        fake = FakeDiscord(latency=0.005)
        hello = hello_for("reconnects")
        thread = fake.add_thread("reconnects", marker=marker(hello), members={BOT_ID})
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            first = await running.connect(http)
            await first.send_json(hello)
            await first.receive_json(timeout=10)
            # Only the archive edit is slow: far longer than teardown's 1 s archive budget.
            fake.body_latency.append(("PATCH", "/channels/{channel}", lambda body: body.get("archived") is True, 3.0))
            await first.close()
            await asyncio.sleep(0.2)
            second = await running.connect(http)
            await second.send_json({**hello, "session_epoch": "e2"})
            ack = await second.receive_json(timeout=15)
            await asyncio.sleep(3.2)  # Until the first session's archive has landed.
            still_open = not fake.threads[thread.id].archived
            await second.close()

        self.assertEqual(ack["type"], "hello_ack")
        self.assertTrue(still_open, "an archive from the earlier session closed the reconnected session's thread")

    async def test_a_teardown_that_stops_waiting_in_a_global_rate_limit_leaves_discord_usable(self) -> None:
        """Cancelling a discord.py 2.7.1 request while it sleeps out a global 429 leaves its global-limit event
        cleared, so every later request waits forever (the #147 review). Teardown's wait for its archive ends while
        discord.py is in that sleep; the next session must still attach."""
        fake = FakeDiscord(latency=0.002)
        ending, arriving = hello_for("ending"), hello_for("arriving")
        fake.add_thread("ending", marker=marker(ending), members={BOT_ID})
        fake.add_thread("arriving", marker=marker(arriving), archived=True, locked=True)
        with (
            patch.object(bridge_module, "SHUTDOWN_THREAD_CLEANUP_TIMEOUT_SECONDS", 0.2),
            patch.object(bridge_module, "THREAD_CLOSE_WAIT_SECONDS", 0.1),
        ):
            async with scenario(fake) as running, aiohttp.ClientSession() as http:
                first = await running.connect(http)
                await first.send_json(ending)
                await first.receive_json(timeout=10)
                # The close notice meets a global rate limit: 100 s, scaled to 1 s, far longer than teardown waits.
                fake.faults.append(Fault("POST", "/channels/{channel}/messages", status=429, retry_after=100, is_global=True))
                await first.send_json({"type": "session_end", "session_id": "ending", "session_epoch": "e1"})
                await first.close()
                self.assertTrue(await until(lambda: not fake.faults, timeout=5), "the close notice was never sent")
                await asyncio.sleep(0.3)  # Teardown has stopped waiting; discord.py is still in the global sleep.
                second = await running.connect(http)
                await second.send_json(arriving)
                ack = await second.receive_json(timeout=10)
                await second.close()

        self.assertEqual(ack["type"], "hello_ack")

    async def test_an_event_sent_right_after_the_ack_is_delivered(self) -> None:
        """discord.py re-caches a reopened thread only when its gateway THREAD_UPDATE arrives, after the REST reply.
        Main acknowledges as soon as the reopen returns, and handlers that look the thread up in the cache drop
        events sent in that window."""
        fake = FakeDiscord(latency=0.005, gateway_delay=0.2)
        hello = hello_for("archived")
        thread = fake.add_thread("archived", marker=marker(hello), archived=True, locked=True)
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello)
            await websocket.receive_json(timeout=10)
            await websocket.send_json(
                {
                    "type": "user_message",
                    "session_id": "archived",
                    "session_epoch": "e1",
                    "message": "typed right after reconnecting",
                }
            )
            delivered = await until(lambda: any("typed right after reconnecting" in m.content for m in thread.messages), 1)
            await websocket.close()

        self.assertTrue(delivered, "the first event after hello_ack never reached the thread")

    async def test_a_failed_discovery_read_does_not_create_a_duplicate_thread(self) -> None:
        """Main's discovery treats an error reading a candidate's history as 'not this session', so a transient
        Discord failure hides the session's existing thread and the bridge creates a second one."""
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("flaky")
        existing = fake.add_thread("flaky", marker=marker(hello), archived=True, locked=True)
        fake.faults.append(
            Fault("GET", "/channels/{channel}/messages", times=20, status=502, match=lambda ids: ids["channel"] == str(existing.id))
        )
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello)
            ack = await websocket.receive_json(timeout=15)
            await websocket.close()

        self.assertEqual(ack.get("thread_id"), existing.id, "the session was given a new thread instead of its own")
        self.assertEqual([t.id for t in running.threads_marked_for(hello)], [existing.id])

    @fails_on_main
    async def test_a_create_retried_by_discord_py_leaves_no_duplicate_thread(self) -> None:
        """discord.py retries a 5xx internally. When Discord created the thread before answering 502, the retry
        creates a second one; main posts the marker only in the second, so the first is an unmarked orphan."""
        fake = FakeDiscord(latency=0.002)
        fake.faults.append(Fault("POST", "/channels/{channel}/threads", status=502, applied=True))
        hello = hello_for("fresh")
        async with scenario(fake) as running, aiohttp.ClientSession() as http:
            websocket = await running.connect(http)
            await websocket.send_json(hello)
            await websocket.receive_json(timeout=15)
            await websocket.close()

        self.assertEqual(len(fake.threads), 1, "creating the session's thread left more than one thread")

    async def test_a_first_deploy_sweep_does_not_archive_a_session_about_to_reconnect(self) -> None:
        """Right after a restart, main's first sweep archives every unbound session thread, including those of
        sessions whose clients are still waiting out their reconnect delay (no store record protects them yet)."""
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("returning")
        thread = fake.add_thread("returning", marker=marker(hello), members={BOT_ID})
        with patch.object(bridge_module, "STARTUP_RECONNECT_GRACE_SECONDS", 0.05):
            async with scenario(fake) as running, aiohttp.ClientSession() as http:
                sweep = asyncio.create_task(running.bridge.cleanup_stale_sessions())
                try:
                    await asyncio.sleep(0.6)  # The client's reconnect delay after the server restart.
                    websocket = await running.connect(http)
                    await websocket.send_json(hello)
                    await websocket.receive_json(timeout=10)
                    await websocket.close()
                finally:
                    sweep.cancel()
                    await asyncio.gather(sweep, return_exceptions=True)

        self.assertNotIn(thread.id, running.archived, "the startup sweep archived a thread whose session was reconnecting")
