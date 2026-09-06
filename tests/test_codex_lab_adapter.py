from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from discord_blue.codex_lab_adapter import LOCAL_ONLY, OUTPUT_LIMIT, DevAdapter, assistant_text, validate_url
from discord_blue.codex_lab_rpc import AppServerClient, RpcError, TransportError
from discord_blue.doodads.agent_session import bridge as bridge_module
from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
from tests.fakes_agent_session import FakeBot, FakeReplyMessage, FakeThread

Json = dict[str, Any]


class FakeRpc:
    def __init__(self, responses: dict[str, list[Json | Exception]] | None = None) -> None:
        self.responses = responses or {}
        self.requests: list[tuple[str, Json | None]] = []
        self.events: asyncio.Queue[Json] = asyncio.Queue()
        self.initialized = False
        self.requested = asyncio.Event()

    async def initialize(self) -> Json:
        self.initialized = True
        return {}

    async def request(self, method: str, params: Json | None = None) -> Json:
        self.requests.append((method, params))
        self.requested.set()
        results = self.responses.get(method, [])
        if not results:
            raise AssertionError(f"Unexpected RPC request: {method} {params}")
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def receive(self) -> Json:
        return await self.events.get()

    def put(self, *events: Json) -> None:
        for event in events:
            self.events.put_nowait(event)


class FakeControlWebSocket:
    def __init__(self, *messages: Json) -> None:
        self.frames = [SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(message)) for message in messages]
        self.sent: list[Json] = []

    def __aiter__(self) -> AsyncIterator[object]:
        async def frames() -> AsyncIterator[object]:
            for frame in self.frames:
                yield frame

        return frames()

    async def send_json(self, payload: Json) -> None:
        self.sent.append(payload)


class FailingWebSocket:
    async def send_json(self, _payload: Json) -> None:
        raise ConnectionError("ambiguous websocket write")


class GuardedWebSocket:
    def __init__(self) -> None:
        self.active = False
        self.overlapped = False
        self.encoded: list[str] = []

    async def send_json(self, payload: Json) -> None:
        if self.active:
            self.overlapped = True
            raise RuntimeError("overlapping websocket writes")
        self.active = True
        try:
            await asyncio.sleep(0.002)
            self.encoded.append(json.dumps(payload))
        finally:
            self.active = False


class BlockingWebSocket:
    async def send_json(self, _payload: Json) -> None:
        await asyncio.Event().wait()


def thread(*, status: str = "idle", **fields: object) -> Json:
    return {
        "id": "thread-1",
        "cwd": "/workspace/project",
        "gitInfo": {"branch": "feature/adapter"},
        "canAcceptDirectInput": True,
        "status": {"type": status},
        **fields,
    }


def adapter_for(
    rpc: FakeRpc,
    *,
    command_limit: int = 10_000,
    heartbeat_interval: float = 30,
    reconnect_delay: float = 2,
    io_timeout: float = 15,
) -> DevAdapter:
    return DevAdapter(
        cast(AppServerClient, rpc),
        "thread-1",
        command_limit=command_limit,
        heartbeat_interval=heartbeat_interval,
        reconnect_delay=reconnect_delay,
        io_timeout=io_timeout,
    )


def command(adapter: DevAdapter, command_id: str, kind: str, **fields: object) -> Json:
    return {
        "type": "command",
        "command_id": command_id,
        "session_id": adapter.thread_id,
        "session_epoch": adapter.epoch,
        "kind": kind,
        **fields,
    }


