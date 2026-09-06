from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from aiohttp import ClientSession, ClientWebSocketResponse, web

from discord_blue.codex_lab_rpc import AppServerClient, RpcError, TransportError

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@asynccontextmanager
async def unix_server(handler: Handler) -> AsyncIterator[Path]:
    with tempfile.TemporaryDirectory() as temp_dir:
        socket_path = Path(temp_dir) / "app-server.sock"
        app = web.Application()
        app.router.add_get("/", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.UnixSite(runner, socket_path)
        await site.start()
        socket_path.chmod(0o600)
        try:
            yield socket_path
        finally:
            await runner.cleanup()


class AppServerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_sends_required_handshake_and_cleans_up(self) -> None:
        received: list[dict[str, Any]] = []
        closed = asyncio.Event()

        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            received.append(await websocket.receive_json())
            await websocket.send_json({"id": received[0]["id"], "result": {"userAgent": "test"}})
            received.append(await websocket.receive_json())
            async for _frame in websocket:
                pass
            closed.set()
            return websocket

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path) as client:
                result = await client.initialize()
                self.assertEqual(result, {"userAgent": "test"})
            await asyncio.wait_for(closed.wait(), timeout=1)

        initialize, initialized = received
        self.assertEqual(initialize["method"], "initialize")
        self.assertTrue(initialize["id"].startswith("discord-blue-"))
        self.assertEqual(initialize["params"]["clientInfo"]["name"], "discord_blue")
        self.assertTrue(initialize["params"]["capabilities"]["experimentalApi"])
        self.assertEqual(initialized, {"method": "initialized"})

    async def test_interleaved_notification_and_response_are_routed_independently(self) -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            message = await websocket.receive_json()
            await websocket.send_json({"method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
            await websocket.send_json({"id": message["id"], "result": {"thread": {"id": "thread-1"}}})
            async for _frame in websocket:
                pass
            return websocket

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path) as client:
                request_task = asyncio.create_task(client.request("thread/start", {"cwd": "/tmp"}))
                event = await asyncio.wait_for(client.receive(), timeout=1)
                result = await asyncio.wait_for(request_task, timeout=1)

        self.assertEqual(event["method"], "turn/started")
        self.assertEqual(result["thread"]["id"], "thread-1")

    async def test_server_request_is_queued_until_adapter_responds(self) -> None:
        response: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.send_json(
                {
                    "id": 42,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": ["true"]},
                }
            )
            response.set_result(await websocket.receive_json())
            async for _frame in websocket:
                pass
            return websocket

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path) as client:
                request = await asyncio.wait_for(client.receive(), timeout=1)
                await client.respond(request["id"], {"decision": "accept"})
                server_response = await asyncio.wait_for(response, timeout=1)

        self.assertEqual(server_response, {"id": 42, "result": {"decision": "accept"}})

    async def test_rpc_error_is_confirmed_and_does_not_expose_server_values(self) -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            message = await websocket.receive_json()
            await websocket.send_json(
                {
                    "id": message["id"],
                    "error": {"code": -32602, "message": "secret echoed", "data": {"token": "secret"}},
                }
            )
            async for _frame in websocket:
                pass
            return websocket

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path) as client:
                with self.assertRaises(RpcError) as raised:
                    await client.request("thread/start", {})

        self.assertEqual(raised.exception.code, -32602)
        self.assertNotIn("secret", str(raised.exception))

    async def test_timeout_is_fatal_and_signals_consumer(self) -> None:
        connection_closed = asyncio.Event()

        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.receive_json()
            async for _frame in websocket:
                pass
            connection_closed.set()
            return websocket

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path, timeout=0.05) as client:
                with self.assertRaisesRegex(TransportError, "timed out"):
                    await client.request("thread/start", {})
                with self.assertRaisesRegex(TransportError, "timed out"):
                    await client.receive()
                await asyncio.wait_for(connection_closed.wait(), timeout=1)

    async def test_disconnect_fails_outstanding_request_and_consumer(self) -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.receive_json()
            await websocket.close()
            return websocket

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path) as client:
                with self.assertRaisesRegex(TransportError, "disconnected"):
                    await client.request("thread/start", {})
                with self.assertRaisesRegex(TransportError, "disconnected"):
                    await client.receive()

    async def test_binary_frame_is_fatal(self) -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.send_bytes(b"not JSON text")
            async for _frame in websocket:
                pass
            return websocket

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path) as client:
                with self.assertRaisesRegex(TransportError, "invalid websocket frame"):
                    await client.receive()

    async def test_malformed_json_is_fatal(self) -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.send_str("{")
            async for _frame in websocket:
                pass
            return websocket

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path) as client:
                with self.assertRaisesRegex(TransportError, "malformed JSON"):
                    await client.receive()

    async def test_queue_overflow_is_fatal_and_replaces_unconsumed_messages(self) -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.send_json({"method": "event/one"})
            await websocket.send_json({"method": "event/two"})
            async for _frame in websocket:
                pass
            return websocket

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path, queue_size=1) as client:
                await asyncio.sleep(0)
                with self.assertRaisesRegex(TransportError, "queue overflow"):
                    await asyncio.wait_for(client.receive(), timeout=1)

    async def test_cancellation_discards_waiter_without_poisoning_connection(self) -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            first = await websocket.receive_json()
            second = await websocket.receive_json()
            await websocket.send_json({"id": first["id"], "result": {"late": True}})
            await websocket.send_json({"id": second["id"], "result": {"ok": True}})
            async for _frame in websocket:
                pass
            return websocket

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path) as client:
                cancelled = asyncio.create_task(client.request("first"))
                await asyncio.sleep(0)
                cancelled.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelled
                self.assertEqual(await client.request("second"), {"ok": True})

    async def test_relative_socket_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            AppServerClient("relative.sock")

    async def test_non_socket_endpoint_is_rejected(self) -> None:
        with tempfile.NamedTemporaryFile() as endpoint:
            client = AppServerClient(Path(endpoint.name))
            with self.assertRaisesRegex(TransportError, "not a Unix socket"):
                await client.start()

    async def test_socket_owned_by_another_user_is_rejected(self) -> None:
        socket_stat = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.getuid() + 1)
        with patch("discord_blue.codex_lab_rpc.Path.stat", return_value=socket_stat):
            client = AppServerClient(Path("/tmp/app-server.sock"))
            with self.assertRaisesRegex(TransportError, "unexpected owner"):
                await client.start()

    async def test_world_writable_socket_is_rejected(self) -> None:
        socket_stat = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o602, st_uid=os.getuid())
        with patch("discord_blue.codex_lab_rpc.Path.stat", return_value=socket_stat):
            client = AppServerClient(Path("/tmp/app-server.sock"))
            with self.assertRaisesRegex(TransportError, "writable by other users"):
                await client.start()

    async def test_connect_timeout_closes_session(self) -> None:
        async def handler(_request: web.Request) -> web.StreamResponse:
            return web.Response()

        async def stall_connect(*_args: object, **_kwargs: object) -> ClientWebSocketResponse:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async with unix_server(handler) as socket_path:
            client = AppServerClient(socket_path, timeout=0.01)
            with patch.object(ClientSession, "ws_connect", side_effect=stall_connect):
                with self.assertRaisesRegex(TransportError, "could not connect"):
                    await client.start()
            self.assertIsNone(client._session)

    async def test_cancelled_connect_closes_session(self) -> None:
        connect_started = asyncio.Event()

        async def handler(_request: web.Request) -> web.StreamResponse:
            return web.Response()

        async def stall_connect(*_args: object, **_kwargs: object) -> ClientWebSocketResponse:
            connect_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async with unix_server(handler) as socket_path:
            client = AppServerClient(socket_path)
            with patch.object(ClientSession, "ws_connect", side_effect=stall_connect):
                start = asyncio.create_task(client.start())
                await connect_started.wait()
                start.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await start
            self.assertIsNone(client._session)

    async def test_send_timeout_is_fatal(self) -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            async for _frame in websocket:
                pass
            return websocket

        async def stall_send(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        async with unix_server(handler) as socket_path:
            async with AppServerClient(socket_path, timeout=0.01) as client:
                with patch.object(ClientWebSocketResponse, "send_json", side_effect=stall_send):
                    with self.assertRaisesRegex(TransportError, "send timed out"):
                        await client.respond("request-1", {})
                with self.assertRaisesRegex(TransportError, "send timed out"):
                    await client.receive()
