"""Attach to the stock Codex app-server daemon as one extra client and mirror its live threads.

Joining uses ``thread/resume`` with no config overrides: overrides can shut an idle
thread down and resume it cold. On a loaded thread, resume only adds this
connection as a subscriber and replays a read-only goal snapshot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp

from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.codex_bridge.rpc import AppServerClient, RpcError, TransportError
from discord_blue.codex_bridge.session import Rpc, ThreadSession, final_answer

Json = dict[str, Any]
logger = logging.getLogger(__name__)

CLIENT_INFO = {"name": "discord_blue_codex_bridge", "title": "Discord Blue Codex bridge", "version": "0.1.0"}
SWEEP_SECONDS = 60.0
# Streaming deltas are never mirrored; opting out keeps the bounded receive queue small.
QUIET_NOTIFICATIONS = [
    "item/agentMessage/delta",
    "item/plan/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta",
    "item/commandExecution/outputDelta",
    "item/commandExecution/terminalInteraction",
    "item/fileChange/outputDelta",
    "item/fileChange/patchUpdated",
    "item/mcpToolCall/progress",
    "command/exec/outputDelta",
    "process/outputDelta",
    "thread/tokenUsage/updated",
    "account/rateLimits/updated",
    "turn/diff/updated",
    "turn/plan/updated",
    "fs/changed",
]


class CodexBridge:
    def __init__(self, config: BridgeConfig, *, connect: Callable[[], AppServerClient] | None = None) -> None:
        self.config = config
        self.connect = connect or (lambda: AppServerClient(config.socket_path, queue_size=1000))
        self.rpc: Rpc | None = None
        self.http: aiohttp.ClientSession | None = None
        self.sessions: dict[str, ThreadSession] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        # Threads released after a long idle stay detached until they become active again.
        self.released: set[str] = set()

    async def run(self) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=20)) as http:
            self.http = http
            while True:
                try:
                    async with self.connect() as rpc:
                        await rpc.initialize(CLIENT_INFO, opt_out_notification_methods=QUIET_NOTIFICATIONS)
                        self.rpc = rpc
                        await self.discover()
                        await self.pump(rpc)
                except (TransportError, RpcError, OSError) as exc:
                    logger.warning("Codex app-server connection ended: %s", exc)
                finally:
                    await self.detach_all()
                await asyncio.sleep(self.config.reconnect_seconds)

    async def discover(self) -> None:
        assert self.rpc is not None
        cursor = None
        while True:
            page = await self.rpc.request("thread/loaded/list", {"cursor": cursor})
            for thread_id in page.get("data") or []:
                await self.join(str(thread_id))
            if not (cursor := page.get("nextCursor")):
                return

    async def pump(self, rpc: AppServerClient) -> None:
        last_sweep = time.monotonic()
        while True:
            try:
                message = await asyncio.wait_for(rpc.receive(), SWEEP_SECONDS)
            except TimeoutError:
                message = None
            if message is not None:
                await self.dispatch(message)
            if time.monotonic() - last_sweep >= SWEEP_SECONDS:
                last_sweep = time.monotonic()
                await self.release_idle()

    async def dispatch(self, message: Json) -> None:
        method = str(message.get("method"))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        assert isinstance(params, dict)
        thread_id = str(params.get("threadId") or "")
        session = self.sessions.get(thread_id)
        if "id" in message:
            if session is not None:
                session.on_request(message["id"], method, params)
            return
        if method == "thread/started":
            started = str((params.get("thread") or {}).get("id") or "")
            self.released.discard(started)
            await self.join(started)
        elif method == "thread/status/changed":
            if session is not None:
                session.on_status(params.get("status") or {})
                return
            if (params.get("status") or {}).get("type") == "active":
                self.released.discard(thread_id)
            await self.join(thread_id)
        elif session is None:
            return
        elif method == "thread/closed":
            await self.detach(thread_id)
        elif method == "thread/name/updated":
            session.title = params.get("threadName") or session.title
        elif method == "turn/started":
            session.on_turn_started(str((params.get("turn") or {}).get("id")))
        elif method == "item/completed":
            session.on_item_completed(str(params.get("turnId")), params.get("item") or {})
        elif method == "turn/completed":
            session.on_turn_completed(params.get("turn") or {})
        elif method == "serverRequest/resolved":
            session.on_resolved(params.get("requestId"))  # type: ignore[arg-type]

    async def join(self, thread_id: str) -> None:
        if not thread_id or thread_id in self.sessions or thread_id in self.released or self.rpc is None:
            return
        try:
            thread = (await self.rpc.request("thread/read", {"threadId": thread_id}))["thread"]
            # Never load a thread from disk, and skip subagents and threads nobody has used yet.
            if thread.get("parentThreadId") or thread.get("ephemeral") or (thread.get("status") or {}).get("type") == "notLoaded":
                return
            if not (thread.get("name") or thread.get("preview")):
                return
            await self.rpc.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
            page = await self.rpc.request("thread/turns/list", {"threadId": thread_id, "limit": 1, "itemsView": "summary"})
        except RpcError as exc:
            logger.info("Not joining Codex thread %s: %s", thread_id, exc)
            return
        latest = (page.get("data") or [{}])[0]
        items = [
            (i.get("phase"), i["text"])
            for i in latest.get("items") or []
            if i.get("type") == "agentMessage" and isinstance(i.get("text"), str)
        ]
        active = latest.get("id") if latest.get("status") == "inProgress" else None
        session = ThreadSession(
            self.config, self.rpc, thread, active_turn_id=active, last_answer=None if active else final_answer(items)
        )
        assert self.http is not None
        self.sessions[thread_id] = session
        self.tasks[thread_id] = asyncio.create_task(session.run(self.http), name=f"codex-bridge-{thread_id}")
        logger.info("Mirroring Codex thread %s (%s)", thread_id, session.title)

    async def detach(self, thread_id: str) -> None:
        session, task = self.sessions.pop(thread_id, None), self.tasks.pop(thread_id, None)
        if session is not None:
            await session.stop()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def detach_all(self) -> None:
        for thread_id in list(self.sessions):
            await self.detach(thread_id)
        self.released.clear()
        self.rpc = None

    async def release_idle(self) -> None:
        """Unsubscribe from long-idle threads so a closed TUI's thread can unload."""
        now = time.monotonic()
        for thread_id, session in list(self.sessions.items()):
            if session.idle_since is None or session.prompts or now - session.idle_since < self.config.idle_release_seconds:
                continue
            await self.detach(thread_id)
            self.released.add(thread_id)
            if self.rpc is not None:
                await self.rpc.request("thread/unsubscribe", {"threadId": thread_id})