async def collect_events(adapter: DevAdapter, rpc: FakeRpc, events: list[Json], expected: int) -> list[Json]:
    rpc.put(*events)
    task = asyncio.create_task(adapter.app_events())
    try:
        async with asyncio.timeout(2):
            while len(adapter.outbox) < expected:
                await adapter.available.wait()
                await asyncio.sleep(0)
        return list(adapter.outbox)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class AdapterAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_requires_materialized_root_direct_input_thread(self) -> None:
        for invalid in (
            thread(id="another-thread"),
            thread(parentThreadId="parent"),
            thread(canAcceptDirectInput=False),
        ):
            with self.subTest(thread=invalid):
                rpc = FakeRpc({"thread/resume": [{"thread": invalid}]})
                adapter = adapter_for(rpc)

                with self.assertRaisesRegex(TransportError, "existing root thread"):
                    await adapter.attach()

                self.assertTrue(rpc.initialized)

    async def test_attach_uses_excluded_history_and_builds_bounded_identity_snapshot(self) -> None:
        rpc = FakeRpc({"thread/resume": [{"thread": thread(), "initialTurnsPage": {"data": []}}]})
        adapter = adapter_for(rpc)

        await adapter.attach()

        self.assertEqual(
            rpc.requests,
            [
                (
                    "thread/resume",
                    {
                        "threadId": "thread-1",
                        "excludeTurns": True,
                        "initialTurnsPage": {"limit": 1, "itemsView": "summary", "sortDirection": "desc"},
                    },
                )
            ],
        )
        self.assertEqual(adapter.hello["type"], "hello")
        self.assertEqual(adapter.hello["cwd"], "/workspace/project")
        self.assertEqual(adapter.hello["branch"], "feature/adapter")
        self.assertNotIn("assistant_message", adapter.hello)
        self.assertEqual(adapter.completed, set())

    async def test_attach_backfill_marks_turn_completed_so_live_event_is_not_replayed(self) -> None:
        completed = {
            "id": "turn-before-attach",
            "status": "completed",
            "items": [{"id": "answer-1", "type": "agentMessage", "text": "existing answer"}],
        }
        rpc = FakeRpc({"thread/resume": [{"thread": thread(), "initialTurnsPage": {"data": [completed]}}]})
        adapter = adapter_for(rpc)
        await adapter.attach()

        published = await collect_events(
            adapter,
            rpc,
            [
                {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": completed}},
                {"method": "turn/started", "params": {"threadId": "thread-1", "turn": {"id": "turn-new"}}},
            ],
            1,
        )

        self.assertEqual(adapter.hello["assistant_message"], "existing answer")
        self.assertEqual(adapter.completed, {"turn-before-attach"})
        self.assertEqual([event["type"] for event in published], ["status_changed"])

    async def test_attach_omits_absent_branch_and_rejects_snapshot_turn_without_id(self) -> None:
        no_branch_rpc = FakeRpc({"thread/resume": [{"thread": thread(gitInfo=None), "initialTurnsPage": {"data": []}}]})
        adapter = adapter_for(no_branch_rpc)
        await adapter.attach()
        self.assertNotIn("branch", adapter.hello)

        invalid_rpc = FakeRpc(
            {
                "thread/resume": [
                    {
                        "thread": thread(),
                        "initialTurnsPage": {"data": [{"status": "completed", "items": []}]},
                    }
                ]
            }
        )
        with self.assertRaisesRegex(TransportError, "Invalid app-server turn snapshot"):
            await adapter_for(invalid_rpc).attach()

    async def test_attach_converts_known_resume_rejection_without_retry(self) -> None:
        rpc = FakeRpc({"thread/resume": [RpcError(-32602)]})

        with self.assertRaisesRegex(TransportError, "Cannot attach thread"):
            await adapter_for(rpc).attach()

        self.assertEqual([method for method, _ in rpc.requests], ["thread/resume"])


class AdapterEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_turn_combines_item_event_and_turn_fallback_once(self) -> None:
        rpc = FakeRpc()
        adapter = adapter_for(rpc)
        events: list[Json] = [
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "answer-1", "type": "agentMessage", "text": "first"},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [
                            {"id": "answer-1", "type": "agentMessage", "text": "duplicate"},
                            {"id": "answer-2", "type": "agentMessage", "text": "second"},
                        ],
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}},
            },
        ]

        published = await collect_events(adapter, rpc, events, 1)

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["type"], "turn_complete")
        self.assertEqual(published[0]["assistant_message"], "duplicate\n\nsecond")
        self.assertEqual(adapter.completed, {"turn-1"})

    async def test_events_ignore_unrelated_threads_and_hide_native_requests(self) -> None:
        rpc = FakeRpc()
        adapter = adapter_for(rpc)
        secret = "do-not-forward-this-prompt"
        events = [
            {"method": "turn/started", "params": {"threadId": "other-thread", "turn": {"id": "other-turn"}}},
            {
                "id": "approval-rpc-1",
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "thread-1", "command": ["echo", secret], "reason": secret},
            },
            {
                "id": "input-rpc-1",
                "method": "item/tool/requestUserInput",
                "params": {"threadId": "thread-1", "questions": [{"question": secret}]},
            },
        ]

        published = await collect_events(adapter, rpc, events, 2)

        self.assertEqual([event["type"] for event in published], ["status_changed", "status_changed"])
        self.assertTrue(all(event["message"].startswith("Action required in the native TUI") for event in published))
        self.assertNotIn(secret, json.dumps(published))
        self.assertEqual(rpc.requests, [])

    async def test_local_request_tracking_clears_only_after_every_request_resolves(self) -> None:
        rpc = FakeRpc()
        adapter = adapter_for(rpc)
        events: list[Json] = [
            {"id": "request-1", "method": "item/requestApproval", "params": {"threadId": "thread-1"}},
            {"id": 2, "method": "item/requestUserInput", "params": {"threadId": "thread-1"}},
            {"method": "serverRequest/resolved", "params": {"threadId": "thread-1", "requestId": "request-1"}},
            {"method": "serverRequest/resolved", "params": {"threadId": "thread-1", "requestId": 2}},
        ]

        published = await collect_events(adapter, rpc, events, 3)

        self.assertEqual(adapter.local_requests, set())
        self.assertEqual([event["type"] for event in published], ["status_changed"] * 3)
        self.assertTrue(all("request-1" not in json.dumps(event) for event in published))
        self.assertEqual(published[-1]["message"], "Local request resolved; see native TUI for its outcome.")

    async def test_pending_local_requests_have_a_hard_limit(self) -> None:
        rpc = FakeRpc()
        adapter = adapter_for(rpc)
        adapter.local_requests.update(range(100))
        rpc.put({"id": "over-limit", "method": "item/requestApproval", "params": {"threadId": "thread-1"}})

        with self.assertRaisesRegex(TransportError, "Too many pending local requests"):
            await adapter.app_events()

    async def test_completed_output_and_incomplete_turn_tracking_are_bounded(self) -> None:
        adapter = adapter_for(FakeRpc())
        oversized = "x" * (OUTPUT_LIMIT + 100)
        adapter.remember_answer("turn-1", {"id": "answer-1", "text": oversized})

        self.assertLessEqual(len(adapter.answers["turn-1"]["answer-1"]), OUTPUT_LIMIT + 1)
        for number in range(2, 65):
            adapter.remember_answer(f"turn-{number}", {"id": "answer-{number}", "text": "x"})
        with self.assertRaisesRegex(TransportError, "Too many incomplete turns"):
            adapter.remember_answer("turn-65", {"id": "answer-65", "text": "x"})

        turn_payload = {"items": [{"id": "answer-1", "type": "agentMessage", "text": oversized}]}
        self.assertEqual(len(assistant_text(turn_payload)), OUTPUT_LIMIT)

    def test_output_backlog_has_a_hard_limit(self) -> None:
        adapter = adapter_for(FakeRpc())
        for _ in range(256):
            adapter.publish("status_changed", message="queued")

        with self.assertRaisesRegex(TransportError, "backlog is full"):
            adapter.publish("status_changed", message="one too many")

    async def test_completion_queued_while_offline_is_delivered_once_after_connection(self) -> None:
        adapter = adapter_for(FakeRpc())
        adapter.publish("turn_complete", message="Turn completed", assistant_message="offline answer")
        websocket = FakeControlWebSocket()

        delivery = asyncio.create_task(adapter.deliver(cast(aiohttp.ClientWebSocketResponse, websocket)))
        try:
            async with asyncio.timeout(2):
                while websocket.sent == []:
                    await asyncio.sleep(0)
            self.assertEqual(websocket.sent[0]["assistant_message"], "offline answer")
            self.assertEqual(list(adapter.outbox), [])
        finally:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)

    async def test_ambiguous_output_write_is_removed_and_never_replayed(self) -> None:
        adapter = adapter_for(FakeRpc())
        adapter.publish("turn_complete", message="Turn completed", assistant_message="possibly delivered")

        with self.assertRaisesRegex(ConnectionError, "ambiguous websocket write"):
            await adapter.deliver(cast(aiohttp.ClientWebSocketResponse, FailingWebSocket()))

        self.assertEqual(list(adapter.outbox), [])


class AdapterCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_reply_starts_turn_and_deduplicates_completed_command(self) -> None:
        rpc = FakeRpc({"thread/read": [{"thread": thread(status="idle")}], "turn/start": [{"turn": {"id": "turn-1"}}]})
        adapter = adapter_for(rpc)
        message = command(adapter, "command-1", "reply", text="Continue")

        first = await adapter.command(message)
        duplicate = await adapter.command(dict(message))

        self.assertEqual(first, duplicate)
        self.assertEqual(first["type"], "command_ack")
        self.assertEqual(
            rpc.requests,
            [
                ("thread/read", {"threadId": "thread-1", "includeTurns": False}),
                ("turn/start", {"threadId": "thread-1", "input": [{"type": "text", "text": "Continue"}]}),
            ],
        )

    async def test_active_reply_steers_exact_current_turn(self) -> None:
        rpc = FakeRpc(
            {
                "thread/read": [{"thread": thread(status="active")}],
                "thread/turns/list": [{"data": [{"id": "turn-active", "status": "inProgress"}]}],
                "turn/steer": [{}],
            }
        )
        adapter = adapter_for(rpc)

        response = await adapter.command(command(adapter, "command-1", "reply", text="Adjust course"))

        self.assertEqual(response["type"], "command_ack")
        self.assertEqual(
            rpc.requests[-1],
            (
                "turn/steer",
                {
                    "threadId": "thread-1",
                    "input": [{"type": "text", "text": "Adjust course"}],
                    "expectedTurnId": "turn-active",
                },
            ),
        )

    async def test_pause_interrupts_exact_current_turn(self) -> None:
        rpc = FakeRpc(
            {
                "thread/read": [{"thread": thread(status="active")}],
                "thread/turns/list": [{"data": [{"id": "turn-active", "status": "inProgress"}]}],
                "turn/interrupt": [{}],
            }
        )
        adapter = adapter_for(rpc)

        response = await adapter.command(command(adapter, "command-1", "pause_current_turn"))

        self.assertEqual(response["type"], "command_ack")
        self.assertEqual(rpc.requests[-1], ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-active"}))

    async def test_stale_identity_reused_id_and_command_cap_do_not_call_rpc(self) -> None:
        rpc = FakeRpc({"thread/read": [{"thread": thread()}], "turn/start": [{}]})
        adapter = adapter_for(rpc, command_limit=1)
        stale = command(adapter, "stale", "reply", text="ignored")
        stale["session_epoch"] = "old-epoch"
        self.assertIn("Stale session identity", (await adapter.command(stale))["reason"])

        original = command(adapter, "same-id", "reply", text="first")
        self.assertEqual((await adapter.command(original))["type"], "command_ack")
        conflict = command(adapter, "same-id", "reply", text="different")
        self.assertIn("reused with different content", (await adapter.command(conflict))["reason"])
        self.assertIn("Command limit reached", (await adapter.command(command(adapter, "over-cap", "status_request")))["reason"])
        self.assertEqual(len(rpc.requests), 2)

    async def test_local_only_commands_and_approval_decisions_are_rejected(self) -> None:
        adapter = adapter_for(FakeRpc())
        for number, kind in enumerate(("new_session", "continue_autonomously", "request_user_input_response")):
            with self.subTest(kind=kind):
                response = await adapter.command(command(adapter, f"command-{number}", kind))
                self.assertEqual(response["type"], "command_reject")
                self.assertEqual(response["reason"], LOCAL_ONLY)

        approval = command(adapter, "approval-command", "unused", approval_id="approval-1")
        approval["type"] = "approval_decision"
        response = await adapter.command(approval)
        self.assertEqual(response["type"], "approval_decision_reject")
        self.assertEqual(response["approval_id"], "approval-1")
        self.assertEqual(response["reason"], LOCAL_ONLY)

    async def test_known_rpc_rejection_is_cached_and_never_retried(self) -> None:
        rpc = FakeRpc({"thread/read": [{"thread": thread()}], "turn/start": [RpcError(-32000)]})
        adapter = adapter_for(rpc)
        message = command(adapter, "command-1", "reply", text="Continue")

        first = await adapter.command(message)
        second = await adapter.command(dict(message))

        self.assertEqual(first, second)
        self.assertIn("rejected the command", first["reason"])
        self.assertEqual([method for method, _ in rpc.requests], ["thread/read", "turn/start"])

    async def test_ambiguous_transport_failure_escapes_and_is_never_retried(self) -> None:
        rpc = FakeRpc({"thread/read": [{"thread": thread()}], "turn/start": [TransportError("timed out after possible write")]})
        adapter = adapter_for(rpc)
        message = command(adapter, "command-1", "reply", text="Continue")

        with self.assertRaisesRegex(TransportError, "timed out after possible write"):
            await adapter.command(message)
        replay = await adapter.command(dict(message))

        self.assertIn("already in progress", replay["reason"])
        self.assertEqual([method for method, _ in rpc.requests], ["thread/read", "turn/start"])

    async def test_end_detaches_discord_and_explicitly_leaves_native_session_running(self) -> None:
        adapter = adapter_for(FakeRpc())
        websocket = FakeControlWebSocket(command(adapter, "command-1", "end_session"))

        await adapter.controls(cast(aiohttp.ClientWebSocketResponse, websocket))

        self.assertTrue(adapter.stopped.is_set())
        self.assertEqual([payload["type"] for payload in websocket.sent], ["command_ack", "status_changed"])
        self.assertIn("native TUI session continues", websocket.sent[-1]["message"])


class AdapterConfigurationTests(unittest.TestCase):
    def test_timing_defaults_and_bounds_are_explicit(self) -> None:
        adapter = adapter_for(FakeRpc())
        self.assertEqual((adapter.heartbeat_interval, adapter.reconnect_delay, adapter.io_timeout), (30, 2, 15))

        for options in (
            {"heartbeat_interval": 0},
            {"reconnect_delay": 0},
            {"io_timeout": 0},
            {"command_limit": 0},
        ):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "must be positive"):
                adapter_for(FakeRpc(), **cast(Any, options))

    def test_bridge_url_requires_exact_secure_endpoint_or_literal_loopback(self) -> None:
        for accepted in (
            "wss://bridge.example.com/agent-session/connect",
            "ws://127.0.0.1:8787/agent-session/connect",
            "ws://[::1]:8787/agent-session/connect",
        ):
            with self.subTest(accepted=accepted):
                validate_url(accepted)

        for rejected in (
            "http://127.0.0.1/agent-session/connect",
            "ws://localhost/agent-session/connect",
            "ws://10.0.0.5/agent-session/connect",
            "wss://user:password@bridge.example.com/agent-session/connect",
            "wss://bridge.example.com/wrong",
            "wss://bridge.example.com/agent-session/connect?token=secret",
            "wss://bridge.example.com/agent-session/connect#fragment",
        ):
            with self.subTest(rejected=rejected), self.assertRaises(ValueError):
                validate_url(rejected)


class AdapterDiscordLoopTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def configured(adapter: DevAdapter) -> None:
        adapter.hello = adapter.event(
            "hello",
            host_label="Codex Lab test adapter",
            cwd="/workspace/project",
            assistant_message="existing assistant snapshot",
            pid=42,
        )

    async def test_idle_connection_sends_repeated_heartbeats_after_fresh_rpc_reads(self) -> None:
        rpc = FakeRpc({"thread/read": [{"thread": thread()} for _ in range(20)]})
        adapter = adapter_for(rpc, heartbeat_interval=0.003, reconnect_delay=0.001, io_timeout=0.5)
        self.configured(adapter)
        received: list[Json] = []
        extensions: list[str | None] = []

        async def handler(request: web.Request) -> web.WebSocketResponse:
            extensions.append(request.headers.get("Sec-WebSocket-Extensions"))
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            received.append(await websocket.receive_json())
            await websocket.send_json({"type": "hello_ack"})
            received.append(await websocket.receive_json())
            for _ in range(3):
                received.append(await websocket.receive_json())
            await websocket.send_json(command(adapter, "end-heartbeat-test", "end_session"))
            received.append(await websocket.receive_json())
            received.append(await websocket.receive_json())
            return websocket

        app = web.Application()
        app.router.add_get("/agent-session/connect", handler)
        async with TestServer(app) as server:
            url = str(server.make_url("/agent-session/connect")).replace("http://", "ws://", 1)
            await asyncio.wait_for(adapter.discord(url, "test-token"), timeout=2)

        heartbeats = [message for message in received if message.get("type") == "heartbeat"]
        reads = [request for request in rpc.requests if request[0] == "thread/read"]
        self.assertGreaterEqual(len(heartbeats), 3)
        self.assertGreaterEqual(len(reads), len(heartbeats))
        self.assertEqual(extensions, [None])

    async def test_close_after_ack_reconnects_same_epoch_without_snapshot_and_cached_command_does_not_repeat_rpc(self) -> None:
        rpc = FakeRpc({"thread/read": [{"thread": thread()}], "turn/start": [{}]})
        adapter = adapter_for(rpc, reconnect_delay=0.001, io_timeout=0.5)
        self.configured(adapter)
        hellos: list[Json] = []
        responses: list[Json] = []
        replayed_command: Json | None = None

        async def handler(request: web.Request) -> web.WebSocketResponse:
            nonlocal replayed_command
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            hello = await websocket.receive_json()
            hellos.append(hello)
            await websocket.send_json({"type": "hello_ack"})
            await websocket.receive_json()  # initial status
            if len(hellos) == 1:
                replayed_command = command(adapter, "replayed-command", "reply", text="exactly once")
                await websocket.send_json(replayed_command)
                responses.append(await websocket.receive_json())
                await websocket.close()
            else:
                assert replayed_command is not None
                await websocket.send_json(replayed_command)
                responses.append(await websocket.receive_json())
                await websocket.send_json(command(adapter, "end-reconnect-test", "end_session"))
                responses.append(await websocket.receive_json())
                responses.append(await websocket.receive_json())
            return websocket

        app = web.Application()
        app.router.add_get("/agent-session/connect", handler)
        async with TestServer(app) as server:
            url = str(server.make_url("/agent-session/connect")).replace("http://", "ws://", 1)
            await asyncio.wait_for(adapter.discord(url, "test-token"), timeout=2)

        self.assertEqual(len(hellos), 2)
        self.assertEqual(hellos[0]["session_epoch"], hellos[1]["session_epoch"])
        self.assertEqual(hellos[0]["assistant_message"], "existing assistant snapshot")
        self.assertNotIn("assistant_message", hellos[1])
        self.assertEqual([response["type"] for response in responses[:2]], ["command_ack", "command_ack"])
        self.assertEqual([method for method, _ in rpc.requests], ["thread/read", "turn/start"])

    async def test_close_before_hello_ack_is_transient_and_reconnects_cleanly(self) -> None:
        adapter = adapter_for(FakeRpc(), reconnect_delay=0.001, io_timeout=0.5)
        self.configured(adapter)
        hellos: list[Json] = []

        async def handler(request: web.Request) -> web.WebSocketResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            hellos.append(await websocket.receive_json())
            if len(hellos) == 1:
                await websocket.close()
            else:
                await websocket.send_json({"type": "hello_ack"})
                await websocket.receive_json()
                await websocket.send_json(command(adapter, "end-before-ack-test", "end_session"))
                await websocket.receive_json()
                await websocket.receive_json()
            return websocket

        app = web.Application()
        app.router.add_get("/agent-session/connect", handler)
        async with TestServer(app) as server:
            url = str(server.make_url("/agent-session/connect")).replace("http://", "ws://", 1)
            await asyncio.wait_for(adapter.discord(url, "test-token"), timeout=2)

        self.assertEqual(len(hellos), 2)
        self.assertEqual(hellos[0]["session_epoch"], hellos[1]["session_epoch"])
        self.assertIn("assistant_message", hellos[1])

    async def test_malformed_text_hello_ack_raises_transport_error(self) -> None:
        adapter = adapter_for(FakeRpc(), reconnect_delay=0.001, io_timeout=0.5)
        self.configured(adapter)

        async def handler(request: web.Request) -> web.WebSocketResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.receive_json()
            await websocket.send_str("not JSON")
            return websocket

        app = web.Application()
        app.router.add_get("/agent-session/connect", handler)
        async with TestServer(app) as server:
            url = str(server.make_url("/agent-session/connect")).replace("http://", "ws://", 1)
            with self.assertRaisesRegex(TransportError, "hello acknowledgement JSON"):
                await asyncio.wait_for(adapter.discord(url, "test-token"), timeout=2)

    async def test_concurrent_large_completion_ack_and_heartbeat_writes_are_serialized_and_parseable(self) -> None:
        adapter = adapter_for(FakeRpc(), io_timeout=0.5)
        websocket = GuardedWebSocket()
        messages = [
            adapter.event("turn_complete", message="Turn completed", assistant_message="x" * 1_500),
            adapter.event("command_ack", command_id="command-1"),
            adapter.event("heartbeat"),
        ]

        await asyncio.gather(*(adapter.send(cast(aiohttp.ClientWebSocketResponse, websocket), message) for message in messages))

        self.assertFalse(websocket.overlapped)
        self.assertCountEqual(
            [json.loads(encoded)["type"] for encoded in websocket.encoded], [message["type"] for message in messages]
        )

    async def test_send_timeout_bounds_lock_and_websocket_drain(self) -> None:
        adapter = adapter_for(FakeRpc(), io_timeout=0.001)

        with self.assertRaises(TimeoutError):
            await adapter.send(cast(aiohttp.ClientWebSocketResponse, BlockingWebSocket()), adapter.event("heartbeat"))


class AdapterBridgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_uses_real_bridge_websocket_for_reply_and_detach(self) -> None:
        rpc = FakeRpc(
            {
                "thread/resume": [{"thread": thread(), "initialTurnsPage": {"data": []}}],
                "thread/read": [{"thread": thread(status="idle")}],
                "turn/start": [{"turn": {"id": "turn-1"}}],
            }
        )
        adapter = adapter_for(rpc)
        await adapter.attach()

        config = SimpleNamespace(
            agent_session=SimpleNamespace(token="integration-token", operator_role_name="", channel_id=0),
            discord=SimpleNamespace(employee_role_name="", bot_channel_id=0),
        )
        discord_thread = FakeThread(555)
        bridge = AgentSessionBridge(cast(Any, FakeBot(cast(Any, config), discord_thread)))
        app = web.Application()
        bridge.register_routes(app)
        attachment = bridge_module.SessionThread(thread=cast(Any, discord_thread), notification_message_id=None)
        attached = asyncio.Event()
        acknowledged = asyncio.Event()

        async def attach_thread(_hello: object) -> object:
            attached.set()
            return attachment

        handle_command_ack = bridge.handle_command_ack

        async def observe_command_ack(payload: Json) -> None:
            await handle_command_ack(payload)
            if payload.get("command_id") != "transport-barrier":
                acknowledged.set()

        thread_type_patch = patch.object(bridge_module.discord, "Thread", FakeThread)
        thread_type_patch.start()
        server = TestServer(app)
        await server.start_server()
        websocket_task: asyncio.Task[None] | None = None
        try:
            bridge_url = str(server.make_url("/agent-session/connect")).replace("http://", "ws://", 1)
            with (
                patch.object(bridge, "find_or_create_session_thread", new=AsyncMock(side_effect=attach_thread)),
                patch.object(bridge, "handle_command_ack", new=observe_command_ack),
            ):
                websocket_task = asyncio.create_task(adapter.discord(bridge_url, "integration-token"))
                await asyncio.wait_for(attached.wait(), timeout=2)

                reply = FakeReplyMessage(101, discord_thread, "Continue through the real bridge")
                discord_thread.add_message(reply)
                self.assertTrue(await bridge.send_thread_reply(cast(Any, reply)))
                async with asyncio.timeout(2):
                    while not any(method == "turn/start" for method, _ in rpc.requests):
                        rpc.requested.clear()
                        await rpc.requested.wait()
                await asyncio.wait_for(acknowledged.wait(), timeout=2)

                self.assertEqual(reply.reactions, [])
                self.assertEqual(rpc.requests[-1][0], "turn/start")
                response = await bridge.send_end_session(cast(Any, discord_thread), cast(Any, SimpleNamespace(id=123)))
                self.assertEqual(response, "Asked the agent session to end this session.")
                await asyncio.wait_for(websocket_task, timeout=2)
                self.assertTrue(adapter.stopped.is_set())
                self.assertFalse(any(method in {"thread/close", "thread/archive"} for method, _ in rpc.requests))
        finally:
            thread_type_patch.stop()
            if websocket_task is not None and not websocket_task.done():
                websocket_task.cancel()
                await asyncio.gather(websocket_task, return_exceptions=True)
            await server.close()


if __name__ == "__main__":
    unittest.main()
