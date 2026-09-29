from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
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
from discord_blue.doodads.agent_session.protocol import (
    APPROVAL_COMMAND_DISPLAY_LIMIT,
    REMOTE_ACTIONS,
    SERVER_FEATURES,
    RemoteApprovalRequest,
    SessionHello,
)

Json = dict[str, Any]
TOKEN = "test-token"
RESPONSES = ("command_ack", "command_reject", "approval_decision_ack", "approval_decision_reject")


def status(thread_id: str, kind: str) -> Json:
    return {"method": "thread/status/changed", "params": {"threadId": thread_id, "status": {"type": kind}}}


def thread(thread_id: str, **fields: object) -> Json:
    base: Json = {"id": thread_id, "cwd": "/work/project", "gitInfo": {"branch": "fix/login"}, "preview": "Fix the login bug"}
    return {**base, "status": {"type": "idle"}, **fields}


CURRENT_FEATURES = sorted(SERVER_FEATURES)


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

    def __init__(self, features: list[str] | None = None) -> None:
        # None acknowledges like a server that predates hello_ack features.
        self.features = features
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
                ack: Json = {"type": "hello_ack", "thread_id": 1}
                await websocket.send_json(ack if self.features is None else {**ack, "features": self.features})
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
async def running_bridge(
    rpc: FakeRpc, features: list[str] | None = CURRENT_FEATURES
) -> AsyncIterator[tuple[CodexBridge, FakeDiscordBlue]]:
    discord = FakeDiscordBlue(features)
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
    async def test_opens_sessions_for_live_root_threads_and_joins_only_busy_ones(self) -> None:
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
            thread("busy", status={"type": "active", "activeFlags": []}),
            latest_turn=answered,
        )
        async with running_bridge(rpc) as (_bridge, discord):
            hellos = {h["session_id"]: h for h in [await discord.next("hello") for _ in range(2)]}

        # Joining sends no config overrides; an idle thread is mirrored without subscribing.
        self.assertEqual(rpc.called("thread/resume"), [{"threadId": "busy", "excludeTurns": True}])
        parsed = SessionHello.from_payload(hellos["root"])
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

    async def test_an_approval_carries_codex_command_exactly_as_the_shell_runs_it(self) -> None:
        command_line = 'echo "$(git rev-parse HEAD)" | tee head.txt'
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            params = {"threadId": "root", "turnId": "t1", "itemId": "item-1", "command": command_line, "cwd": "/work"}
            await bridge.dispatch({"id": 7, "method": "item/commandExecution/requestApproval", "params": params})
            approval = RemoteApprovalRequest.from_payload(await discord.next("approval_request"))

        # Discord shows command_text; servers that predate it still get an argv.
        self.assertEqual((approval.command_text, approval.command), (command_line, shlex.split(command_line)))

    async def test_a_server_that_does_not_list_command_text_gets_no_approvals(self) -> None:
        # An older server would show shlex.join of the argv: longer, re-quoted, possibly with a fence.
        rpc = FakeRpc(thread("root"))
        params = {"threadId": "root", "turnId": "t1", "itemId": "item-1", "command": "git status", "cwd": "/work"}
        async with running_bridge(rpc, features=None) as (bridge, discord):
            # Raised before the server answered hello, then after.
            await bridge.dispatch({"id": 7, "method": "item/commandExecution/requestApproval", "params": params})
            await discord.next("hello")
            await bridge.dispatch({"id": 8, "method": "item/commandExecution/requestApproval", "params": params})
            events = [await discord.next() for _ in range(2)]
            self.assertTrue(discord.received.empty())

        waiting = ("status_changed", "Waiting on a decision in the Codex TUI")
        self.assertEqual([(event["type"], event["message"]) for event in events], [waiting, waiting])
        self.assertEqual(rpc.responses, [])

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

    async def test_pending_prompts_are_sent_after_queued_status_so_the_server_keeps_them(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            # Both events queue before the session connects, as they do across a Discord reconnect.
            params = {"threadId": "root", "turnId": "t1", "itemId": "call-1", "questions": [{"id": "q", "question": "?"}]}
            await bridge.dispatch({"method": "turn/started", "params": {"threadId": "root", "turn": {"id": "t1"}}})
            await bridge.dispatch({"id": "req-1", "method": "item/tool/requestUserInput", "params": params})
            await discord.next("hello")
            events = [await discord.next() for _ in range(2)]

        self.assertEqual([e["type"] for e in events], ["status_changed", "request_user_input"])

    async def test_bridge_joins_while_a_turn_runs_and_catches_up_on_what_it_missed(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            typed = {"type": "userMessage", "id": "u1", "clientId": None, "content": [{"type": "text", "text": "typed in the TUI"}]}
            rpc.latest_turn = {"id": "t1", "status": "inProgress", "items": [typed]}
            await bridge.dispatch(status("root", "active"))
            # Notifications that also arrive after the join are not mirrored twice.
            await bridge.dispatch({"method": "turn/started", "params": {"threadId": "root", "turn": {"id": "t1"}}})
            await bridge.dispatch({"method": "item/completed", "params": {"threadId": "root", "turnId": "t1", "item": typed}})
            answer = {"type": "agentMessage", "id": "a1", "phase": "final_answer", "text": "Fixed it."}
            done = {"threadId": "root", "turn": {"id": "t1", "status": "completed", "items": [answer]}}
            await bridge.dispatch({"method": "turn/completed", "params": done})
            await bridge.dispatch({"method": "turn/completed", "params": done})
            events = [await discord.next() for _ in range(3)]
            self.assertTrue(discord.received.empty())

            await bridge.dispatch(status("root", "idle"))
            await bridge.dispatch(status("root", "active"))

        self.assertEqual(
            [(e["type"], e["message"], e.get("assistant_message")) for e in events],
            [
                ("status_changed", "Turn started", None),
                ("user_message", "typed in the TUI", None),
                ("turn_complete", TURN_DONE, "Fixed it."),
            ],
        )
        membership = [name for name, _ in rpc.calls if name in ("thread/resume", "thread/unsubscribe")]
        self.assertEqual(membership, ["thread/resume", "thread/unsubscribe", "thread/resume"])

    async def test_a_turn_that_finished_before_the_join_is_still_reported(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            answer = {"type": "agentMessage", "id": "a1", "phase": "final_answer", "text": "Quick one."}
            rpc.latest_turn = {"id": "t1", "status": "completed", "items": [answer]}
            await bridge.dispatch(status("root", "active"))
            await bridge.dispatch(status("root", "idle"))
            done = await discord.next("turn_complete")

        self.assertEqual(done["assistant_message"], "Quick one.")
        # No turn/completed will follow, so the bridge must not stay joined and keep the thread loaded.
        self.assertEqual(rpc.called("thread/unsubscribe"), [{"threadId": "root"}])

    async def test_a_pending_prompt_keeps_the_bridge_joined_and_a_replay_is_not_repeated(self) -> None:
        rpc = FakeRpc(thread("root", status={"type": "active", "activeFlags": []}))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            params = {"threadId": "root", "turnId": "t1", "itemId": "item-1", "command": "make test"}
            await bridge.dispatch({"method": "turn/started", "params": {"threadId": "root", "turn": {"id": "t1"}}})
            await discord.next("status_changed")
            await bridge.dispatch({"id": 7, "method": "item/commandExecution/requestApproval", "params": params})
            await bridge.dispatch({"id": 7, "method": "item/commandExecution/requestApproval", "params": params})
            await bridge.sessions["root"].release()
            self.assertEqual(rpc.called("thread/unsubscribe"), [])

            done = {"threadId": "root", "turn": {"id": "t1", "status": "interrupted"}}
            await bridge.dispatch({"method": "turn/completed", "params": done})
            events = [await discord.next() for _ in range(3)]

        self.assertEqual([e["type"] for e in events], ["approval_request", "approval_resolved", "status_changed"])
        self.assertEqual(rpc.called("thread/unsubscribe"), [{"threadId": "root"}])

    async def test_discord_reply_to_an_idle_thread_joins_before_starting_the_turn(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            session = bridge.sessions["root"]
            self.assertEqual((await discord.control(command(session, "c1", "reply", text="go on")))["type"], "command_ack")

            rpc.threads["root"]["status"] = {"type": "notLoaded"}
            await session.release()
            rejected = await discord.control(command(session, "c2", "reply", text="again"))

        self.assertEqual(rejected["reason"], "This Codex thread has closed; reopen it in the Codex TUI.")
        calls = [name for name, _ in rpc.calls if name != "thread/loaded/list"]
        self.assertEqual(
            calls[calls.index("thread/resume") - 1 :],
            ["thread/read", "thread/resume", "thread/turns/list", "turn/start", "thread/unsubscribe", "thread/read"],
        )

    async def test_the_discord_session_ends_when_stock_unloads_the_thread(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            await bridge.dispatch(status("root", "notLoaded"))
            self.assertEqual(bridge.sessions, {})
            async with asyncio.timeout(5):
                while not discord.sockets[-1].closed:
                    await asyncio.sleep(0.01)


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

    def test_values_must_have_their_toml_type(self) -> None:
        url = 'server_url = "ws://discord-blue:8787/agent-session/connect"'
        for body, error in (
            (f'{url}\nallow_insecure_ws = "false"', "allow_insecure_ws must be true or false"),
            (f"{url}\nallow_insecure_ws = 1", "allow_insecure_ws must be true or false"),
            ("server_url = 5", "server_url must be a string"),
            (f"{url}\nallow_insecure_ws = true\nhost_label = true", "host_label must be a string"),
            (f"{url}\nallow_insecure_wss = true", "unknown config keys: allow_insecure_wss"),
        ):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, error):
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
