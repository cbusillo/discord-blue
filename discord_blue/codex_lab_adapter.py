"""Opt-in development adapter for one daemon-backed native Codex Lab TUI thread."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import sys
import uuid
from collections import deque
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from discord_blue.codex_lab_rpc import AppServerClient, RpcError, TransportError

Json = dict[str, Any]
OUTPUT_LIMIT = 32_000
LOCAL_ONLY = "Use the native TUI for approvals, input, autonomous continuation, and new chats."


def assistant_text(turn: Json) -> str:
    return "\n\n".join(
        item["text"]
        for item in turn.get("items", [])
        if item.get("type") == "agentMessage" and isinstance(item.get("text"), str) and isinstance(item.get("id"), str)
    )[:OUTPUT_LIMIT]


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != "/agent-session/connect":
        raise ValueError("Use a credential-free /agent-session/connect URL without query or fragment.")
    if not parsed.hostname:
        raise ValueError("Discord bridge URL needs a host.")
    if parsed.scheme == "wss":
        return
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme != "ws" or not loopback:
        raise ValueError("Use wss; unencrypted ws is allowed only for a literal loopback address.")


class DevAdapter:
    def __init__(
        self,
        rpc: AppServerClient,
        thread_id: str,
        *,
        command_limit: int = 10_000,
        heartbeat_interval: float = 30,
        reconnect_delay: float = 2,
        io_timeout: float = 15,
    ) -> None:
        self.rpc = rpc
        self.thread_id = thread_id
        self.epoch = str(uuid.uuid4())
        self.command_limit = command_limit
        if min(heartbeat_interval, reconnect_delay, io_timeout, command_limit) <= 0:
            raise ValueError("Adapter timing and bounds must be positive.")
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_delay = reconnect_delay
        self.io_timeout = io_timeout
        self.send_lock = asyncio.Lock()
        self.local_requests: set[str | int] = set()
        self.commands: dict[str, tuple[str, Json]] = {}
        self.completed: set[str] = set()
        self.answers: dict[str, dict[str, str]] = {}
        self.outbox: deque[Json] = deque()
        self.available = asyncio.Event()
        self.stopped = asyncio.Event()
        self.hello: Json = {}
        self.status = "Connected to native TUI. " + LOCAL_ONLY

    def event(self, kind: str, **fields: object) -> Json:
        return {"type": kind, "session_id": self.thread_id, "session_epoch": self.epoch, **fields}

    def publish(self, kind: str, **fields: object) -> None:
        if len(self.outbox) >= 256:
            raise TransportError("Discord output backlog is full; adapter stopped without replaying controls.")
        self.outbox.append(self.event(kind, **fields))
        self.available.set()

    async def attach(self) -> None:
        await self.rpc.initialize()
        try:
            result = await self.rpc.request(
                "thread/resume",
                {
                    "threadId": self.thread_id,
                    "excludeTurns": True,
                    "initialTurnsPage": {"limit": 1, "itemsView": "summary", "sortDirection": "desc"},
                },
            )
        except RpcError as exc:
            raise TransportError("Cannot attach thread. Start its first turn in the daemon-backed TUI and check its ID.") from exc
        thread = result.get("thread", {})
        if thread.get("id") != self.thread_id or thread.get("parentThreadId") or thread.get("canAcceptDirectInput") is not True:
            raise TransportError("Attach requires an existing root thread that accepts direct input.")
        self.hello = self.event(
            "hello",
            host_label="Codex Lab dev adapter",
            cwd=thread.get("cwd", ""),
            branch=(thread.get("gitInfo") or {}).get("branch"),
            pid=os.getpid(),
        )
        if not self.hello.get("branch"):
            self.hello.pop("branch", None)
        for turn in (result.get("initialTurnsPage") or {}).get("data", []):
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise TransportError("Invalid app-server turn snapshot.")
            if turn.get("status") != "inProgress":
                self.completed.add(turn["id"])
                self.hello["assistant_message"] = assistant_text(turn)

    async def app_events(self) -> None:
        while True:
            event = await self.rpc.receive()
            params = event.get("params", {})
            if not isinstance(params, dict) or params.get("threadId") != self.thread_id:
                continue
            method = event.get("method")
            if "id" in event:
                # A second subscriber must not answer for, or disclose prompts from, the native TUI.
                if len(self.local_requests) >= 100:
                    raise TransportError("Too many pending local requests.")
                self.local_requests.add(event["id"])
                self.status = "Action required in the native TUI. " + LOCAL_ONLY
                self.publish("status_changed", message=self.status)
            elif method == "serverRequest/resolved":
                self.local_requests.discard(params.get("requestId"))
                if not self.local_requests:
                    self.status = "Local request resolved; see native TUI for its outcome."
                    self.publish("status_changed", message=self.status)
            elif method == "turn/started":
                self.status = "Turn in progress"
                self.publish("status_changed", message=self.status)
            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    self.remember_answer(params.get("turnId"), item)
            elif method == "turn/completed":
                turn = params.get("turn", {})
                turn_id = turn.get("id")
                if not isinstance(turn_id, str) or turn_id in self.completed:
                    continue
                if len(self.completed) >= 10_000:
                    raise TransportError("Session event limit reached; restart the adapter for a new epoch.")
                for item in turn.get("items", []):
                    if item.get("type") == "agentMessage":
                        self.remember_answer(turn_id, item)
                text = "\n\n".join(self.answers.pop(turn_id, {}).values())
                self.completed.add(turn_id)
                if len(text) > OUTPUT_LIMIT:
                    text = text[:OUTPUT_LIMIT] + "\n[Output truncated; see the native TUI.]"
                self.status = "Turn " + str(turn.get("status", "finished"))
                self.local_requests.clear()
                self.publish("turn_complete", message=self.status, assistant_message=text)
            elif method == "error":
                self.publish("error", message="Codex Lab reported an error; see the native TUI for details.")

    def remember_answer(self, turn_id: object, item: Json) -> None:
        if not isinstance(turn_id, str) or turn_id in self.completed or not isinstance(item.get("text"), str):
            return
        if len(self.answers) >= 64 and turn_id not in self.answers:
            raise TransportError("Too many incomplete turns.")
        parts = self.answers.setdefault(turn_id, {})
        item_id = item.get("id")
        if isinstance(item_id, str) and len(parts) >= 256 and item_id not in parts:
            raise TransportError("Too many assistant items in one turn.")
        if isinstance(item_id, str) and (item_id in parts or sum(map(len, parts.values())) <= OUTPUT_LIMIT):
            parts[item_id] = item["text"][: OUTPUT_LIMIT + 1]

    async def command(self, message: Json) -> Json:
        command_id = message.get("command_id")
        reject = self.event("command_reject", command_id=command_id, reason=LOCAL_ONLY)
        if message.get("type") == "approval_decision":
            reject = self.event("approval_decision_reject", approval_id=message.get("approval_id"), reason=LOCAL_ONLY)
        if message.get("session_id") != self.thread_id or message.get("session_epoch") != self.epoch:
            return {**reject, "reason": "Stale session identity; command was not executed."}
        if message.get("type") == "approval_decision":
            return reject
        if not isinstance(command_id, str) or not command_id or len(command_id) > 128:
            return {**reject, "reason": "Invalid command ID."}
        fingerprint = hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
        if command_id in self.commands:
            previous, response = self.commands[command_id]
            return response if previous == fingerprint else {**reject, "reason": "Command ID reused with different content."}
        if len(self.commands) >= self.command_limit:
            return {**reject, "reason": "Command limit reached; restart adapter for a new epoch."}
        # Reserve before any await. Ambiguous RPC failures terminate the process; they are never retried.
        self.commands[command_id] = (fingerprint, {**reject, "reason": "Command already in progress."})
        kind = message.get("kind")
        response = self.event("command_ack", command_id=command_id)
        try:
            if kind in {"reply", "pause_current_turn"}:
                state = await self.rpc.request("thread/read", {"threadId": self.thread_id, "includeTurns": False})
                thread = state.get("thread", {})
                if thread.get("id") != self.thread_id or thread.get("canAcceptDirectInput") is not True:
                    raise ValueError("Thread no longer accepts direct input.")
                status = thread.get("status", {}).get("type")
                active_id = None
                if status == "active":
                    page = await self.rpc.request(
                        "thread/turns/list",
                        {"threadId": self.thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"},
                    )
                    turns = page.get("data", [])
                    if turns and turns[0].get("status") == "inProgress":
                        active_id = turns[0].get("id")
                    if not isinstance(active_id, str):
                        raise ValueError("Active turn changed; check status and send a new command.")
                elif status != "idle":
                    raise ValueError("Thread is not ready; check the native TUI.")
                if kind == "pause_current_turn":
                    if active_id is None:
                        raise ValueError("There is no active turn to pause.")
                    await self.rpc.request("turn/interrupt", {"threadId": self.thread_id, "turnId": active_id})
                else:
                    text = message.get("text")
                    if not isinstance(text, str) or not text.strip() or len(text) > 16_000:
                        raise ValueError("Reply must contain 1 to 16000 characters.")
                    params: Json = {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]}
                    if active_id is not None:
                        params["expectedTurnId"] = active_id
                    await self.rpc.request("turn/steer" if active_id else "turn/start", params)
            elif kind == "status_request":
                await self.rpc.request("thread/read", {"threadId": self.thread_id, "includeTurns": False})
                self.publish("status_changed", message=self.status)
            elif kind == "end_session":
                self.stopped.set()
            else:
                response = reject
        except RpcError:
            response = {**reject, "reason": "Codex Lab rejected the command; check the native TUI and send a new command."}
        except ValueError as exc:
            response = {**reject, "reason": str(exc)}
        self.commands[command_id] = (fingerprint, response)
        return response

    async def send(self, ws: aiohttp.ClientWebSocketResponse, message: Json) -> None:
        # Bound both lock acquisition and drain; a blocked Discord writer must reconnect.
        async with asyncio.timeout(self.io_timeout):
            async with self.send_lock:
                await ws.send_json(message)

    async def deliver(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await self.available.wait()
            while self.outbox:
                # Discord has no event-delivery acknowledgement. Do not replay an ambiguous write.
                event = self.outbox.popleft()
                await self.send(ws, event)
            # No await between checking the empty queue and clearing its wakeup.
            self.available.clear()

    async def controls(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for frame in ws:
            if frame.type != aiohttp.WSMsgType.TEXT:
                raise TransportError("Unexpected Discord WebSocket frame.")
            try:
                message = json.loads(frame.data)
            except ValueError as exc:
                raise TransportError("Invalid Discord JSON.") from exc
            if not isinstance(message, dict):
                raise TransportError("Invalid Discord message.")
            if message.get("type") in {"command", "approval_decision"}:
                response = await self.command(message)
                await self.send(ws, response)
                if self.stopped.is_set():
                    await self.send(
                        ws, self.event("status_changed", message="Discord adapter detached; native TUI session continues.")
                    )
                    return

    async def heartbeats(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            # A healthy adapter alone must not keep a dead app-server looking connected.
            await self.rpc.request("thread/read", {"threadId": self.thread_id, "includeTurns": False})
            await self.send(ws, self.event("heartbeat"))

    async def discord(self, url: str, token: str) -> None:
        validate_url(url)
        if not token.strip():
            raise ValueError("Bridge token environment variable is empty.")
        first_connection = True
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20)) as session:
            while not self.stopped.is_set():
                try:
                    async with session.ws_connect(url, headers={"Authorization": "Bearer " + token}, max_msg_size=128_000) as ws:
                        hello = dict(self.hello)
                        if not first_connection:
                            hello.pop("assistant_message", None)
                        await self.send(ws, hello)
                        async with asyncio.timeout(self.io_timeout):
                            frame = await ws.receive()
                        if frame.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                            raise ConnectionError("Discord closed before hello acknowledgement.")
                        if frame.type != aiohttp.WSMsgType.TEXT:
                            raise TransportError("Invalid Discord hello acknowledgement frame.")
                        try:
                            ack = json.loads(frame.data)
                        except ValueError as exc:
                            raise TransportError("Invalid Discord hello acknowledgement JSON.") from exc
                        if not isinstance(ack, dict) or ack.get("type") != "hello_ack":
                            raise TransportError("Discord did not acknowledge the session.")
                        first_connection = False
                        await self.send(ws, self.event("status_changed", message=self.status))
                        tasks = [asyncio.create_task(fn(ws)) for fn in (self.controls, self.deliver, self.heartbeats)]
                        try:
                            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                task.result()
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                except aiohttp.WSServerHandshakeError as exc:
                    if exc.status in {401, 403, 404}:
                        raise TransportError("Discord rejected authentication or endpoint; check adapter configuration.") from exc
                except (aiohttp.ClientError, TimeoutError, OSError):
                    pass
                if not self.stopped.is_set():
                    await asyncio.sleep(self.reconnect_delay)


async def run(socket_path: str, thread_id: str, url: str, token: str) -> None:
    validate_url(url)
    if not token.strip():
        raise ValueError("Bridge token environment variable is empty.")
    async with AppServerClient(socket_path) as rpc:
        adapter = DevAdapter(rpc, thread_id)
        await adapter.attach()
        tasks = [asyncio.create_task(adapter.app_events()), asyncio.create_task(adapter.discord(url, token))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, help="Absolute path to the shared native TUI's app-server Unix socket")
    parser.add_argument("--thread-id", required=True, help="Exact existing root thread ID, after its first turn")
    parser.add_argument("--bridge-url", required=True, help="wss://HOST/agent-session/connect")
    parser.add_argument("--token-env", default="AGENT_SESSION_TOKEN", help="Environment variable containing the bridge token")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.socket, args.thread_id, args.bridge_url, os.environ.get(args.token_env, "")))
    except KeyboardInterrupt:
        pass
    except (TransportError, RpcError, ValueError, OSError, aiohttp.ClientError):
        # Exception strings from HTTP/RPC libraries can contain credentials or user content.
        print(
            "Adapter stopped. Check socket ownership, daemon health, a materialized thread ID, and bridge configuration.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        main()
