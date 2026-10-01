"""Attach to the stock Codex app-server daemon as one extra client and mirror its live threads.

Each loaded root thread gets a Discord session. The bridge subscribes with
``thread/resume`` (no config overrides, which can restart an idle thread cold)
only while a turn runs or a prompt is pending, and unsubscribes afterwards.
Stock broadcasts ``thread/status/changed`` to every client, so the bridge rejoins
when a turn starts, and stock replays pending requests on the join. Once the TUI
closes and nobody is subscribed, stock unloads the thread and reports
``notLoaded``; the bridge then ends the Discord session, which archives its thread.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.codex_bridge.rpc import AppServerClient, RpcError, TransportError
from discord_blue.codex_bridge.session import Rejected, Rpc, ThreadSession, latest_turn

Json = dict[str, Any]
logger = logging.getLogger(__name__)
UNNAMED = "it has no name or preview yet"

CLIENT_INFO = {"name": "discord_blue_codex_bridge", "title": "Discord Blue Codex bridge", "version": "0.1.0"}
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
        self.retries: dict[str, asyncio.Task[None]] = {}
        self.joining: set[str] = set()
        # The newest status change that arrived while a retried join was reading its thread, applied once it joins.
        self.late_status: dict[str, Json] = {}

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
        while True:
            await self.dispatch(await rpc.receive())

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
            await self.join(str((params.get("thread") or {}).get("id") or ""))
        elif method == "thread/status/changed":
            status = params.get("status") or {}
            if session is not None:
                await self.apply_status(session, status)
            elif thread_id in self.joining:
                self.late_status[thread_id] = status
            else:
                await self.join(thread_id)
        elif session is None:
            if method == "thread/name/updated":
                # Naming a thread nobody has mirrored yet is also the first sign that it is in use.
                await self.join(thread_id)
            return
        elif method == "thread/closed":
            await self.detach(thread_id, ended=True)
        elif method == "thread/name/updated":
            session.rename(params.get("threadName"))
        elif method == "turn/started":
            session.on_turn_started(str((params.get("turn") or {}).get("id")))
        elif method == "item/completed":
            session.on_item_completed(str(params.get("turnId")), params.get("item") or {})
        elif method == "turn/completed":
            session.on_turn_completed(params.get("turn") or {})
            await session.release()
        elif method == "serverRequest/resolved":
            session.on_resolved(params.get("requestId"))  # type: ignore[arg-type]
            await session.release()

    async def join(self, thread_id: str) -> None:
        """Open a Discord session for a loaded root thread; subscribe only if it is busy."""
        if not thread_id or thread_id in self.sessions or thread_id in self.joining or self.rpc is None:
            return
        rpc = self.rpc
        self.joining.add(thread_id)
        self.late_status.pop(thread_id, None)
        try:
            thread = (await rpc.request("thread/read", {"threadId": thread_id}))["thread"]
            busy = (thread.get("status") or {}).get("type") == "active"
            # Never load a thread from disk, and skip subagents and threads nobody has used yet.
            if skip := self.skip_reason(thread):
                logger.info("Not joining Codex thread %s: %s", thread_id, skip)
                if busy and skip == UNNAMED:
                    self.retry_join(thread_id)
                return
            page = await rpc.request("thread/turns/list", {"threadId": thread_id, "limit": 1, "itemsView": "summary"})
        except RpcError as exc:
            logger.info("Not joining Codex thread %s: %s", thread_id, exc)
            return
        finally:
            self.joining.discard(thread_id)
            late = self.late_status.pop(thread_id, None)
        if thread_id in self.sessions or rpc is not self.rpc:
            return
        session = ThreadSession(self.config, rpc, thread, latest_turn(page))
        assert self.http is not None
        self.sessions[thread_id] = session
        self.tasks[thread_id] = asyncio.create_task(session.run(self.http), name=f"codex-bridge-{thread_id}")
        logger.info("Mirroring Codex thread %s (%s)", thread_id, session.label.current)
        if (thread.get("status") or {}).get("type") == "active":
            await self.subscribe(session)
        if late is not None:
            await self.apply_status(session, late)

    async def apply_status(self, session: ThreadSession, status: Json) -> None:
        if status.get("type") == "notLoaded":
            # Stock unloads a thread once no client is subscribed: its TUI has closed.
            await self.detach(session.thread_id, ended=True)
            return
        session.on_status(status)
        if status.get("type") == "active":
            await self.subscribe(session)
        elif status.get("type") == "idle":
            # Covers a turn that finished before the join landed, which sends no turn/completed here.
            # Stock reports idle before turn/completed; a turn still being tracked holds the join.
            await session.release()

    @staticmethod
    def skip_reason(thread: Json) -> str | None:
        if thread.get("parentThreadId"):
            return "it is a subagent thread"
        if thread.get("ephemeral"):
            return "it is ephemeral"
        if (thread.get("status") or {}).get("type") == "notLoaded":
            return "it is not loaded"
        if not (thread.get("name") or thread.get("preview")):
            return UNNAMED
        return None

    def retry_join(self, thread_id: str) -> None:
        """Read a busy unnamed thread again shortly: its prompt is recorded just after its first turn starts."""
        if thread_id not in self.retries:
            self.retries[thread_id] = asyncio.create_task(self.rejoin_unnamed(thread_id), name=f"codex-bridge-retry-{thread_id}")

    async def rejoin_unnamed(self, thread_id: str) -> None:
        try:
            for delay in self.config.unnamed_retry_seconds:
                await asyncio.sleep(delay)
                if thread_id in self.sessions:
                    return
                await self.join(thread_id)
            if thread_id not in self.sessions:
                logger.info("Codex thread %s still has no name or preview; it is joined when it is named or goes idle", thread_id)
        finally:
            if self.retries.get(thread_id) is asyncio.current_task():
                del self.retries[thread_id]

    async def subscribe(self, session: ThreadSession) -> None:
        try:
            await session.subscribe()
        except (Rejected, RpcError) as exc:
            logger.info("Not subscribing to Codex thread %s: %s", session.thread_id, exc)

    async def detach(self, thread_id: str, *, ended: bool = False) -> None:
        """Stop mirroring a thread. An ended thread closes in Discord now; otherwise (the daemon went away) it waits
        out Discord Blue's grace period for this bridge to reconnect."""
        session, task = self.sessions.pop(thread_id, None), self.tasks.pop(thread_id, None)
        if session is not None:
            await (session.end() if ended else session.stop())
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def detach_all(self) -> None:
        retries = list(self.retries.values())
        self.retries.clear()
        self.late_status.clear()
        for retry in retries:
            retry.cancel()
        await asyncio.gather(*retries, return_exceptions=True)
        for thread_id in list(self.sessions):
            await self.detach(thread_id)
        self.rpc = None
