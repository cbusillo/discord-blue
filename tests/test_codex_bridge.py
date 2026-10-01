from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import tempfile
import unittest
from unittest.mock import patch
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from itertools import pairwise
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from discord_blue.codex_bridge.__main__ import FAILURE_RESET_SECONDS, codex_home, run_bridge, run_bridges
from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.config import BridgeConfig, load_config, socket_for_home
from discord_blue.codex_bridge.session import CAPABILITIES, TURN_DONE, ThreadSession
from discord_blue.doodads.agent_session.protocol import (
    APPROVAL_COMMAND_DISPLAY_LIMIT,
    REMOTE_ACTIONS,
    SERVER_FEATURES,
    RemoteApprovalRequest,
    SessionHello,
)
from tests.fakes_discord_blue import TOKEN, FakeDiscordBlue

Json = dict[str, Any]


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

    async def test_codex_renames_and_substantial_prompts_retitle_the_thread(self) -> None:
        rpc = FakeRpc(thread("root", preview="Continue"))
        async with running_bridge(rpc) as (bridge, discord):
            hello = SessionHello.from_payload(await discord.next("hello"))
            typed = {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "Fix the flaky login test"}]}
            await bridge.dispatch({"method": "item/completed", "params": {"threadId": "root", "turnId": "t1", "item": typed}})
            for name in ("Login flake", None):
                await bridge.dispatch({"method": "thread/name/updated", "params": {"threadId": "root", "threadName": name}})
            titles = [(await discord.next("title_changed"))["title"] for _ in range(3)]

        # "Continue" names nothing, so the thread starts as the repo (and branch) alone.
        self.assertEqual((hello.harness, hello.title), ("codex", None))
        self.assertEqual(titles, ["Fix the flaky login test", "Login flake", "Fix the flaky login test"])

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
            [(e["type"], e.get("message") or e.get("title"), e.get("assistant_message")) for e in events],
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
            await discord.next("session_end")  # So Discord Blue closes the thread now, not after a grace period.
            async with asyncio.timeout(5):
                while not discord.sockets[-1].closed:
                    await asyncio.sleep(0.01)

    async def test_a_daemon_drop_does_not_end_the_discord_session(self) -> None:
        rpc = FakeRpc(thread("root"))
        async with running_bridge(rpc) as (bridge, discord):
            await discord.next("hello")
            await bridge.detach_all()
            async with asyncio.timeout(5):
                while not discord.sockets[-1].closed:
                    await asyncio.sleep(0.01)
            received = [discord.received.get_nowait()["type"] for _ in range(discord.received.qsize())]

        # Without session_end, Discord Blue keeps the thread through its grace period for the reconnect.
        self.assertNotIn("session_end", received)


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

    def test_default_transport_follows_account_home_not_shared_sqlite_home(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"CODEX_HOME": home, "CODEX_SQLITE_HOME": "/shared"}):
            config = self.load('server_url = "wss://bridge.example/agent-session/connect"')
            self.assertEqual(config.socket_path, socket_for_home(Path(home)))
            explicit = self.load('server_url = "wss://bridge.example/agent-session/connect"\nsocket_path = "/chosen.sock"')
            self.assertEqual(explicit.socket_path, Path("/chosen.sock"))

    def test_no_account_home_uses_default_transport(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": "", "CODEX_SQLITE_HOME": "/shared"}):
            self.assertEqual(
                self.load('server_url = "wss://bridge.example/agent-session/connect"').socket_path,
                socket_for_home(Path.home() / ".codex"),
            )

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
            (f"{url}\nallow_insecure_ws = true\nhello_timeout_seconds = true", "hello_timeout_seconds must be a number"),
            (f"{url}\nallow_insecure_ws = true\nhello_timeout_seconds = 0", "hello_timeout_seconds must be positive"),
        ):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, error):
                self.load(body)

    def test_hello_timeout_is_configurable(self) -> None:
        url = 'server_url = "wss://bridge.example/agent-session/connect"'
        default = self.load(url).hello_timeout_seconds
        self.assertEqual(self.load(f"{url}\nhello_timeout_seconds = 45").hello_timeout_seconds, 45)
        self.assertEqual(self.load(f"{url}\nhello_timeout_seconds = 12.5").hello_timeout_seconds, 12.5)
        self.assertGreaterEqual(default, 300)  # Outlasts a restart's attach queue (#148).

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


