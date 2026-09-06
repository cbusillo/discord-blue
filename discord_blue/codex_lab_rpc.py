from __future__ import annotations

import asyncio
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Self

from aiohttp import ClientSession, ClientWebSocketResponse, UnixConnector, WSMsgType

JsonObject = dict[str, Any]
RequestId = str | int


class TransportError(Exception):
    """The outcome of an RPC is unknown because its connection failed."""


class RpcError(Exception):
    """A JSON-RPC request that the server definitively rejected."""

    def __init__(self, code: int | None) -> None:
        self.code = code
        detail = f" (code {code})" if code is not None else ""
        super().__init__(f"JSON-RPC request rejected{detail}")


class AppServerClient:
    """Bounded JSON-RPC client for an app-server Unix websocket."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout: float = 15.0,
        queue_size: int = 100,
        max_msg_size: int = 4 * 1024 * 1024,
    ) -> None:
        path = Path(socket_path)
        if not path.is_absolute():
            raise ValueError("socket_path must be absolute")
        if timeout <= 0 or queue_size <= 0 or max_msg_size <= 0:
            raise ValueError("timeout and bounds must be positive")

        self._socket_path = str(path)
        self._timeout = timeout
        self._max_msg_size = max_msg_size
        self._messages: asyncio.Queue[JsonObject | TransportError] = asyncio.Queue(maxsize=queue_size)
        self._pending: dict[str, asyncio.Future[JsonObject]] = {}
        self._id_prefix = f"discord-blue-{uuid.uuid4().hex}-"
        self._next_id = 0
        self._session: ClientSession | None = None
        self._websocket: ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._failure: TransportError | None = None
        self._started = False

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._started:
            if self._failure is not None:
                raise self._failure
            return
        self._started = True
        try:
            socket_stat = Path(self._socket_path).stat()
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise TransportError("app-server endpoint is not a Unix socket")
            if socket_stat.st_uid != os.getuid():
                raise TransportError("app-server socket has an unexpected owner")
            if socket_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise TransportError("app-server socket is writable by other users")
        except OSError:
            failure = TransportError("could not inspect app-server socket")
            self._mark_failed(failure)
            raise failure from None
        except TransportError as exc:
            self._mark_failed(exc)
            raise
        connector = UnixConnector(path=self._socket_path)
        self._session = ClientSession(connector=connector)
        try:
            async with asyncio.timeout(self._timeout):
                self._websocket = await self._session.ws_connect(
                    "http://localhost/",
                    max_msg_size=self._max_msg_size,
                )
        except asyncio.CancelledError:
            await self._session.close()
            self._session = None
            self._mark_failed(TransportError("app-server connection setup canceled"))
            raise
        except Exception:
            await self._session.close()
            self._session = None
            failure = TransportError("could not connect to app-server")
            self._mark_failed(failure)
            raise failure from None
        self._reader = asyncio.create_task(self._reader_loop(), name="discord-blue-app-server-reader")

    async def initialize(
        self,
        client_info: JsonObject | None = None,
        *,
        experimental_api: bool = True,
    ) -> JsonObject:
        result = await self.request(
            "initialize",
            {
                "clientInfo": client_info or {"name": "discord_blue", "title": "Discord Blue", "version": "0.2.0"},
                "capabilities": {"experimentalApi": experimental_api},
            },
        )
        await self._send({"method": "initialized"})
        return result

    async def request(self, method: str, params: JsonObject | None = None) -> JsonObject:
        self._require_connected()
        request_id = f"{self._id_prefix}{self._next_id}"
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: JsonObject = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params

        try:
            await self._send(message)
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            future.cancel()
            failure = TransportError("JSON-RPC request timed out")
            await self._terminate(failure)
            raise failure from None
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            future.cancel()
            raise
        except TransportError:
            if future.done() and not future.cancelled():
                future.exception()
            raise
        finally:
            self._pending.pop(request_id, None)

    async def receive(self) -> JsonObject:
        if self._failure is not None and self._messages.empty():
            raise self._failure
        item = await self._messages.get()
        self._messages.task_done()
        if isinstance(item, TransportError):
            raise item
        return item

    async def respond(self, request_id: RequestId, result: JsonObject | None = None) -> None:
        if isinstance(request_id, bool) or not isinstance(request_id, str | int):
            raise ValueError("request_id must be a string or integer")
        await self._send({"id": request_id, "result": result or {}})

    async def close(self) -> None:
        async with self._close_lock:
            self._mark_failed(TransportError("app-server client closed"))
            reader, self._reader = self._reader, None
            if reader is not None and reader is not asyncio.current_task():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            await self._close_transport()

    def _require_connected(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._websocket is None or self._websocket.closed:
            raise TransportError("app-server client is not connected")

    async def _send(self, message: JsonObject) -> None:
        self._require_connected()
        assert self._websocket is not None
        try:
            async with asyncio.timeout(self._timeout):
                async with self._send_lock:
                    await self._websocket.send_json(message)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            failure = TransportError("JSON-RPC send timed out")
            await self._terminate(failure)
            raise failure from None
        except Exception:
            failure = TransportError("could not send JSON-RPC message")
            await self._terminate(failure)
            raise failure from None

    async def _reader_loop(self) -> None:
        assert self._websocket is not None
        try:
            async for frame in self._websocket:
                if frame.type is not WSMsgType.TEXT:
                    raise TransportError("app-server sent an invalid websocket frame")
                try:
                    payload = frame.json()
                except (TypeError, ValueError):
                    raise TransportError("app-server sent malformed JSON") from None
                self._route(payload)
        except asyncio.CancelledError:
            return
        except TransportError as exc:
            self._mark_failed(exc)
        except Exception:
            self._mark_failed(TransportError("app-server connection failed"))
        else:
            self._mark_failed(TransportError("app-server disconnected"))
        finally:
            await self._close_transport()

    def _route(self, payload: object) -> None:
        if not isinstance(payload, dict) or payload.get("jsonrpc", "2.0") != "2.0":
            raise TransportError("app-server sent an invalid JSON-RPC message")
        method = payload.get("method")
        if isinstance(method, str) and method:
            request_id = payload.get("id")
            if "id" in payload and (isinstance(request_id, bool) or not isinstance(request_id, str | int)):
                raise TransportError("app-server sent an invalid JSON-RPC request")
            try:
                self._messages.put_nowait(payload)
            except asyncio.QueueFull:
                raise TransportError("app-server message queue overflow") from None
            return

        request_id = payload.get("id")
        has_result = "result" in payload
        has_error = "error" in payload
        if not isinstance(request_id, str) or has_result == has_error:
            raise TransportError("app-server sent an invalid JSON-RPC response")
        result = payload.get("result")
        error = payload.get("error")
        if has_result and not isinstance(result, dict):
            raise TransportError("app-server sent an invalid JSON-RPC result")
        if has_error and not isinstance(error, dict):
            raise TransportError("app-server sent an invalid JSON-RPC error")

        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        if has_error:
            assert isinstance(error, dict)
            code = error.get("code")
            future.set_exception(RpcError(code if isinstance(code, int) and not isinstance(code, bool) else None))
        else:
            assert isinstance(result, dict)
            future.set_result(result)

    async def _terminate(self, failure: TransportError) -> None:
        self._mark_failed(failure)
        await self.close()

    def _mark_failed(self, failure: TransportError) -> None:
        if self._failure is not None:
            return
        self._failure = failure
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._pending.clear()
        while not self._messages.empty():
            self._messages.get_nowait()
            self._messages.task_done()
        self._messages.put_nowait(failure)

    async def _close_transport(self) -> None:
        websocket = self._websocket
        session = self._session
        if websocket is not None and not websocket.closed:
            await websocket.close()
        if session is not None and not session.closed:
            await session.close()
        if self._websocket is websocket:
            self._websocket = None
        if self._session is session:
            self._session = None
