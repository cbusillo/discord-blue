"""End-to-end check against a disposable stock Codex app-server (opt-in).

Set CODEX_BIN to a stock ``codex`` binary to run it on macOS. It never touches the
live daemon: the server gets a temporary home, synthetic auth and a fake model.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.codex_bridge.rpc import AppServerClient
from discord_blue.codex_bridge.session import ThreadSession
from tests.stock_codex import ASK_QUESTIONS, SANDBOX_POLICY, UNLOAD_DELAY_SECONDS, StockCodex
from discord_blue.doodads.agent_session.protocol import SERVER_FEATURES
from tests.fakes_discord_blue import TOKEN, FakeDiscordBlue

Json = dict[str, Any]
CODEX_BIN = os.environ.get("CODEX_BIN", "")
NATIVE_MODEL = "gpt-5.4-mini"


@asynccontextmanager
async def native_tui(codex: StockCodex, *, remote: bool) -> AsyncIterator[asyncio.subprocess.Process]:
    """A real TUI using only disposable homes and the loopback fake model."""
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    os.set_blocking(master, False)
    output = bytearray()
    loop = asyncio.get_running_loop()

    def drain() -> None:
        try:
            data = os.read(master, 65536)
            output.extend(data)
            if b"\x1b[6n" in data:
                os.write(master, b"\x1b[1;1R")
        except (BlockingIOError, OSError):
            pass

    loop.add_reader(master, drain)
    args = ["--remote", "unix://"] if remote else []
    env = {**codex.env, "TERM": "xterm-256color"}

    def terminal_session() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/sandbox-exec",
            "-p",
            SANDBOX_POLICY,
            codex.codex_bin,
            *args,
            "-m",
            NATIVE_MODEL,
            "-c",
            "model_reasoning_effort=medium",
            "--",
            "Mirror native TUI launch",
            cwd=codex.work,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=terminal_session,
        )
        try:
            yield process
        except TimeoutError as exc:
            raise AssertionError(f"Native TUI did not run (exit={process.returncode}): {output[-8000:]!r}") from exc
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        loop.remove_reader(master)
        os.close(master)
        os.close(slave)


class TuiStandIn:
    """A second client that owns the thread, like the native TUI; it never answers requests."""

    def __init__(self, socket_path: Path) -> None:
        self.rpc = AppServerClient(socket_path, queue_size=10_000)
        self.notes: list[Json] = []
        self.drainer: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.rpc.start()
        await self.rpc.initialize({"name": "tui_stand_in", "version": "0"})
        self.drainer = asyncio.create_task(self.drain())

    async def drain(self) -> None:
        while True:
            self.notes.append(await self.rpc.receive())

    async def turn(self, thread_id: str, text: str, **extra: object) -> None:
        mark = len(self.notes)
        await self.rpc.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}], **extra})
        await self.wait_for("turn/completed", mark)

    async def wait_for(self, method: str, mark: int = 0) -> Json:
        async with asyncio.timeout(30):
            while True:
                for note in self.notes[mark:]:
                    if note.get("method") == method:
                        return note
                await asyncio.sleep(0.05)


class GatedBridge(CodexBridge):
    """The real bridge, with a gate that can hold back joins so a request is raised while it is away."""

    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self.gate = asyncio.Event()
        self.gate.set()

    async def subscribe(self, session: ThreadSession) -> None:
        await self.gate.wait()
        await super().subscribe(session)


async def eventually(condition: Callable[[], bool], seconds: float = 30) -> None:
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.05)


@unittest.skipUnless(CODEX_BIN and sys.platform == "darwin", "set CODEX_BIN to a stock codex binary (macOS sandbox-exec)")
class StockAppServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_slice_two_approvals_and_new_session(self) -> None:
        discord = FakeDiscordBlue(sorted(SERVER_FEATURES))
        app = web.Application()
        app.router.add_get("/agent-session/connect", discord.connect)
        async with StockCodex(CODEX_BIN) as codex, TestServer(app) as server:
            tui = TuiStandIn(codex.socket_path)
            await tui.start()
            thread_id = (await tui.rpc.request("thread/start", {"cwd": str(codex.work)}))["thread"]["id"]
            await tui.turn(thread_id, "initial prompt")
            config = BridgeConfig(f"ws://127.0.0.1:{server.port}/agent-session/connect", TOKEN, codex.socket_path, "test")
            bridge = CodexBridge(config)
            running = asyncio.create_task(bridge.run())
            try:
                hello = await discord.next("hello")
                ids = {"session_id": thread_id, "session_epoch": hello["session_epoch"]}
                for action, kind, verdict in (
                    ("PATCH", "file_change", "denied"),
                    ("PATCH", "file_change", "approved"),
                    ("PERMISSIONS", "permissions", "denied"),
                    ("PERMISSIONS", "permissions", "approved"),
                ):
                    mark = len(tui.notes)
                    turn = asyncio.create_task(tui.turn(thread_id, action))
                    approval = await discord.next("approval_request", timeout=30)
                    self.assertEqual(approval["approval_kind"], kind)
                    self.assertIn("patched.txt" if kind == "file_change" else "network", approval["content_text"])
                    decision = {"type": "approval_decision", "approval_id": approval["approval_id"], "decision": verdict, **ids}
                    self.assertEqual((await discord.control(decision))["type"], "approval_decision_ack")
                    await turn
                    await tui.wait_for("serverRequest/resolved", mark)
                    if kind == "file_change" and verdict == "denied":
                        self.assertFalse((codex.work / "patched.txt").exists())
                    if kind == "permissions":
                        granted = json.loads(codex.tool_outputs[-1]["output"])["permissions"]
                        self.assertEqual(bool((granted.get("network") or {}).get("enabled")), verdict == "approved")
                self.assertEqual((codex.work / "patched.txt").read_text(), "approved patch\n")
                new = {"type": "command", "command_id": "new", "kind": "new_session", **ids}
                response = await discord.control(new)
                self.assertEqual(response["type"], "command_ack", repr(response))
                created = await discord.next("hello", timeout=30)
                self.assertNotEqual(created["session_id"], thread_id)
                loaded = (await tui.rpc.request("thread/loaded/list", {}))["data"]
                self.assertIn(created["session_id"], loaded)
                readback = (await tui.rpc.request("thread/read", {"threadId": created["session_id"]}))["thread"]
                self.assertEqual(readback["cwd"], str(codex.work))
                reply = {
                    "type": "command",
                    "command_id": "reply-new",
                    "kind": "reply",
                    "text": "new session reply",
                    "session_id": created["session_id"],
                    "session_epoch": created["session_epoch"],
                }
                self.assertEqual((await discord.control(reply))["type"], "command_ack")
                while True:
                    done = await discord.next("turn_complete", timeout=30)
                    if done["session_id"] == created["session_id"]:
                        self.assertIn("new session reply", done["assistant_message"])
                        break
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                if tui.drainer is not None:
                    tui.drainer.cancel()
                await tui.rpc.close()
                await discord.close()

    async def test_explicit_remote_mirrors_the_native_tui_with_reasoning_override(self) -> None:
        discord = FakeDiscordBlue(sorted(SERVER_FEATURES))
        app = web.Application()
        app.router.add_get("/agent-session/connect", discord.connect)
        async with StockCodex(CODEX_BIN) as codex, TestServer(app) as server:
            with (codex.home / "config.toml").open("a") as config_file:
                config_file.write(f'\n[projects."{codex.work.resolve()}"]\ntrust_level = "trusted"\n')
            config = BridgeConfig(
                f"ws://127.0.0.1:{server.port}/agent-session/connect",
                TOKEN,
                codex.socket_path,
                "test",
            )
            async with AppServerClient(codex.socket_path) as rpc:
                await rpc.initialize()
                # The same TUI flags without --remote select an embedded runtime, not this daemon.
                async with native_tui(codex, remote=False):
                    await eventually(lambda: codex.calls > 0)
                    self.assertEqual((await rpc.request("thread/loaded/list", {}))["data"], [])
            running = asyncio.create_task(CodexBridge(config).run())
            try:
                async with native_tui(codex, remote=True):
                    hello = await discord.next("hello", timeout=30)
                    self.assertEqual(hello["harness"], "codex")
                    if not hello.get("assistant_message"):
                        done = await discord.next("turn_complete", timeout=30)
                        self.assertIn("Mirror native TUI launch", done["assistant_message"])
                    else:
                        self.assertIn("Mirror native TUI launch", hello["assistant_message"])
                    async with AppServerClient(codex.socket_path) as readback:
                        await readback.initialize()
                        loaded = (await readback.request("thread/read", {"threadId": hello["session_id"]}))["thread"]
                        self.assertEqual(loaded["reasoningEffort"], "medium")
                        self.assertEqual(loaded["model"], NATIVE_MODEL)
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                await discord.close()

    async def test_bridge_mirrors_and_drives_a_thread_owned_by_another_client(self) -> None:
        discord = FakeDiscordBlue(sorted(SERVER_FEATURES))
        app = web.Application()
        app.router.add_get("/agent-session/connect", discord.connect)
        async with StockCodex(CODEX_BIN) as codex, TestServer(app) as server:
            tui = TuiStandIn(codex.socket_path)
            await tui.start()
            thread_id = (await tui.rpc.request("thread/start", {"cwd": str(codex.work)}))["thread"]["id"]
            await tui.turn(thread_id, "hello")
            goal = await tui.rpc.request("thread/goal/set", {"threadId": thread_id, "objective": "ship it", "status": "paused"})

            url = f"ws://127.0.0.1:{server.port}/agent-session/connect"
            config = BridgeConfig(server_url=url, token=TOKEN, socket_path=codex.socket_path, host_label="Codex on test")
            bridge = GatedBridge(config)
            running = asyncio.create_task(bridge.run())
            try:
                hello = await discord.next("hello")
                # The one-word first prompt names nothing, so the thread is named after the repo alone.
                self.assertEqual((hello["session_id"], hello.get("title"), hello["harness"]), (thread_id, None, "codex"))
                self.assertIn("hello", hello["assistant_message"])
                session = bridge.sessions[thread_id]
                # An idle thread is mirrored without joining it.
                self.assertFalse(session.subscribed)

                # A Discord reply to the idle thread joins it, then runs the turn.
                mark = len(tui.notes)
                ids = {"session_id": thread_id, "session_epoch": hello["session_epoch"]}
                reply = {"type": "command", "command_id": "c1", "kind": "reply", "text": "reply from phone", **ids}
                self.assertEqual((await discord.control(reply))["type"], "command_ack")
                done = await discord.next("turn_complete", "user_message")
                self.assertIn("reply from phone", done["assistant_message"])
                await eventually(lambda: not session.subscribed)
                # Joining replays a read-only goal snapshot; it must not clear the owner's goal.
                await tui.wait_for("thread/goal/updated", mark)
                self.assertNotIn("thread/goal/cleared", [n.get("method") for n in tui.notes[mark:]])
                self.assertEqual(await tui.rpc.request("thread/goal/get", {"threadId": thread_id}), goal)

                # An approval raised while the bridge is away is replayed to it when it joins.
                bridge.gate.clear()
                mark = len(tui.notes)
                turn = asyncio.create_task(tui.turn(thread_id, "RUN the command"))
                await tui.wait_for("item/commandExecution/requestApproval", mark)
                self.assertFalse(session.subscribed)
                bridge.gate.set()
                approval = await discord.next("approval_request")
                # Stock sends one shell string; its argv is the shell wrapper the command runs under.
                self.assertEqual(approval["command"][-1], "touch approved.txt")
                decision = {"type": "approval_decision", "approval_id": approval["approval_id"], "decision": "approved", **ids}
                self.assertEqual((await discord.control(decision))["type"], "approval_decision_ack")
                await turn
                self.assertTrue((codex.work / "approved.txt").exists())

                plan = {"mode": "plan", "settings": {"model": "gpt-5.4", "developer_instructions": None}}
                turn = asyncio.create_task(tui.turn(thread_id, "ASK me", collaborationMode=plan))
                prompt = await discord.next("request_user_input")
                self.assertEqual([q["id"] for q in prompt["questions"]], [q["id"] for q in ASK_QUESTIONS])
                response = {"answers": {"pick": {"answers": ["Beta"]}}}
                answer = {"type": "command", "command_id": "c2", "kind": "request_user_input_response", **ids}
                answer |= {"call_id": prompt["call_id"], "turn_id": prompt["turn_id"], "response": response}
                self.assertEqual((await discord.control(answer))["type"], "command_ack")
                await turn

                await tui.turn(thread_id, "typed locally")
                self.assertEqual((await discord.next("user_message"))["message"], "typed locally")

                turn = asyncio.create_task(tui.turn(thread_id, "SLOW work"))
                await discord.next("status_changed")
                pause = {"type": "command", "command_id": "c3", "kind": "pause_current_turn", **ids}
                self.assertEqual((await discord.control(pause))["type"], "command_ack")
                self.assertEqual((await discord.next("status_changed", "turn_complete"))["message"], "Turn aborted")
                await turn

                # Closing the TUI lets stock unload the thread, and the bridge ends the Discord session.
                await eventually(lambda: not session.subscribed)
                if tui.drainer is not None:
                    tui.drainer.cancel()
                await tui.rpc.close()
                await eventually(lambda: discord.sockets[-1].closed, UNLOAD_DELAY_SECONDS + 15)
                self.assertEqual(bridge.sessions, {})
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                if tui.drainer is not None:
                    tui.drainer.cancel()
                await tui.rpc.close()
                await discord.close()
