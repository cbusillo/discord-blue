"""The client side of one Discord Blue agent session, shared by the local bridges.

A client owns one session identity (``session_id`` plus a per-process
``session_epoch``), queues events in a bounded outbox, and keeps one WebSocket to
the agent-session server: hello and hello_ack, heartbeats, controls, and
reconnects with the same identity. Subclasses supply the hello fields and
execute controls; ``Rejected`` carries a reason that is safe to show in Discord.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import OrderedDict, deque
from typing import Any, ClassVar, Protocol

import aiohttp

Json = dict[str, Any]
logger = logging.getLogger(__name__)

PROMPT_EVENTS = {"approval_request", "request_user_input"}
STATUS_EVENTS = {"status_changed", "turn_complete", "error"}
OUTBOX_LIMIT = 256
COMMAND_MEMORY = 1024


class ConnectionConfig(Protocol):
    @property
    def server_url(self) -> str: ...
    @property
    def token(self) -> str: ...
    @property
    def host_label(self) -> str: ...
    @property
    def heartbeat_seconds(self) -> float: ...
    @property
    def reconnect_seconds(self) -> float: ...
    @property
    def hello_timeout_seconds(self) -> float: ...


class Rejected(Exception):
    """A control that was not executed, with a reason safe to show in Discord."""


class AgentSessionClient:
    capabilities: ClassVar[list[str]] = []
    # Errors a command may raise besides Rejected; `failure_reason` turns them into Discord text.
    command_errors: ClassVar[tuple[type[Exception], ...]] = ()

    def __init__(self, config: ConnectionConfig, session_id: str) -> None:
        self.config = config
        self.session_id = session_id
        self.epoch = uuid.uuid4().hex
        self.outbox: deque[Json] = deque()
        self.wakeup = asyncio.Event()
        self.stopped = asyncio.Event()
        self.websocket: aiohttp.ClientWebSocketResponse | None = None
        # Prompts still waiting on an answer, replayed once after every reconnect.
        self.prompts: dict[Any, Json] = {}
        self.commands: OrderedDict[str, Json] = OrderedDict()
        self.last_status: Json | None = None

    # Events to Discord

    def event(self, kind: str, **fields: object) -> Json:
        return {"type": kind, "session_id": self.session_id, "session_epoch": self.epoch, **fields}

    def publish(self, kind: str, **fields: object) -> None:
        event = self.event(kind, **fields)
        if kind in STATUS_EVENTS:
            self.last_status = event
        self.enqueue(event)

    def enqueue(self, event: Json) -> None:
        if len(self.outbox) >= OUTBOX_LIMIT:
            logger.warning("Dropping the oldest queued event for session %s", self.session_id)
            self.outbox.popleft()
        self.outbox.append(event)
        self.wakeup.set()

    def hello(self, *, first: bool) -> Json:
        raise NotImplementedError

    # Controls from Discord

    async def handle_control(self, message: Json) -> Json | None:
        if message.get("type") == "approval_decision":
            return await self.approval_decision(message)
        if message.get("type") != "command":
            return None
        command_id = message.get("command_id")
        if not self.is_current(message):
            return self.event("command_reject", command_id=command_id, reason="Stale session; the command was not executed.")
        if not isinstance(command_id, str) or not command_id:
            return self.event("command_reject", command_id=command_id, reason="Invalid command ID.")
        if command_id in self.commands:
            return self.commands[command_id]
        self.commands[command_id] = self.event("command_reject", command_id=command_id, reason="Command already in progress.")
        try:
            await self.run_command(message)
            response = self.event("command_ack", command_id=command_id)
        except Rejected as exc:
            response = self.event("command_reject", command_id=command_id, reason=str(exc))
        except self.command_errors as exc:
            response = self.event("command_reject", command_id=command_id, reason=self.failure_reason(exc))
        self.commands[command_id] = response
        while len(self.commands) > COMMAND_MEMORY:
            self.commands.popitem(last=False)
        return response

    def is_current(self, message: Json) -> bool:
        return message.get("session_id") == self.session_id and message.get("session_epoch") == self.epoch

    async def run_command(self, message: Json) -> None:
        raise NotImplementedError

    def failure_reason(self, exc: Exception) -> str:
        return str(exc)

    async def approval_decision(self, message: Json) -> Json:
        reason = "This client does not accept approval decisions."
        return self.event("approval_decision_reject", approval_id=str(message.get("approval_id") or ""), reason=reason)

    def status_snapshot(self) -> Json:
        return dict(self.last_status or self.event("status_changed", message="Connected"))

    # Connection

    async def send(self, websocket: aiohttp.ClientWebSocketResponse, message: Json) -> None:
        async with asyncio.timeout(15):
            await websocket.send_json(message)

    async def run(self, http: aiohttp.ClientSession) -> None:
        headers = {"Authorization": f"Bearer {self.config.token}"}
        first = True
        while not self.stopped.is_set():
            try:
                async with http.ws_connect(self.config.server_url, headers=headers, max_msg_size=1024 * 1024) as websocket:
                    self.websocket = websocket
                    await self.send(websocket, self.hello(first=first))
                    async with asyncio.timeout(self.config.hello_timeout_seconds):
                        ack = await websocket.receive_json()
                    if not isinstance(ack, dict) or ack.get("type") != "hello_ack":
                        raise ValueError("Discord Blue did not acknowledge the session")
                    first = False
                    # Prompts retire on disconnect and on every status event, so replay queued history
                    # first and then each still-pending prompt once.
                    self.outbox = deque([*(e for e in self.outbox if e["type"] not in PROMPT_EVENTS), *self.prompts.values()])
                    self.wakeup.set()
                    await self.serve(websocket)
            except aiohttp.WSServerHandshakeError as exc:
                logger.error("Discord Blue refused session %s (HTTP %s); check server_url and token", self.session_id, exc.status)
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError, TypeError) as exc:
                logger.warning("Discord Blue connection for session %s ended: %s", self.session_id, type(exc).__name__)
            finally:
                self.websocket = None
            try:
                await asyncio.wait_for(self.stopped.wait(), self.config.reconnect_seconds)
            except TimeoutError:
                pass

    async def serve(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        async def deliver() -> None:
            while True:
                await self.wakeup.wait()
                self.wakeup.clear()
                # A closed socket would lose the event; it stays queued for the next connection.
                while self.outbox and not websocket.closed:
                    await self.send(websocket, self.outbox.popleft())

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self.config.heartbeat_seconds)
                await self.send(websocket, self.event("heartbeat"))

        async def controls() -> None:
            async for frame in websocket:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    message = json.loads(frame.data)
                except ValueError:
                    continue
                if isinstance(message, dict) and (response := await self.handle_control(message)) is not None:
                    await self.send(websocket, response)

        tasks = [asyncio.create_task(fn()) for fn in (deliver, heartbeat, controls)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self.stopped.set()
        if self.websocket is not None:
            await self.websocket.close()