class MultiHomeTests(unittest.IsolatedAsyncioTestCase):
    def test_empty_home_is_rejected_instead_of_connecting_to_the_working_directory(self) -> None:
        for value in ("", "   "):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                codex_home(value)

    async def test_one_home_restarts_after_a_bad_reply_without_restarting_another(self) -> None:
        ready = asyncio.Event()
        starts: dict[Path, int] = {}
        labels: dict[Path, str] = {}

        class FakeBridge:
            def __init__(self, config: BridgeConfig) -> None:
                self.path = config.socket_path
                labels[self.path] = config.host_label

            async def run(self) -> None:
                starts[self.path] = starts.get(self.path, 0) + 1
                if "broken" in self.path.parts and starts[self.path] == 1:
                    raise KeyError("thread")  # A malformed daemon thread/read response.
                if sum(starts.values()) == 3:
                    ready.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as root:
            home = Path(root).resolve()
            config = BridgeConfig("ws://localhost/agent-session/connect", TOKEN, home / "unused", "test", reconnect_seconds=0.01)
            with patch("discord_blue.codex_bridge.__main__.CodexBridge", FakeBridge), self.assertLogs(level="ERROR"):
                task = asyncio.create_task(run_bridges(config, [home / "broken", home / "healthy"]))
                try:
                    await asyncio.wait_for(ready.wait(), 2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(starts[socket_for_home(home / "broken")], 2)
            self.assertEqual(starts[socket_for_home(home / "healthy")], 1)
            self.assertEqual(len(set(labels.values())), len(labels))
            self.assertTrue(all(str(home) not in label for label in labels.values()))

    async def test_persistent_failure_backs_off_and_logs_frames_without_provider_data(self) -> None:
        delays: list[float] = []
        provider_message = "synthetic-private-provider-data"

        class BrokenBridge:
            def __init__(self, _config: BridgeConfig) -> None:
                pass

            async def run(self) -> None:
                raise KeyError(provider_message)

        async def sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError

        config = BridgeConfig("ws://localhost/agent-session/connect", TOKEN, Path("/unused.sock"), "test")
        with (
            patch("discord_blue.codex_bridge.__main__.CodexBridge", BrokenBridge),
            patch("discord_blue.codex_bridge.__main__.sleep", sleep),
            self.assertLogs(level="ERROR") as logs,
            self.assertRaises(asyncio.CancelledError),
        ):
            await run_bridge(config)
        self.assertEqual(delays[0], config.reconnect_seconds)
        self.assertTrue(all(a < b for a, b in pairwise(delays)))
        self.assertIn("KeyError", "\n".join(logs.output))
        self.assertIn(" in run", "\n".join(logs.output))
        self.assertNotIn(provider_message, "\n".join(logs.output))

    async def test_a_recovered_home_resets_its_unexpected_failure_delay(self) -> None:
        delays: list[float] = []

        class BrokenBridge:
            def __init__(self, _config: BridgeConfig) -> None:
                pass

            async def run(self) -> None:
                raise KeyError("thread")

        async def sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError

        ticks = [0, 0, 0, FAILURE_RESET_SECONDS * 2, FAILURE_RESET_SECONDS * 2, FAILURE_RESET_SECONDS * 2]
        config = BridgeConfig("ws://localhost/agent-session/connect", TOKEN, Path("/unused.sock"), "test")
        with (
            patch("discord_blue.codex_bridge.__main__.CodexBridge", BrokenBridge),
            patch("discord_blue.codex_bridge.__main__.sleep", sleep),
            patch("discord_blue.codex_bridge.__main__.monotonic", side_effect=ticks),
            self.assertLogs(level="ERROR"),
            self.assertRaises(asyncio.CancelledError),
        ):
            await run_bridge(config)
        self.assertEqual(delays[0], config.reconnect_seconds)
        self.assertEqual(delays[1], delays[0])
        self.assertGreater(delays[2], delays[1])

    async def test_multiple_homes_run_independently_and_aliases_join_only_once(self) -> None:
        entered: set[Path] = set()
        stopped: set[Path] = set()
        ready = asyncio.Event()

        class FakeBridge:
            def __init__(self, config: BridgeConfig) -> None:
                self.path = config.socket_path

            async def run(self) -> None:
                entered.add(self.path)
                if len(entered) == 2:
                    ready.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.add(self.path)

        with tempfile.TemporaryDirectory() as root:
            home = Path(root).resolve()
            alias = home / "alias"
            alias.symlink_to(home / "account-a", target_is_directory=True)
            config = BridgeConfig("ws://localhost/agent-session/connect", TOKEN, home / "unused", "test")
            with patch("discord_blue.codex_bridge.__main__.CodexBridge", FakeBridge):
                task = asyncio.create_task(run_bridges(config, [home / "account-a", home / "account-b", alias]))
                try:
                    await asyncio.wait_for(ready.wait(), 2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            expected = {socket_for_home(home / name) for name in ("account-a", "account-b")}
            self.assertEqual(entered, expected)
            self.assertEqual(stopped, expected)

    async def test_no_home_override_preserves_configured_socket(self) -> None:
        used: list[Path] = []

        class FakeBridge:
            def __init__(self, config: BridgeConfig) -> None:
                used.append(config.socket_path)

            async def run(self) -> None:
                return

        config = BridgeConfig("ws://localhost/agent-session/connect", TOKEN, Path("/configured.sock"), "test")
        with patch("discord_blue.codex_bridge.__main__.CodexBridge", FakeBridge):
            await run_bridges(config, [])
        self.assertEqual(used, [config.socket_path])
