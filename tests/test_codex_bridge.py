from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.config import BridgeConfig, load_config
from discord_blue.codex_bridge.session import CAPABILITIES, TURN_DONE, ThreadSession
from discord_blue.doodads.agent_session.protocol import APPROVAL_COMMAND_DISPLAY_LIMIT, REMOTE_ACTIONS, SessionHello

Json = dict[str, Any]
TOKEN = "test-token"
RESPONSES = ("command_ack", "command_reject", "approval_decision_ack", "approval_decision_reject")


def thread(thread_id: str, **fields: object) -> Json:
    base: Json = {"id": thread_id, "cwd": "/work/project", "gitInfo": {"branch": "fix/login"}, "preview": "Fix the login bug"}
    return {**base, "status": {"type": "idle"}, **fields}


class FakeRpc:
    def __init__(self, *threads: Json, latest_turn: Json | None = None) -> None:
        self.threads = {t["id"]: t for t in threads}
        self.latest_turn = latest_turn
        self.calls: list[tuple[str, Json | None]] = []
        self.responses: list[tuple[object, Json | None]] = []

    async def request(self, method: str, params: Json | None = None) -> Json:
        self.calls.append((method, params))
        if method == "thread/loaded/list":
            return {"data": list(self.threads)}
        if method == "thread/read":
            return {"thread": self.threads[str((params or {})["threadId"])]}
        if method == "thread/turns/list":
            return {"data": [self.latest_turn] if self.latest_turn else []}
        return {}

    async def respond(self, request_id: object, result: Json | None = None) -> None:
        self.responses.append((request_id, result))

    def called(self, method: str) -> list[Json | None]:
        return [params for name, params in self.calls if name == method]


class FakeDiscordBlue:
    """Stands in for the deployed agent-session server: acks hello and records events."""

    def __init__(self) -> None:
        self.received: asyncio.Queue[Json] = asyncio.Queue()
        self.sockets: list[web.WebSocketResponse] = []

    async def connect(self, request: web.Request) -> web.WebSocketResponse:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        self.sockets.append(websocket)
        async for frame in websocket:
            message = frame.json()
            if message["type"] == "hello":
                await websocket.send_json({"type": "hello_ack", "thread_id": 1})
            if message["type"] != "heartbeat":
                await self.received.put(message)
        return websocket

    async def close(self) -> None:
        for websocket in self.sockets:
            await websocket.close()

    async def next(self, *kinds: str) -> Json:
        while True:
            message = await asyncio.wait_for(self.received.get(), timeout=5)
            if not kinds or message["type"] in kinds:
                return message

    async def control(self, message: Json) -> Json:
        await self.sockets[-1].send_json(message)
        return await self.next(*RESPONSES)


@asynccontextmanager
async def running_bridge(rpc: FakeRpc) -> AsyncIterator[tuple[CodexBridge, FakeDiscordBlue]]:
    discord = FakeDiscordBlue()
    app = web.Application()
    app.router.add_get("/agent-session/connect", discord.connect)
    async with TestServer(app, host="127.0.0.1") as server, aiohttp.ClientSession() as http:
        url = f"ws://127.0.0.1:{server.port}/agent-session/connect"
        config = BridgeConfig(
            server_url=url, token=TOKEN, socket_path=Path("/unused"), host_label="Codex on test", reconnect_seconds=0.05
        )
        bridge = CodexBridge(config)
        bridge.rpc, bridge.http = rpc, http
        try:
            await bridge.discover()
            yield bridge, discord
        finally:
            await bridge.detach_all()


def command(session: ThreadSession, command_id: str, kind: str, **fields: object) -> Json:
    return {
        "type": "command",
        "command_id": command_id,
        "session_id": session.thread_id,
        "session_epoch": session.epoch,
        "kind": kind,
        **fields,
    }


class CodexBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_joins_only_live_root_threads_without_config_overrides(self) -> None:
        answered = {
            "id": "turn-0",
            "status": "completed",
            "items": [{"type": "agentMessage", "text": "Done.", "phase": "final_answer"}],
        }
        rpc = FakeRpc(
            thread("root"),
            thread("child", parentThreadId="root"),
            thread("untitled", preview=""),
            thread("unloaded", status={"type": "notLoaded"}),
            latest_turn=answered,
        )
        async with running_bridge(rpc) as (_bridge, discord):
            hello = await discord.next("hello")

        self.assertEqual(rpc.called("thread/resume"), [{"threadId": "root", "excludeTurns": True}])
        parsed = SessionHello.from_payload(hello)
        self.assertEqual(
            (parsed.session_id, parsed.title, parsed.cwd, parsed.branch, parsed.assistant_message, parsed.capabilities),
            ("root", "Fix the login bug", "/work/project", "fix/login", "Done.", frozenset(CAPABILITIES)),
        )
        self.assertLess(frozenset(CAPABILITIES), REMOTE_ACTIONS)

    async def test_reply_runs_once_and_its_echo_is_not_mirrored(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            session = bridge.sessions["root"]
            reply = command(session, "cmd-1", "reply", text="try again")
            self.assertEqual((await discord.control(reply))["type"], "command_ack")
            self.assertEqual((await discord.control(reply))["type"], "command_ack")
            [params] = rpc.called("turn/start")
            assert params is not None
            self.assertEqual(params["input"], [{"type": "text", "text": "try again"}])

            echo = {"type": "userMessage", "clientId": params["clientUserMessageId"], "content": params["input"]}
            local = {"type": "userMessage", "clientId": None, "content": [{"type": "text", "text": "typed in the TUI"}]}
            for item in (echo, local):
                await bridge.dispatch({"method": "item/completed", "params": {"threadId": "root", "turnId": "t1", "item": item}})
            self.assertEqual((await discord.next("user_message"))["message"], "typed in the TUI")

    async def test_approval_is_answered_only_by_an_explicit_discord_decision(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            session = bridge.sessions["root"]
            params = {
                "threadId": "root",
                "turnId": "t1",
                "itemId": "item-1",
                "command": "git push origin 'my branch'",
                "cwd": "/work",
            }
            await bridge.dispatch({"id": 7, "method": "item/commandExecution/requestApproval", "params": params})
            await bridge.dispatch({"id": 8, "method": "item/fileChange/requestApproval", "params": {"threadId": "root"}})
            approval = await discord.next("approval_request")
            self.assertEqual(approval["command"], ["git", "push", "origin", "my branch"])
            self.assertEqual(rpc.responses, [])

            decision = {"type": "approval_decision", "approval_id": approval["approval_id"], "decision": "approved"}
            decision |= {"session_id": session.thread_id, "session_epoch": session.epoch}
            self.assertEqual((await discord.control(decision))["type"], "approval_decision_ack")
            self.assertEqual((await discord.control(decision))["type"], "approval_decision_reject")
            await bridge.dispatch({"method": "serverRequest/resolved", "params": {"threadId": "root", "requestId": 7}})

        self.assertEqual(rpc.responses, [(7, {"decision": "accept"})])

    async def test_approvals_discord_cannot_fully_show_stay_in_the_tui(self) -> None:
        base = {"threadId": "root", "turnId": "t1", "itemId": "item-1", "cwd": "/work"}
        fits = "x" * APPROVAL_COMMAND_DISPLAY_LIMIT
        tui_only = {
            "longer than Discord shows": {**base, "command": fits + "y"},
            "breaks out of the code fence": {**base, "command": "echo '```' [ls](https://x)"},
            "asks for more permissions": {**base, "command": "ls", "additionalPermissions": {"network": {"enabled": True}}},
            "asks for network access": {**base, "networkApprovalContext": {"host": "example.com", "protocol": "https"}},
            "carries an unknown field": {**base, "command": "ls", "sandboxOverride": "danger-full-access"},
            "writes to stdin": {**base, "command": "ls", "kind": "writeStdin"},
        }
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            for request_id, (case, params) in enumerate(tui_only.items()):
                with self.subTest(case):
                    await bridge.dispatch({"id": request_id, "method": "item/commandExecution/requestApproval", "params": params})
                    event = await discord.next()
                    self.assertEqual((event["type"], event["message"]), ("status_changed", "Waiting on a decision in the Codex TUI"))
            await bridge.dispatch({"id": 99, "method": "item/commandExecution/requestApproval", "params": {**base, "command": fits}})
            self.assertEqual((await discord.next())["command"], [fits])

        self.assertEqual(bridge.sessions, {})
        self.assertEqual(rpc.responses, [])

    async def test_prompt_answered_in_the_tui_is_retired_and_cannot_be_answered(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            session = bridge.sessions["root"]
            question = {"id": "q", "header": "Pick", "question": "Which?"}
            params = {"threadId": "root", "turnId": "t1", "itemId": "call-1", "questions": [question]}
            await bridge.dispatch({"id": "req-1", "method": "item/tool/requestUserInput", "params": params})
            prompt = await discord.next("request_user_input")
            self.assertEqual(prompt["questions"], [{**question, "isOther": False, "isSecret": False, "options": []}])

            await bridge.dispatch({"method": "serverRequest/resolved", "params": {"threadId": "root", "requestId": "req-1"}})
            resolved = await discord.next()
            self.assertEqual((resolved["type"], resolved["call_id"]), ("request_user_input_resolved", "call-1"))
            answer = command(
                session, "cmd-1", "request_user_input_response", call_id="call-1", turn_id="t1", response={"answers": {}}
            )
            self.assertEqual((await discord.control(answer))["type"], "command_reject")

        self.assertEqual(rpc.responses, [])

    async def test_user_input_answer_is_forwarded(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            session = bridge.sessions["root"]
            params = {
                "threadId": "root",
                "turnId": "t1",
                "itemId": "call-1",
                "questions": [{"id": "q", "header": "", "question": "?"}],
            }
            await bridge.dispatch({"id": "req-1", "method": "item/tool/requestUserInput", "params": params})
            await discord.next("request_user_input")
            answers = {"q": {"answers": ["Beta"]}}
            answer = command(
                session, "cmd-1", "request_user_input_response", call_id="call-1", turn_id="t1", response={"answers": answers}
            )
            self.assertEqual((await discord.control(answer))["type"], "command_ack")

        self.assertEqual(rpc.responses, [("req-1", {"answers": answers})])

    async def test_unsupported_stale_and_idle_controls_are_rejected(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            session = bridge.sessions["root"]
            for message in (
                command(session, "a", "new_session"),
                command(session, "b", "pause_current_turn"),
                {**command(session, "c", "reply", text="hi"), "session_epoch": "old"},
            ):
                self.assertEqual((await discord.control(message))["type"], "command_reject")

            await bridge.dispatch({"method": "turn/started", "params": {"threadId": "root", "turn": {"id": "t2"}}})
            self.assertEqual((await discord.control(command(session, "d", "pause_current_turn")))["type"], "command_ack")

        self.assertEqual([name for name, _ in rpc.calls if name.startswith("turn/")], ["turn/interrupt"])
        self.assertEqual(rpc.called("turn/interrupt"), [{"threadId": "root", "turnId": "t2"}])

    async def test_turn_events_mirror_status_and_final_answer(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            for turn_id, status in (("t1", "completed"), ("t2", "interrupted")):
                await bridge.dispatch({"method": "turn/started", "params": {"threadId": "root", "turn": {"id": turn_id}}})
                for phase, text in (("commentary", "Looking."), ("final_answer", "Fixed it.")):
                    item = {"type": "agentMessage", "phase": phase, "text": text}
                    await bridge.dispatch(
                        {"method": "item/completed", "params": {"threadId": "root", "turnId": turn_id, "item": item}}
                    )
                await bridge.dispatch(
                    {"method": "turn/completed", "params": {"threadId": "root", "turn": {"id": turn_id, "status": status}}}
                )
            events = [await discord.next() for _ in range(4)]

        self.assertEqual(
            [(e["type"], e["message"], e.get("assistant_message")) for e in events],
            [
                ("status_changed", "Turn started", None),
                ("turn_complete", TURN_DONE, "Fixed it."),
                ("status_changed", "Turn started", None),
                ("status_changed", "Turn aborted", None),
            ],
        )

    async def test_reconnect_resends_hello_and_each_pending_prompt_once(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            first = await discord.next("hello")
            params = {"threadId": "root", "turnId": "t1", "itemId": "item-1", "command": "make test"}
            await bridge.dispatch({"id": 1, "method": "item/commandExecution/requestApproval", "params": params})
            await discord.next("approval_request")
            await discord.sockets[-1].close()
            second = await discord.next("hello")
            replayed = await discord.next()

        self.assertEqual((second["session_id"], second["session_epoch"]), (first["session_id"], first["session_epoch"]))
        self.assertEqual((replayed["type"], replayed["command"]), ("approval_request", ["make", "test"]))
        self.assertTrue(discord.received.empty())

    async def test_long_idle_threads_are_released_until_active_again(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            bridge.sessions["root"].idle_since = time.monotonic() - bridge.config.idle_release_seconds - 1
            await bridge.release_idle()
            self.assertEqual((list(bridge.sessions), rpc.called("thread/unsubscribe")), ([], [{"threadId": "root"}]))

            await bridge.dispatch({"method": "thread/status/changed", "params": {"threadId": "root", "status": {"type": "idle"}}})
            self.assertEqual(list(bridge.sessions), [])
            await bridge.dispatch({"method": "thread/status/changed", "params": {"threadId": "root", "status": {"type": "active"}}})
            self.assertEqual(list(bridge.sessions), ["root"])


class ConfigTests(unittest.TestCase):
    def load(self, body: str, token: str | None = "t") -> BridgeConfig:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "bridge.toml"
            if token is not None:
                (Path(home) / "token").write_text(token)
                (Path(home) / "token").chmod(0o600)
                body += f'\ntoken_file = "{Path(home) / "token"}"'
            path.write_text(body)
            return load_config(path)

    def test_server_url_must_be_encrypted_unless_loopback_or_explicitly_trusted(self) -> None:
        self.assertEqual(self.load('server_url = "wss://bridge.example/agent-session/connect"').token, "t")
        self.load('server_url = "ws://127.0.0.1:8787/agent-session/connect"')
        self.load('server_url = "ws://discord-blue:8787/agent-session/connect"\nallow_insecure_ws = true')
        for body in (
            'server_url = "ws://discord-blue:8787/agent-session/connect"',
            'server_url = "wss://bridge.example/every-code/connect"',
            'server_url = "wss://user:pw@bridge.example/agent-session/connect"',
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.load(body)

    def test_token_must_be_present_and_private(self) -> None:
        url = 'server_url = "wss://bridge.example/agent-session/connect"'
        with self.assertRaisesRegex(ValueError, "token"):
            self.load(url, token="")
        with tempfile.TemporaryDirectory() as home:
            token = Path(home) / "token"
            token.write_text("t")
            os.chmod(token, 0o644)
            (Path(home) / "b.toml").write_text(f'{url}\ntoken_file = "{token}"')
            with self.assertRaisesRegex(ValueError, "readable"):
                load_config(Path(home) / "b.toml")
