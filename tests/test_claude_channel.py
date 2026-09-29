from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from discord_blue.claude_channel.__main__ import run_channel
from discord_blue.claude_channel.mcp import METHOD_NOT_FOUND, PROTOCOL_VERSIONS
from discord_blue.claude_channel.session import CAPABILITIES, CHANNEL, PERMISSION_REQUEST, WAITING_LOCALLY, Identity, waiting_message
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.doodads.agent_session.protocol import REMOTE_ACTIONS, SessionHello
from tests.fakes_discord_blue import TOKEN, FakeDiscordBlue

Json = dict[str, Any]
IDENTITY = Identity(session_id="433c3568-3ee5-46df-8728-9756255b9028", cwd="/work/project", branch="fix/login", pid=4242)
# The permission request Claude Code 2.1.284 sent in the spike, verbatim.
SPIKE_REQUEST = {
    "request_id": "poeyw",
    "tool_name": "Bash",
    "description": "Create relay-test.txt file",
    "input_preview": '{ "command": "touch relay-test.txt", "description": "Create relay-test.txt file" }',
}


class FakeClaudeCode:
    """Drives the channel server over stdio with the JSON-RPC messages Claude Code sends."""

    def __init__(self) -> None:
        self.stdin = asyncio.StreamReader()
        self.received: list[Json] = []
        self.arrived = asyncio.Event()
        self.next_id = 0

    # The server's stdout.
    def write(self, data: bytes) -> None:
        self.received.extend(json.loads(line) for line in data.splitlines())
        self.arrived.set()

    async def drain(self) -> None:
        return None

    def send(self, message: Json) -> None:
        self.stdin.feed_data(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")

    async def take(self, matches: Callable[[Json], bool]) -> Json:
        async with asyncio.timeout(5):
            while True:
                for message in self.received:
                    if matches(message):
                        self.received.remove(message)
                        return message
                self.arrived.clear()
                await self.arrived.wait()

    async def request(self, method: str, params: Json | None = None) -> Json:
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, **({"params": params} if params is not None else {})})
        return await self.take(lambda message: message.get("id") == request_id)

    async def notification(self, method: str) -> Json:
        return (await self.take(lambda message: message.get("method") == method))["params"]

    async def initialize(self) -> Json:
        # The initialize request Claude Code 2.1.284 sent in the live check, without its description fields.
        params = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"roots": {"listChanged": True}, "elicitation": {"form": {}, "url": {}}},
            "clientInfo": {"name": "claude-code", "title": "Claude Code", "version": "2.1.284"},
        }
        response = await self.request("initialize", params)
        self.send({"method": "notifications/initialized"})
        return response

    async def settle(self) -> list[Json]:
        """Everything the server wrote before it answered a ping sent now."""
        await self.request("ping")
        return list(self.received)


@asynccontextmanager
async def running_channel(
    *, configured: bool = True, identity: Identity = IDENTITY
) -> AsyncIterator[tuple[FakeClaudeCode, FakeDiscordBlue]]:
    discord = FakeDiscordBlue()
    app = web.Application()
    app.router.add_get("/agent-session/connect", discord.connect)
    async with TestServer(app, host="127.0.0.1") as server:
        url = f"ws://127.0.0.1:{server.port}/agent-session/connect"
        config = BridgeConfig(
            server_url=url, token=TOKEN, socket_path=Path("/unused"), host_label="Claude Code on test", reconnect_seconds=0.05
        )
        claude = FakeClaudeCode()
        channel = asyncio.create_task(run_channel(claude.stdin, claude, config if configured else None, identity))
        try:
            yield claude, discord
        finally:
            claude.stdin.feed_eof()
            await asyncio.wait_for(channel, 5)


def command(hello: Json, command_id: str, kind: str, **fields: object) -> Json:
    identity = {"session_id": hello["session_id"], "session_epoch": hello["session_epoch"]}
    return {"type": "command", "command_id": command_id, "kind": kind, **identity, **fields}


def decision(hello: Json, approval_id: str, verdict: str) -> Json:
    identity = {"session_id": hello["session_id"], "session_epoch": hello["session_epoch"]}
    return {"type": "approval_decision", "approval_id": approval_id, "decision": verdict, **identity}


class ClaudeChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_registers_a_relaying_channel_and_opens_the_session(self) -> None:
        async with running_channel() as (claude, discord):
            initialized = await claude.initialize()
            hello = SessionHello.from_payload(await discord.next("hello"))
            newest = (await claude.request("initialize", {"protocolVersion": "2026-07-28"}))["result"]["protocolVersion"]
            unknown = await claude.request("resources/list")

        result = initialized["result"]
        channel: Json = {"claude/channel": {}, "claude/channel/permission": {}}
        self.assertEqual(result["capabilities"], {"experimental": channel, "tools": {}})
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        # Claude Code registers no channel that negotiated the 2026-07-28 revision.
        self.assertIn(newest, PROTOCOL_VERSIONS)
        self.assertLess(newest, "2026-07-28")
        self.assertEqual(unknown["error"]["code"], METHOD_NOT_FOUND)
        self.assertEqual(
            (hello.session_id, hello.cwd, hello.branch, hello.pid, hello.host_label, hello.capabilities),
            (IDENTITY.session_id, IDENTITY.cwd, IDENTITY.branch, IDENTITY.pid, "Claude Code on test", frozenset(CAPABILITIES)),
        )
        self.assertLess(frozenset(CAPABILITIES), REMOTE_ACTIONS)

    async def test_a_discord_reply_is_injected_once_and_other_controls_are_refused(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            reply = command(hello, "cmd-1", "reply", text="try again")
            self.assertEqual((await discord.control(reply))["type"], "command_ack")
            injected = await claude.notification(CHANNEL)
            self.assertEqual((await discord.control(reply))["type"], "command_ack")
            for refused in (
                command(hello, "cmd-2", "pause_current_turn"),
                command(hello, "cmd-3", "new_session"),
                command(hello, "cmd-4", "reply", text=""),
                {**command(hello, "cmd-5", "reply", text="old"), "session_epoch": "stale"},
            ):
                self.assertEqual((await discord.control(refused))["type"], "command_reject")
            leftover = await claude.settle()

        self.assertEqual(injected, {"content": "try again", "meta": {"command_id": "cmd-1"}})
        self.assertEqual(leftover, [])

    async def test_a_permission_prompt_is_announced_but_only_the_terminal_can_answer_it(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            claude.send({"method": PERMISSION_REQUEST, "params": SPIKE_REQUEST})
            waiting = await discord.next()
            refused = await discord.control(decision(hello, "poeyw", "approved"))
            leftover = await claude.settle()

        self.assertEqual((waiting["type"], waiting["message"]), ("status_changed", f"{WAITING_LOCALLY} (Bash)"))
        # Nothing from the preview or description reaches Discord.
        self.assertNotIn("relay-test", json.dumps(waiting))
        self.assertNotIn("approval_decision", CAPABILITIES)
        self.assertEqual(refused["type"], "approval_decision_reject")
        # No permission verdict ever goes back to Claude Code.
        self.assertEqual(leftover, [])

    async def test_ending_the_claude_session_closes_the_discord_session(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            claude.stdin.feed_eof()
            async with asyncio.timeout(5):
                while not discord.sockets[-1].closed:
                    await asyncio.sleep(0.01)

    async def test_without_a_config_the_channel_answers_but_relays_nothing(self) -> None:
        async with running_channel(configured=False) as (claude, discord):
            result = (await claude.initialize())["result"]

        self.assertEqual(result["capabilities"], {"experimental": {"claude/channel": {}}, "tools": {}})
        self.assertEqual(discord.sockets, [])


class WaitingMessageTests(unittest.TestCase):
    def test_only_a_plain_tool_name_from_the_request_is_shown(self) -> None:
        for tool_name, shown in (
            ("Bash", "Bash"),
            ("mcp__github__create_issue", "mcp__github__create_issue"),
            ("Bash`\n```\n@everyone", "a tool"),
            ("", "a tool"),
            (None, "a tool"),
        ):
            with self.subTest(tool_name=tool_name):
                self.assertEqual(waiting_message({"tool_name": tool_name}), f"{WAITING_LOCALLY} ({shown})")
