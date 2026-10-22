from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import time
import weakref
from collections import Counter
from contextlib import suppress
from functools import partial
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
from aiohttp import WSMsgType, web

from discord_blue.doodads.agent_session.cards import ANSWER_CARD_COMPONENT_ID
from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT
from discord_blue.doodads.agent_session.formatting import advance_code_fence
from discord_blue.doodads.agent_session.formatting import format_user_message
from discord_blue.doodads.agent_session.formatting import is_assistant_message
from discord_blue.doodads.agent_session.messages import send_agent_session_message
from discord_blue.doodads.agent_session.messages import send_assistant_message
from discord_blue.doodads.agent_session.protocol import (
    SERVER_FEATURES,
    RemoteApprovalRequest,
    RemoteRequestUserInput,
    SessionHello,
    SessionStatus,
    UserMessage,
)
from discord_blue.doodads.agent_session.sessions import (
    AgentSession,
    AgentSessionRegistry,
)
from discord_blue.doodads.agent_session import threads as threads_module
from discord_blue.doodads.agent_session.cleanup import (
    MAINTENANCE_DISCOVERY_TIMEOUT_SECONDS,
    PENDING_CLEANUP_MAX_ATTEMPTS,
    SESSION_FINALIZATION_TIMEOUT_SECONDS,
    SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS,
    SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS,
    THREAD_CLOSE_WAIT_SECONDS,
    SessionCleanups,
)
from discord_blue.doodads.agent_session.controls import (
    REACTION_COMPACTING,
    REACTION_FINISHED,
    REACTION_IN_PROGRESS,
    REACTION_REJECTED,
    SessionControls,
)
from discord_blue.doodads.agent_session.interactions import SessionInteractions

from discord_blue.doodads.agent_session.discovery import DiscoveryIndex
from discord_blue.doodads.agent_session.store import SessionState, SessionStore, StoredSession
from discord_blue.doodads.agent_session.thread_worker import RenameTarget, ThreadWorkers
from discord_blue.doodads.agent_session.threads import SessionThread
from discord_blue.doodads.agent_session.threads import SESSION_NOTIFICATION_PREFIXES, SESSION_START_PREFIX
from discord_blue.doodads.agent_session.threads import auto_join_configured_users
from discord_blue.doodads.agent_session.threads import ThreadCreationStopped, create_session_thread
from discord_blue.doodads.agent_session.threads import creation_token_suffix
from discord_blue.doodads.agent_session.threads import new_creation_token
from discord_blue.doodads.agent_session.threads import session_notification_message
from discord_blue.doodads.agent_session.threads import distinct_thread_name
from discord_blue.doodads.agent_session.threads import session_start_message
from discord_blue.health import health_payload
from discord_blue.plugs.discord_plug import BlueBot

# Only complete harness envelopes at the start of a prose line are presentation markup.
# Inline examples, indented code and arbitrary XML remain conversation text.
INJECTED_BLOCK = re.compile(
    r" {0,3}<(system-reminder|task-notification|agent-message|channel)(?=[\s>])[^<>]*>"
    r"(?:([ \t]*\r?\n.*?)^ {0,3}</\1>|([^\r\n]*?)</\1>)",
    re.MULTILINE | re.DOTALL,
)
CLIPPED_REMINDER = re.compile(r" {0,3}<system-reminder>[ \t]*\r?\n")
TASK_METADATA = re.compile(r"<(task-id|tool-use-id|output-file|usage)>.*?(?:</\1>|\Z)", re.DOTALL)
TASK_FIELDS = re.compile(r"</?(?:status|summary|note|event|result)>")


def filter_injected_tags(text: str) -> str:
    """Render envelopes from user-message input; assistant text already has known provenance."""

    def render(block: re.Match[str]) -> str:
        tag = block.group(1)
        body = block.group(2) if block.group(2) is not None else block.group(3)
        if tag == "system-reminder":
            return ""
        if tag == "task-notification":
            body = TASK_FIELDS.sub("", TASK_METADATA.sub("", body)).strip()
            return f"Task notification:\n{body}" if body else ""
        if tag == "agent-message":
            return f"Agent message:\n{body.strip()}"
        return body.strip()

    output: list[str] = []
    fence = None
    position = 0
    while position < len(text):
        if fence is None:
            if envelope := INJECTED_BLOCK.match(text, position):
                output.append(render(envelope))
                position = envelope.end()
                # Envelope bodies are independent text, so their fences cannot hide later envelopes.
                continue
            if CLIPPED_REMINDER.match(text, position) and (
                position == 0 or text.endswith("\n[Truncated; see the Claude Code terminal.]")
            ):
                break  # A standalone reminder or the client's explicitly clipped reminder tail.
        newline = text.find("\n", position)
        end = len(text) if newline == -1 else newline + 1
        line = text[position:end]
        fence = advance_code_fence(line.rstrip("\r\n"), fence)
        output.append(line)
        position = end
    return "".join(output).strip()


logger = logging.getLogger(__name__)
# A connection that drops without a clean session_end (including a failed hello_ack) keeps its thread untouched
# this long, so a reconnect resumes it without an archive, member changes or notices.
SESSION_DISCONNECT_GRACE_SECONDS = 300
SESSION_STORE_WAIT_TIMEOUT_SECONDS = 1
SESSION_ENDED_NOTICE = "Session ended"

STARTUP_RECONNECT_GRACE_SECONDS = 20
# Before giving up on an incomplete discovery (a failed read or listing page), refresh it again after these waits.
DISCOVERY_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0)
DISCORD_UNKNOWN_CHANNEL = 10003
# After a start, sessions from before it reconnect over this long, and until they do nothing marks their threads as
# theirs, so the maintenance sweeps leave unbound threads and notifications alone for this long.
STARTUP_SWEEP_HOLD_SECONDS = 600
SESSION_WEBSOCKET_CLOSE_TIMEOUT_SECONDS = 1
MAINTENANCE_INTERVAL_SECONDS = 300
SHUTDOWN_WEBSOCKET_CLOSE_TIMEOUT_SECONDS = SESSION_WEBSOCKET_CLOSE_TIMEOUT_SECONDS
SHUTDOWN_RUNNER_CLEANUP_TIMEOUT_SECONDS = 5
SHUTDOWN_ATTACH_SETTLE_SECONDS = 10
SHUTDOWN_THREAD_CLEANUP_TIMEOUT_SECONDS = SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS + SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS

AGENT_SESSION_CONNECT_PATH = "/agent-session/connect"


class DiscoveryIncomplete(discord.DiscordException):
    """Discovery could not read every candidate, so it cannot say the session has no thread; nothing is created."""


class CandidateGone(Exception):
    """The thread discovery chose no longer exists on Discord."""

    def __init__(self, thread_id: int) -> None:
        super().__init__(thread_id)
        self.thread_id = thread_id


@dataclasses.dataclass(slots=True)
class CreationCheck:
    created_id: int
    parent_id: int | None
    guild: discord.Guild
    attempts: int = 0


HELLO_PENDING_INTERVAL_SECONDS = 15


class BridgeStopping(ThreadCreationStopped):
    """The bridge is shutting down, so an attach does not start."""


class AgentSessionBridge:
    def __init__(self, bot: BlueBot, *, store_path: Path | None = None) -> None:
        self.bot = bot
        from discord_blue.doodads.agent_session.conversation import ConversationRouter

        self.conversations = ConversationRouter(bot)
        # The container sets HOME to its existing durable /var/lib/discord-blue mount.
        self.store = SessionStore.for_path(store_path if store_path is not None else Path.home() / "agent-sessions.json")
        self.sessions = AgentSessionRegistry()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        # One FIFO admission queue for attaches, including creation; a rate-limited create keeps its slot.
        self._session_attach_lock = asyncio.Lock()
        self._creation_stopping = asyncio.Event()
        self._grace_tasks: set[asyncio.Task[None]] = set()
        # Every change to a session thread (reopen, join, members, notice, archive, leave, rename) goes through its
        # worker, one request at a time and never cancelled.
        self.threads = ThreadWorkers.for_client(bot, self)
        self.discovery = DiscoveryIndex(self.bot_user_id)
        # Creations whose duplicate check could not finish, by token; maintenance retries them.
        self._creation_checks: dict[str, CreationCheck] = {}
        # No await occurs while resolving the entry, so one event loop turn
        # cannot create two locks for the same session ID.
        self._session_lifecycle_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self.cleanups = SessionCleanups(self, self._session_attach_lock)
        # Threads an attach is reopening and has not bound yet; no close may touch them meanwhile.
        self._attaching_threads: Counter[int] = Counter()
        # One attach per session ID; a reconnecting socket joins the one already running instead of starting over.
        self._attach_tasks: dict[str, asyncio.Task[SessionThread]] = {}
        # Each attached thread as the attach resolved it, for handlers while discord.py's cache lacks it.
        self._attached_threads: dict[int, discord.Thread] = {}
        self.controls = SessionControls(self)
        self.interactions = SessionInteractions(self)
        self._monitor_started_at = time.monotonic()
        self._monitor_last_progress = self._monitor_started_at
        self._monitor_has_run = False
        self._maintenance_last_progress = self._monitor_started_at
        self._maintenance_has_run = False

        self._stopping = False

    def session_lifecycle_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_lifecycle_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_lifecycle_locks[session_id] = lock
        return lock

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._stopping = False
        self._monitor_started_at = time.monotonic()
        self._monitor_last_progress = self._monitor_started_at
        self._monitor_has_run = False
        self._maintenance_last_progress = self._monitor_started_at
        self._maintenance_has_run = False

        await self.store.start()
        now = time.time()
        for session_id, record in list(self.store.records.items()):
            if record.status in {"attaching", "live", "grace"}:
                self.store.put(
                    session_id,
                    dataclasses.replace(
                        record,
                        status="grace",
                        grace_until=max(record.grace_until, now + SESSION_DISCONNECT_GRACE_SECONDS),
                        updated_at=now,
                    ),
                )
            elif record.status == "closed" and now - record.updated_at > timedelta(days=30).total_seconds():
                self.store.forget(session_id)
        logger.info(
            "Loaded %s Agent session recovery records; reconnect grace is %s s",
            len(self.store.records),
            SESSION_DISCONNECT_GRACE_SECONDS,
        )

        app = web.Application()
        self.register_routes(app)
        self._runner = web.AppRunner(app, shutdown_timeout=SHUTDOWN_RUNNER_CLEANUP_TIMEOUT_SECONDS)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            self.bot.config.agent_session.listen_host,
            self.bot.config.agent_session.listen_port,
        )
        await self._site.start()
        logger.info(
            "Agent session bridge listening on %s:%s",
            self.bot.config.agent_session.listen_host,
            self.bot.config.agent_session.listen_port,
        )
        if not self.operator_role_name():
            logger.warning(
                "No operator role is configured (agent_session.operator_role_name or discord.employee_role_name):"
                " approvals, session controls and thread replies are refused for everyone"
            )
        self._cleanup_task = asyncio.create_task(self.cleanup_stale_sessions())
        self._heartbeat_task = asyncio.create_task(self.monitor_heartbeats())

    def register_routes(self, app: web.Application) -> None:
        app.router.add_get("/health", self.handle_health)
        app.router.add_get(AGENT_SESSION_CONNECT_PATH, self.handle_connect)
        app.router.add_get("/agent-session/conversation", self.handle_conversation_connect)

    async def handle_conversation_connect(self, request: web.Request) -> web.WebSocketResponse:
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        websocket = web.WebSocketResponse(max_msg_size=65536)
        await websocket.prepare(request)
        try:
            async with asyncio.timeout(15):
                hello = await websocket.receive_json()
            if isinstance(hello, dict) and hello.get("type") == "hello" and not self._stopping:
                await self.conversations.serve(websocket, hello)
        except (ValueError, TypeError, TimeoutError, ConnectionError):
            pass
        finally:
            if not websocket.closed:
                await websocket.close(message=b"conversation disconnected", drain=False)
        return websocket

    async def handle_health(self, _request: web.Request) -> web.Response:
        discord_ready = self.discord_ready()
        gateway = self.bot.gateway_health.evidence(ready=discord_ready)
        discord_healthy = gateway["status"] == "ok"
        monitor = self.agent_session_monitor_health()
        monitor_healthy = not self.bot.config.agent_session.enabled or monitor["status"] not in {"dead", "stalled"}
        return web.json_response(
            health_payload(
                discord_status="ok" if discord_healthy else "unhealthy",
                discord_gateway=gateway,
                agent_session_enabled=self.bot.config.agent_session.enabled,
                active_agent_sessions=len(self.sessions.live_sessions()),
                disconnected_agent_sessions=len(self.sessions.disconnected_sessions()),
                pending_agent_session_cleanups=len(self.cleanups.pending),
                agent_session_monitor=monitor,
            ),
            status=200 if discord_healthy and monitor_healthy else 503,
        )

    def background_task_health(
        self,
        task: asyncio.Task[None] | None,
        *,
        last_progress: float,
        has_run: bool,
        stall_threshold: float,
    ) -> dict[str, object]:
        elapsed = max(0.0, time.monotonic() - last_progress)
        if self._stopping:
            status = "stopped"
        elif task is not None and task.done():
            status = "dead"
        elif elapsed > stall_threshold:
            status = "stalled"
        elif not has_run:
            status = "starting"
        else:
            status = "ok"
        return {
            "status": status,
            "seconds_since_progress": round(elapsed, 3),
        }

    def agent_session_monitor_health(self) -> dict[str, object]:
        heartbeat_interval = getattr(self.bot.config.agent_session, "heartbeat_check_interval_seconds", 30)
        heartbeat = self.background_task_health(
            self._heartbeat_task,
            last_progress=self._monitor_last_progress,
            has_run=self._monitor_has_run,
            stall_threshold=heartbeat_interval + SESSION_FINALIZATION_TIMEOUT_SECONDS + 5,
        )
        maintenance = self.background_task_health(
            self._cleanup_task,
            last_progress=self._maintenance_last_progress,
            has_run=self._maintenance_has_run,
            stall_threshold=(
                (STARTUP_RECONNECT_GRACE_SECONDS if not self._maintenance_has_run else MAINTENANCE_INTERVAL_SECONDS)
                + MAINTENANCE_DISCOVERY_TIMEOUT_SECONDS
                + SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS
                + 5
            ),
        )
        persistence = {
            "status": "unhealthy" if self.store.unhealthy(MAINTENANCE_INTERVAL_SECONDS) else "ok",
            "records": len(self.store.records),
        }
        statuses = {heartbeat["status"], maintenance["status"], persistence["status"]}
        if "unhealthy" in statuses or "dead" in statuses:
            status = "dead"
        elif "stalled" in statuses:
            status = "stalled"
        elif self._stopping:
            status = "stopped"
        elif "starting" in statuses:
            status = "starting"
        else:
            status = "ok"
        return {"status": status, "heartbeat": heartbeat, "maintenance": maintenance, "store": persistence}

    def discord_ready(self) -> bool:
        if self.bot.user is None:
            return False
        is_closed = getattr(self.bot, "is_closed", None)
        if callable(is_closed) and is_closed():
            return False
        is_ready = getattr(self.bot, "is_ready", None)
        return not callable(is_ready) or bool(is_ready())

    async def stop(self) -> None:
        await self.conversations.stop()
        if self._runner is None:
            return
        self._stopping = True
        self._creation_stopping.set()
        for task in list(self._grace_tasks):
            task.cancel()
        await self.stop_background_task("maintenance", self._cleanup_task)
        self._cleanup_task = None
        await self.stop_background_task("heartbeat", self._heartbeat_task)
        self._heartbeat_task = None
        # Queued attaches stop at the lock and put back the sessions they replaced, so shutdown saves those too. They
        # are waited for, not cancelled: one may be sending a Discord request.
        if self._attach_tasks:
            await asyncio.wait(list(self._attach_tasks.values()), timeout=SHUTDOWN_ATTACH_SETTLE_SECONDS)
        await self.disconnect_active_sessions()
        self.threads.stop()
        try:
            await asyncio.wait_for(self._runner.cleanup(), timeout=SHUTDOWN_RUNNER_CLEANUP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("Agent session bridge runner cleanup timed out during shutdown")
        finally:
            self._runner = None
            self._site = None
            try:
                await self.threads.bounded(self.store.close(), SESSION_STORE_WAIT_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("Agent session store is still flushing at shutdown")

    @staticmethod
    async def stop_background_task(name: str, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            logger.warning("Agent session %s task failed before shutdown: %r", name, result)

    async def disconnect_active_sessions(self) -> None:
        async with self._session_attach_lock:
            sessions = list(self.sessions.by_session.values())

        close_tasks = [asyncio.create_task(self.disconnect_active_session(session.session_id, session)) for session in sessions]
        results = await asyncio.gather(*close_tasks, return_exceptions=True)
        for session, result in zip(sessions, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Unable to disconnect Agent session %s during shutdown: %r", session.session_id, result)

    async def disconnect_active_session(self, session_id: str, session: AgentSession) -> None:
        del session_id
        if session.ended:
            await self.finalize_session(session, close_message=b"bridge shutdown", shutdown=True)
            return
        async with self.session_lifecycle_lock(session.session_id):
            if self.sessions.get(session.session_id) is not session:
                return
            self.save_session(session, "grace", grace_until=time.time() + SESSION_DISCONNECT_GRACE_SECONDS)
            # Retain ownership until the socket closes: its handler's finally must not finalize this thread.
            if not session.websocket.closed:
                await self.close_session_websocket(
                    session.session_id, session, message=b"bridge shutdown", timeout=SHUTDOWN_WEBSOCKET_CLOSE_TIMEOUT_SECONDS
                )
            self.sessions.remove_if_current(session)
            logger.info("Kept Agent session %s thread %s open for restart", session.session_id, session.thread_id)

    def save_session(self, session: AgentSession, status: SessionState, *, grace_until: float = 0) -> None:
        if session.thread_id is not None:
            self.store.put(
                session.session_id,
                StoredSession(
                    session.thread_id,
                    session.notification_message_id,
                    session_start_message(session.hello),
                    status,
                    grace_until,
                    time.time(),
                ),
            )

    def stored_thread_protected(self, thread_id: int) -> bool:
        if self.sessions.get_by_thread(thread_id) is not None:
            return False  # In-memory ownership already protects it, and its duplicate notices still need cleanup.
        return any(
            record.thread_id == thread_id
            and record.status in {"attaching", "live", "grace"}
            and (record.status != "grace" or record.grace_until > time.time())
            for record in self.store.records.values()
        )

    async def finalize_session(
        self,
        session: AgentSession,
        *,
        close_message: bytes = b"session disconnected",
        shutdown: bool = False,
    ) -> bool:
        lifecycle_lock = self.session_lifecycle_lock(session.session_id)
        async with lifecycle_lock:
            removed = self.sessions.remove_if_current(session)
            if removed is None:
                return False
            if removed.thread_id is not None:
                self._attached_threads.pop(removed.thread_id, None)

            fallback_cleanup = self.cleanups.pending_cleanup_for_session(removed)
            self.save_session(removed, "closing")
            self.cleanups.finalizing.add(fallback_cleanup.key)
            try:
                websocket_timeout = SHUTDOWN_WEBSOCKET_CLOSE_TIMEOUT_SECONDS if shutdown else SESSION_WEBSOCKET_CLOSE_TIMEOUT_SECONDS
                if not removed.websocket.closed:
                    await self.close_session_websocket(
                        removed.session_id,
                        removed,
                        message=close_message,
                        timeout=websocket_timeout,
                    )

                try:
                    async with asyncio.timeout(SHUTDOWN_THREAD_CLEANUP_TIMEOUT_SECONDS):
                        # Only waits are bounded here; every Discord request inside runs to completion.
                        residual = await self.cleanups.close_session_thread(removed, fallback_cleanup)
                except TimeoutError:
                    logger.warning(
                        "Agent session thread cleanup for %s timed out%s",
                        removed.session_id,
                        " during shutdown" if shutdown else "",
                    )
                    residual = fallback_cleanup
                if residual is not None:
                    self.cleanups.save_cleanup(residual)
                    self.cleanups.remember_pending_cleanup(residual)
                else:
                    self.save_session(removed, "closed")
                return True
            except asyncio.CancelledError:
                self.cleanups.save_cleanup(fallback_cleanup)
                self.cleanups.remember_pending_cleanup(fallback_cleanup)
                raise
            except Exception:
                self.cleanups.save_cleanup(fallback_cleanup)
                self.cleanups.remember_pending_cleanup(fallback_cleanup)
                raise
            finally:
                self.cleanups.finalizing.discard(fallback_cleanup.key)

    @staticmethod
    async def close_session_websocket(
        session_id: str,
        session: AgentSession,
        *,
        message: bytes,
        timeout: float,
    ) -> None:
        try:
            await asyncio.wait_for(
                session.websocket.close(message=message, drain=False),
                timeout=timeout,
            )
        except Exception:
            logger.warning("Unable to close Agent session websocket %s", session_id, exc_info=True)

    async def cleanup_stale_sessions(self) -> None:
        await asyncio.sleep(STARTUP_RECONNECT_GRACE_SECONDS)
        while True:
            self.record_maintenance_progress()
            try:
                self.store.retry_failed_write()
                await self.cleanups.recover_stored_cleanups()
                await self.cleanups.retry_pending_cleanups()
                await self.retry_creation_checks()
                if time.monotonic() - self._monitor_started_at >= STARTUP_SWEEP_HOLD_SECONDS:
                    await self.cleanups.cleanup_stale_session_notifications()
                    await self.cleanups.cleanup_stale_session_threads()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent session maintenance iteration failed")
            finally:
                self._maintenance_has_run = True
                self.record_maintenance_progress()
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)

    async def monitor_heartbeats(self) -> None:
        while True:
            await asyncio.sleep(self.bot.config.agent_session.heartbeat_check_interval_seconds)
            self.record_monitor_progress()
            try:
                await self.close_timed_out_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent session heartbeat iteration failed")
            finally:
                self._monitor_has_run = True
                self.record_monitor_progress()

    def record_monitor_progress(self) -> None:
        self._monitor_last_progress = time.monotonic()

    def record_maintenance_progress(self) -> None:
        self._maintenance_last_progress = time.monotonic()

    async def close_timed_out_sessions(self) -> None:
        timeout = timedelta(seconds=self.bot.config.agent_session.heartbeat_timeout_seconds)
        now = datetime.now(UTC)
        for session_id, session in list(self.sessions.by_session.items()):
            if self.sessions.get(session_id) is not session:
                continue  # Replaced while this sweep ran: the newer connection has its own clock.
            if session.grace_task is not None or not session.acknowledged:
                continue  # In grace (its timer ends it) or still attaching (heartbeats start at the ack).
            if not session.websocket.closed and now - session.last_seen <= timeout:
                continue
            lifecycle_lock = self.session_lifecycle_lock(session_id)
            if lifecycle_lock.locked():
                continue
            try:
                if not session.websocket.closed:
                    logger.warning(
                        "Agent session %s timed out after %s seconds without heartbeat",
                        session_id,
                        self.bot.config.agent_session.heartbeat_timeout_seconds,
                    )
                # A silent or closed connection is a drop, not an end: close it and start the grace period.
                await self.end_connection(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unable to finalize stale Agent session %s", session_id)
            finally:
                self.record_monitor_progress()

    def attaching(self) -> bool:
        """An attach is under way, so a thread or notification without a session may be about to get one."""
        return self._session_attach_lock.locked() or bool(self._attach_tasks)

    async def handle_connect(self, request: web.Request) -> web.WebSocketResponse:
        if not self._authorized(request):
            raise web.HTTPUnauthorized()

        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        session: AgentSession | None = None

        try:
            async for message in websocket:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    logger.warning(
                        "Invalid Agent session bridge JSON type=%s length=%s",
                        type(message.data).__name__,
                        len(message.data),
                    )
                    continue
                if not isinstance(payload, dict):
                    logger.warning("Ignoring non-object Agent session bridge JSON type=%s", type(payload).__name__)
                    continue

                message_type = payload.get("type")
                if message_type == "hello" and "conversation" in payload:
                    await websocket.close(message=b"use conversation endpoint", drain=False)
                    break
                if (
                    message_type == "hello"
                    and self.bot.config.conversation.enabled
                    and (payload.get("session_id") == self.bot.config.conversation.session_id)
                ):
                    await websocket.close(message=b"conversation cannot attach as mirrored session", drain=False)
                    break
                if message_type != "hello" and (
                    session is None
                    or self.sessions.get(session.session_id) is not session
                    or payload.get("session_id") != session.session_id
                    or payload.get("session_epoch") != session.session_epoch
                ):
                    logger.warning(
                        "Ignoring Agent session event %r for %r/%r outside connection %r/%r",
                        message_type,
                        payload.get("session_id"),
                        payload.get("session_epoch"),
                        session.session_id if session is not None else None,
                        session.session_epoch if session is not None else None,
                    )
                    continue
                if message_type == "hello":
                    if session is not None:
                        logger.warning("Rejecting a second hello on Agent session connection %s", session.session_id)
                        await websocket.close(message=b"hello already received", drain=False)
                        break
                    try:
                        hello = SessionHello.from_payload(payload)
                    except (KeyError, TypeError, ValueError, OverflowError):
                        logger.warning("Rejecting invalid Agent session hello")
                        await websocket.close(message=b"invalid hello", drain=False)
                        break
                    if self._stopping:
                        await websocket.close(message=b"bridge shutdown", drain=False)
                        break
                    # The newest connection is the session's from now on; the attach answers only it.
                    previous = self.sessions.get(hello.session_id)
                    session = AgentSession(hello=hello, websocket=websocket)
                    self.sessions.register(session)
                    task = self.attach_task(hello, previous)
                    try:
                        if hello.hello_pending:
                            # Wait without cancelling the attach or an in-flight discord.py request. Only the
                            # current socket receives progress; events remain queued until the final ack.
                            while not task.done():
                                done, _ = await asyncio.wait({task}, timeout=HELLO_PENDING_INTERVAL_SECONDS)
                                if done:
                                    break
                                if self.sessions.get(hello.session_id) is not session or websocket.closed:
                                    raise ConnectionError("session left while waiting for a thread")
                                await websocket.send_json(
                                    {
                                        "type": "hello_pending",
                                        "session_id": hello.session_id,
                                        "session_epoch": hello.session_epoch,
                                        "message": "Waiting for a thread",
                                    }
                                )
                        session_thread = await asyncio.shield(task)
                    except ThreadCreationStopped:
                        await websocket.close(message=b"bridge shutdown", drain=False)
                        break
                    except ConnectionError:
                        logger.info("Agent session %s left while waiting for a thread", hello.session_id)
                        if self.sessions.get(hello.session_id) is session:
                            # A dropped socket still owns this attach. Let it bind before end_connection decides
                            # between grace and finalization; otherwise it would archive the just-created thread.
                            with suppress(discord.DiscordException, ValueError, ThreadCreationStopped):
                                await asyncio.shield(task)
                        break
                    except (discord.DiscordException, ValueError):
                        await websocket.close(message=b"unable to attach Discord thread", drain=False)
                        break
                    if self.sessions.get(hello.session_id) is not session:
                        # A newer connection of this session joined the attach while this one waited; it gets the ack.
                        await websocket.close(message=b"replaced by a newer connection", drain=False)
                        break
                    if session.thread_id != session_thread.thread.id:
                        # This connection joined after the attach bound the thread to an earlier one: it is the
                        # thread's owner now, so it is bound before it is acknowledged.
                        self.sessions.bind_thread(hello.session_id, session_thread.thread.id, session_thread.notification_message_id)
                    try:
                        await websocket.send_json(
                            {
                                "type": "hello_ack",
                                "features": sorted(SERVER_FEATURES),
                                "thread_id": session_thread.thread.id,
                                **({"capabilities": sorted(hello.capabilities)} if hello.capabilities is not None else {}),
                            }
                        )
                    except ConnectionError:
                        # The client stopped waiting; it will reconnect, so this starts a grace period below.
                        logger.info("Agent session %s left before its hello was acknowledged", hello.session_id)
                        break
                    # Heartbeats start after the ack, so the watchdog's clock starts here too.
                    session.acknowledged = True
                    session.touch()
                    if self.sessions.get(session.session_id) is session and not self._stopping:
                        self.save_session(session, "live")
                    # Attached, acknowledged and outside every lock: bring the thread's name up to date in the
                    # background. The thread's worker coalesces and rate-limits; nothing here waits on Discord.
                    self.request_thread_name(session)
                elif message_type in {"approval_resolved", "request_user_input_resolved"}:
                    await self.interactions.handle_prompt_resolved(message_type, payload)
                elif message_type == "heartbeat" and session is not None:
                    session.touch()
                elif message_type == "user_message":
                    user_message = UserMessage.from_payload(payload)
                    await self.handle_user_message(user_message)
                elif message_type in {"status_changed", "turn_complete", "error"}:
                    status = SessionStatus.from_payload(payload)
                    await self.handle_session_status(message_type, status)
                elif message_type == "approval_request":
                    approval = RemoteApprovalRequest.from_payload(payload)
                    await self.interactions.handle_approval_request(approval)
                elif message_type == "request_user_input":
                    request_user_input = RemoteRequestUserInput.from_payload(payload)
                    await self.interactions.handle_request_user_input(request_user_input)
                elif message_type == "approval_decision_ack":
                    logger.info("Agent session approval decision ack: %s", payload.get("approval_id"))
                    await self.interactions.handle_approval_decision_ack(payload)
                elif message_type == "approval_decision_reject":
                    logger.warning("Agent session approval decision reject: %s", payload)
                    await self.interactions.handle_approval_decision_reject(payload)
                elif message_type == "session_end" and session is not None:
                    # A clean end: close the thread now instead of waiting out a grace period.
                    session.ended = True
                    break
                elif message_type == "title_changed" and session is not None:
                    await self.handle_title_changed(session, payload.get("title"))
                elif message_type == "notice" and session is not None and session.thread_id is not None:
                    if isinstance(notice := payload.get("message"), str) and notice.strip():
                        await self.post_thread_notice(session.thread_id, notice)
                elif message_type == "command_ack":
                    logger.info("Agent session command ack: %s", payload.get("command_id"))
                    await self.controls.handle_command_ack(payload)
                elif message_type == "command_reject":
                    logger.warning("Agent session command reject: %s", payload)
                    await self.controls.handle_command_reject(payload)
        finally:
            if session is not None:
                await self.end_connection(session)

        return websocket

    def attach_task(self, hello: SessionHello, previous: AgentSession | None) -> asyncio.Task[SessionThread]:
        """The session's running attach, or a new one; it outlives the sockets that wait for it."""
        task = self._attach_tasks.get(hello.session_id)
        if task is None:
            task = asyncio.create_task(self.attach(hello, previous), name=f"agent-session-attach-{hello.session_id}")
            self._attach_tasks[hello.session_id] = task
            task.add_done_callback(partial(self.attach_done, hello.session_id))
        return task

    def attach_done(self, session_id: str, task: asyncio.Task[SessionThread]) -> None:
        if self._attach_tasks.get(session_id) is task:
            del self._attach_tasks[session_id]
        if not task.cancelled():
            task.exception()  # Retrieved here; every socket that still waits gets it too.

    async def attach(self, hello: SessionHello, previous: AgentSession | None) -> SessionThread:
        """Find, reopen or create the session's thread and bind it to whichever connection is current by then.

        The first hello's identity decides discovery; a reconnecting socket joins rather than starting again, so
        progress survives a client that stops waiting for its ack.
        """
        session_id = hello.session_id
        async with self.session_lifecycle_lock(session_id), self._session_attach_lock:
            try:
                if self._stopping:
                    raise BridgeStopping
                session_thread = await self.resume_thread_in_grace(previous, hello) or await self.find_or_create_session_thread(
                    hello
                )
            except (discord.DiscordException, ValueError, ThreadCreationStopped) as exc:
                if not isinstance(exc, ThreadCreationStopped):
                    logger.warning(
                        "Unable to attach Discord thread for Agent session %s: %s",
                        session_id,
                        type(exc).__name__,
                        exc_info=True,
                    )
                current = self.sessions.get(session_id)
                if current is not None and current is not previous and current.thread_id is None:
                    self.sessions.remove_if_current(current)
                self.restore_replaced(previous)
                raise
            if previous is not None:
                # Provisional replacement can fail and restore this connection, so only retire its views after attach succeeds.
                self.sessions.stop_approval_views(previous)
            if previous is not None and previous.grace_task is not None:
                # Only now: had the attach failed, the old session's timer still closes its thread.
                previous.grace_task.cancel()
            self.sessions.bind_thread(session_id, session_thread.thread.id, session_thread.notification_message_id)
            self._attached_threads[session_thread.thread.id] = session_thread.thread
            current = self.sessions.get(session_id)
            if current is not None:
                self.save_session(current, "attaching")
                try:
                    await self.threads.bounded(self.store.flush(), SESSION_STORE_WAIT_TIMEOUT_SECONDS)
                except TimeoutError:
                    logger.info("Agent session %s attached while its recovery record is still flushing", session_id)
                except OSError as exc:
                    # The health endpoint reports the persistence failure; discovery still permits reconnects.
                    self.store.error = exc
                    logger.warning("Agent session %s attached without a durable recovery record", session_id)
            try:
                await self.backfill_latest_assistant_message(session_thread.thread, hello)
            except Exception:
                # Only the thread's history is short; the session is attached all the same.
                logger.warning("Unable to backfill Agent session thread %s", session_thread.thread.id, exc_info=True)
        return session_thread

    async def end_connection(self, session: AgentSession) -> None:
        """A connection closed: a clean end closes the thread now; any other drop waits out a grace period."""
        if self.sessions.get(session.session_id) is not session:
            # Replaced: the thread is the newer connection's now. Should that one fail to attach, this one is put back
            # (restore_replaced), and its grace starts then; ending it here would close the thread under it.
            with suppress(Exception):
                await asyncio.wait_for(session.websocket.close(), timeout=SESSION_WEBSOCKET_CLOSE_TIMEOUT_SECONDS)
            return
        if self._stopping and not session.ended:
            # stop() saves and disconnects it; a server shutdown is not a session_end.
            return
        if session.ended or session.thread_id is None:
            await self.finalize_session(session)
            return
        if not session.websocket.closed:
            with suppress(Exception):
                await asyncio.wait_for(session.websocket.close(), timeout=SESSION_WEBSOCKET_CLOSE_TIMEOUT_SECONDS)
        self.start_grace(session)

    def start_grace(self, session: AgentSession) -> None:
        if session.grace_task is None:
            self.save_session(session, "grace", grace_until=time.time() + SESSION_DISCONNECT_GRACE_SECONDS)
            session.grace_task = asyncio.create_task(self.expire_grace(session), name=f"agent-session-grace-{session.session_id}")
            self._grace_tasks.add(session.grace_task)
            session.grace_task.add_done_callback(self._grace_tasks.discard)

    async def expire_grace(self, session: AgentSession) -> None:
        await asyncio.sleep(SESSION_DISCONNECT_GRACE_SECONDS)
        await self.end_after_grace(session)

    async def end_after_grace(self, session: AgentSession) -> None:
        # A reconnect replaced this session in the registry; the thread belongs to it now.
        if self.sessions.get(session.session_id) is not session:
            return
        try:
            await self.finalize_session(session, close_message=b"disconnect grace expired")
        except asyncio.CancelledError:
            raise
        except Exception:
            # finalize_session kept a pending cleanup, so the maintenance sweep retries the thread.
            logger.warning("Unable to close Agent session %s after its grace period", session.session_id, exc_info=True)

    def restore_replaced(self, previous: AgentSession | None) -> None:
        """A reconnect failed to attach: the session it replaced, if it held a thread, is the session's again.

        Its connection may be open still (it is live again), closed and in grace (its timer still ends it), closed
        before its drop was handled (its grace starts now), or its grace may have run out while the reconnect waited,
        since the timer stands down for a replacement (it is ended now). During shutdown it is only put back: shutdown
        saves every registered session once queued attaches have settled.
        """
        if previous is None or previous.thread_id is None or self.sessions.get(previous.session_id) is not None:
            return
        self.sessions.register(previous)
        if self._stopping or (not previous.websocket.closed and not previous.ended):
            return
        if previous.grace_task is None and not previous.ended:
            self.start_grace(previous)
        elif previous.ended or (previous.grace_task is not None and previous.grace_task.done()):
            # Not awaited: the failed attach still holds this session's lifecycle lock, which finalize needs.
            ending = asyncio.create_task(self.end_after_grace(previous), name=f"agent-session-grace-{previous.session_id}")
            self._grace_tasks.add(ending)
            ending.add_done_callback(self._grace_tasks.discard)

    async def resume_thread_in_grace(self, previous: AgentSession | None, hello: SessionHello) -> SessionThread | None:
        """A reconnect within grace takes its thread back as it is: no discovery, unarchive, joins or notices.

        Returns None, for a normal attach, when the thread changed during grace or cannot be confirmed.
        """
        if previous is None or previous.grace_task is None or previous.thread_id is None:
            return None
        if self.threads.closing(previous.thread_id):
            return None  # Its close already started; a normal attach reopens it once the close's request lands.
        try:
            # Ask Discord, not the cache: a thread deleted during grace can still be cached.
            thread = await self.bot.fetch_channel(previous.thread_id)
        except discord.DiscordException:
            return None
        if not isinstance(thread, discord.Thread) or thread.archived or thread.locked:
            return None
        cached = self.bot.get_channel(thread.id)
        thread = cached if isinstance(cached, discord.Thread) else thread
        self.sessions.bind_thread(hello.session_id, thread.id, previous.notification_message_id)
        return SessionThread(thread=thread, notification_message_id=previous.notification_message_id)

    async def find_or_create_session_thread(self, hello: SessionHello) -> SessionThread:
        try:
            return await self.find_or_create_session_thread_once(hello)
        except CandidateGone as gone:
            # The thread discovery found was deleted meanwhile (a cached listing still named it): forget it and
            # resolve again, which creates a replacement if the session has no other thread.
            self.discovery.forget(gone.thread_id)
            return await self.find_or_create_session_thread_once(hello)

    def forget_stored_hint(self, session_id: str, record: StoredSession) -> None:
        if self.store.records.get(session_id) is record:
            self.store.forget(session_id)

    async def validated_stored_thread(self, session_id: str, record: StoredSession) -> discord.Thread | None:
        try:
            thread = await self.bot.fetch_channel(record.thread_id)
        except discord.Forbidden:
            # Definite lost access makes this hint unusable. Discovery already skips forbidden candidates.
            self.forget_stored_hint(session_id, record)
            self.discovery.forget(record.thread_id)
            return None
        except discord.NotFound:
            self.forget_stored_hint(session_id, record)
            self.discovery.forget(record.thread_id)
            return None
        # Other Discord failures remain failures, not evidence permitting a duplicate creation.
        parent_id = self.bot.config.agent_session.channel_id or self.bot.config.discord.bot_channel_id
        if not isinstance(thread, discord.Thread) or thread.parent_id != parent_id or thread.owner_id != self.bot_user_id():
            self.forget_stored_hint(session_id, record)
            return None
        opening = [
            message.content
            async for message in thread.history(limit=10, oldest_first=True)
            if message.author.id == self.bot_user_id()
        ]
        expected = self.session_start_without_pid(record.marker)
        if expected is None or not any(self.session_start_without_pid(message) == expected for message in opening):
            self.forget_stored_hint(session_id, record)
            return None
        return thread

    async def resume_stored_thread(self, hello: SessionHello) -> SessionThread | None:
        record = self.store.records.get(hello.session_id)
        if record is None:
            return None
        expected = self.session_start_without_pid(session_start_message(hello))
        if self.session_start_without_pid(record.marker) != expected:
            return None  # Retain the stored marker's session identity and metadata; PID changes are tolerated.
        mapped = self.sessions.by_thread.get(record.thread_id)
        if mapped is not None and mapped != hello.session_id:
            return None
        thread = await self.validated_stored_thread(hello.session_id, record)
        if thread is None:
            return None
        self._attaching_threads[thread.id] += 1
        try:
            if thread.archived or thread.locked or self.threads.closing(thread.id) or record.status in {"closing", "closed"}:
                thread = await self.threads.open(thread)
            notification_id = record.notification_id
            try:
                channel = await threads_module.get_agent_session_channel(self.bot)
                if notification_id is not None:
                    try:
                        notification = await channel.fetch_message(notification_id)
                        if (
                            notification.author.id != self.bot_user_id()
                            or threads_module.notification_thread_id(notification.content) != thread.id
                            or self.cleanups.notification_going(notification_id)
                        ):
                            notification_id = None
                    except discord.NotFound:
                        notification_id = None
                if notification_id is None:
                    notification = await send_agent_session_message(channel, session_notification_message(hello, thread))
                    notification_id = notification.id
            except (discord.DiscordException, ValueError):
                logger.warning("Unable to refresh Agent session notification for thread %s; attaching anyway", thread.id)
            self.sessions.bind_thread(hello.session_id, thread.id, notification_id)
            logger.info("Recovered Agent session %s from store in thread %s", hello.session_id, thread.id)
            return SessionThread(thread=thread, notification_message_id=notification_id)
        finally:
            self._attaching_threads[thread.id] -= 1
            if self._attaching_threads[thread.id] <= 0:
                del self._attaching_threads[thread.id]

    async def find_or_create_session_thread_once(self, hello: SessionHello) -> SessionThread:
        if stored := await self.resume_stored_thread(hello):
            return stored
        thread = await self.find_existing_session_thread(hello)
        if thread is None:
            token = new_creation_token()
            session_thread = await create_session_thread(
                self.bot,
                hello,
                token=token,
                settle=partial(self.delete_creation_duplicates, token),
                stopping=self._creation_stopping,
            )
            self.discovery.add(session_thread.thread, [session_start_message(hello)])
            self.sessions.bind_thread(
                hello.session_id,
                session_thread.thread.id,
                session_thread.notification_message_id,
            )
            return session_thread

        mapped_session_id = self.sessions.by_thread.get(thread.id)
        if mapped_session_id is not None and mapped_session_id != hello.session_id:
            raise ValueError(f"Agent session thread {thread.id} is already attached")
        # The thread's worker reopens it after any close request of its last session has landed, never before. From
        # here until it is bound, the thread counts as owned, so a close that is already under way stops.
        self._attaching_threads[thread.id] += 1
        try:
            try:
                thread = await self.threads.open(thread)
            except discord.NotFound as exc:
                if exc.code == DISCORD_UNKNOWN_CHANNEL:
                    raise CandidateGone(thread.id) from exc
                raise
            notification_message_id = await self.ensure_session_notification(hello, thread)
            self.sessions.bind_thread(hello.session_id, thread.id, notification_message_id)
        finally:
            self._attaching_threads[thread.id] -= 1
            if self._attaching_threads[thread.id] <= 0:
                del self._attaching_threads[thread.id]
        return SessionThread(thread=thread, notification_message_id=notification_message_id)

    async def delete_creation_duplicates(self, token: str, created: discord.Thread) -> None:
        """Close other threads of this creation: discord.py retries a create that failed with a 5xx, and Discord may
        have made the first one anyway. Runs before anything is posted, so a duplicate is still empty. If the check
        cannot finish now, maintenance retries it."""
        try:
            done = await self.check_creation_duplicates(token, created.id, created.parent_id, created.guild)
        except Exception:
            logger.warning("Unable to check for duplicate Agent session threads of %s", created.id, exc_info=True)
            done = False
        if not done:
            self._creation_checks[token] = CreationCheck(created.id, created.parent_id, created.guild)

    async def check_creation_duplicates(self, token: str, created_id: int, parent_id: int | None, guild: discord.Guild) -> bool:
        """True once every duplicate is archived and locked; False if listing or an archive failed, to retry later.

        A duplicate is archived rather than deleted: the checks below cannot rule out that someone posts in it
        before the request lands, and an archive can be undone.
        """
        suffix = creation_token_suffix(token)
        bot_user_id = self.bot_user_id()
        try:
            active = await guild.active_threads()
        except Exception:
            logger.warning("Unable to check for duplicate Agent session threads of %s", created_id, exc_info=True)
            return False
        done = True
        for thread in active:
            # Only an empty thread the bot itself created under the same parent, carrying this creation's token: a
            # name alone can be copied, but not ownership, and a session thread has at least its marker.
            if (
                thread.id == created_id
                or thread.parent_id != parent_id
                or thread.owner_id != bot_user_id
                or thread.message_count
                or not (thread.name or "").endswith(suffix)
                or thread.id in self.sessions.by_thread
            ):
                continue
            remaining = await self.threads.close(thread, {"archive", "leave"}, timeout=THREAD_CLOSE_WAIT_SECONDS)
            if "archive" in remaining:
                logger.warning("Unable to archive duplicate Agent session thread %s yet", thread.id)
                done = False
            else:
                logger.info("Archived duplicate Agent session thread %s of %s", thread.id, created_id)
        return done

    async def retry_creation_checks(self) -> None:
        for token, check in list(self._creation_checks.items()):
            self.record_maintenance_progress()
            check.attempts += 1
            try:
                done = await self.check_creation_duplicates(token, check.created_id, check.parent_id, check.guild)
            except Exception:
                logger.warning("Unable to check for duplicate Agent session threads of %s", check.created_id, exc_info=True)
                done = False
            if done:
                self._creation_checks.pop(token, None)
            elif check.attempts >= PENDING_CLEANUP_MAX_ATTEMPTS:
                self._creation_checks.pop(token, None)
                logger.warning("Giving up on duplicate Agent session threads of %s", check.created_id)

    async def ensure_session_notification(self, hello: SessionHello, thread: discord.Thread) -> int | None:
        existing_message_id = await self.find_session_notification_for_thread(thread.id)
        if existing_message_id is not None:
            return existing_message_id
        try:
            channel = await threads_module.get_agent_session_channel(self.bot)
            message = await send_agent_session_message(channel, session_notification_message(hello, thread))
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to create Agent session notification for thread %s", thread.id)
            return None
        return message.id

    async def find_session_notification_for_thread(self, thread_id: int) -> int | None:
        try:
            channel = await threads_module.get_agent_session_channel(self.bot)
        except ValueError:
            return None
        bot_user = self.bot.user
        if bot_user is None:
            return None
        try:
            async for message in channel.history(limit=None):
                if message.author.id != bot_user.id:
                    continue
                if not message.content.startswith(SESSION_NOTIFICATION_PREFIXES):
                    continue
                if self.cleanups.notification_going(message.id):
                    continue  # Going away: the attach posts a new one instead.
                if threads_module.notification_thread_id(message.content) == thread_id:
                    return message.id
        except discord.DiscordException:
            logger.warning("Unable to scan Agent session notifications for thread %s", thread_id)
        return None

    async def find_existing_session_thread(self, hello: SessionHello) -> discord.Thread | None:
        """The session's thread from the shared discovery index; None only when a complete index has no match.

        An incomplete index (a listing page or a candidate's read failed) is refreshed a few times; if it stays
        incomplete the attach fails with DiscoveryIncomplete rather than creating a second thread.
        """
        try:
            channel = await threads_module.get_agent_session_channel(self.bot)
        except ValueError:
            logger.warning("Unable to find reusable Agent session thread: channel is unavailable")
            return None
        for attempt, delay in enumerate((0.0, *DISCOVERY_RETRY_DELAYS_SECONDS)):
            if delay:
                await asyncio.sleep(delay)
            await self.discovery.fresh(channel, force=attempt > 0)
            if self.discovery.complete:
                return await self.match_session_thread(hello, complete=True)
        # Still incomplete: an exact match is still this session's thread (the best of those read), but neither a
        # pid-relaxed match nor "no match" can be trusted, since an unread thread might be the better or only one.
        thread = await self.match_session_thread(hello, complete=False)
        if thread is not None:
            return thread
        raise DiscoveryIncomplete(f"Agent session discovery stayed incomplete for session {hello.session_id}")

    async def match_session_thread(self, hello: SessionHello, *, complete: bool) -> discord.Thread | None:
        expected_starts = self.expected_session_start_messages(hello)
        expected_starts_without_pid = self.session_start_messages_without_pid(expected_starts)
        matches: list[discord.Thread] = []
        pid_relaxed_matches: list[discord.Thread] = []
        skipped_mapped = 0
        entries = self.discovery.entries()
        for entry in entries:
            mapped_session_id = self.sessions.by_thread.get(entry.thread.id)
            if mapped_session_id is not None and mapped_session_id != hello.session_id:
                skipped_mapped += 1
                continue
            opening = entry.opening or []
            if any(message in expected_starts for message in opening):
                matches.append(entry.thread)
            elif expected_starts_without_pid and any(
                self.session_start_without_pid(message) in expected_starts_without_pid for message in opening
            ):
                pid_relaxed_matches.append(entry.thread)
        if not complete:
            pid_relaxed_matches = []
        if len(matches) == 1:
            return matches[0]
        if matches:
            # Rare (an earlier duplicate): the thread with the most conversation wins, as before.
            scored_threads = [(await self.score_session_thread(thread), thread) for thread in matches]
            _, thread = max(scored_threads, key=lambda pair: pair[0])
            return thread
        if len(pid_relaxed_matches) == 1:
            thread = pid_relaxed_matches[0]
            logger.info(
                "Reusing Agent session thread %s despite pid mismatch for cwd=%s branch=%s host=%s",
                thread.id,
                hello.cwd,
                hello.branch or "unknown",
                hello.host_label,
            )
            return thread
        if pid_relaxed_matches:
            logger.warning(
                "Not reusing Agent session thread for cwd=%s branch=%s host=%s because %s pid-relaxed candidates matched",
                hello.cwd,
                hello.branch or "unknown",
                hello.host_label,
                len(pid_relaxed_matches),
            )
        elif complete:
            logger.info(
                "No reusable Agent session thread found for cwd=%s branch=%s host=%s pid=%s "
                "after checking %s candidate(s), skipped_mapped=%s",
                hello.cwd,
                hello.branch or "unknown",
                hello.host_label,
                hello.pid,
                len(entries),
                skipped_mapped,
            )
        return None

    @staticmethod
    def expected_session_start_messages(hello: SessionHello) -> set[str]:
        expected = session_start_message(hello)
        starts = {expected, AgentSessionBridge.legacy_session_start_without_session(expected)}
        if hello.host_label == "Agent":
            starts.add(expected.replace("\nhost: Agent\n", "\nhost: Agent session\n"))
            starts.add(
                AgentSessionBridge.legacy_session_start_without_session(expected).replace(
                    "\nhost: Agent\n", "\nhost: Agent session\n"
                )
            )
        return starts

    @staticmethod
    def legacy_session_start_without_session(content: str) -> str:
        return re.sub(r"\nsession: `[^`]+`\n", "\n", content, count=1)

    @staticmethod
    def session_start_messages_without_pid(starts: set[str]) -> set[str]:
        return {start_without_pid for start in starts if (start_without_pid := AgentSessionBridge.session_start_without_pid(start))}

    @staticmethod
    def session_start_without_pid(content: str) -> str | None:
        lines = content.splitlines()
        if not lines or lines[0] != SESSION_START_PREFIX:
            return None
        if not any(line.startswith("session: `") for line in lines):
            return None
        if not lines[-1].startswith("pid: `"):
            return None
        return "\n".join(lines[:-1])

    async def score_session_thread(self, thread: discord.Thread) -> tuple[int, int, int]:
        assistant_messages = 0
        messages = 0
        try:
            async for message in thread.history(limit=50):
                messages += 1
                if self.is_bot_assistant_message(message):
                    assistant_messages += 1
        except discord.DiscordException:
            logger.warning("Unable to score Agent session thread %s", thread.id)
        return assistant_messages, messages, thread.id

    async def backfill_latest_assistant_message(
        self,
        thread: discord.Thread,
        hello: SessionHello,
    ) -> None:
        if await self.thread_has_assistant_message(thread):
            return
        assistant_message = hello.assistant_message
        if assistant_message is None:
            return
        await send_assistant_message(thread, assistant_message)

    def is_bot_assistant_message(self, message: discord.Message) -> bool:
        bot_user = self.bot.user
        if bot_user is None or message.author.id != bot_user.id:
            return False
        if is_assistant_message(message.content):
            return True
        if any(
            isinstance(component, discord.Container) and component.id == ANSWER_CARD_COMPONENT_ID for component in message.components
        ):
            return True
        # Also recognize marked cards from before the explicit component ID.
        return any(
            isinstance(child, discord.TextDisplay) and is_assistant_message(child.content)
            for component in message.components
            if isinstance(component, discord.Container)
            for child in component.children
        )

    async def thread_has_assistant_message(self, thread: discord.Thread) -> bool:
        try:
            async for message in thread.history(limit=50):
                if self.is_bot_assistant_message(message):
                    return True
        except discord.DiscordException:
            logger.warning("Unable to inspect Agent session assistant history %s", thread.id)
        return False

    async def handle_thread_reaction(
        self,
        thread: discord.Thread,
        message_id: int,
        emoji: str,
        user: discord.User | discord.Member,
    ) -> bool:
        if not self.is_operator(user):
            return False

        session = self.sessions.get_by_thread(thread.id)
        if session is None:
            return False

        if session.control_message_id == message_id:
            return await self.controls.handle_session_control_reaction(session, thread, message_id, emoji, user)

        for approval_id, pending in session.pending_approvals.items():
            if pending.message_id == message_id:
                return await self.interactions.handle_approval_reaction(
                    session,
                    thread,
                    approval_id,
                    emoji,
                    user,
                )
        return False

    async def handle_session_status(self, message_type: str, status: SessionStatus) -> None:
        session = self.sessions.get(status.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Agent session status for unknown session: %s", status.session_id)
            return
        if status.session_epoch != session.session_epoch:
            logger.warning("Agent session status for stale session epoch: %s", status.session_id)
            return
        session.status_revision += 1
        session.last_status_message = status.message
        if message_type == "turn_complete":
            session.display_state = "done"
        elif message_type == "error":
            session.display_state = "failed"
        elif "unconfirmed" in (status.message or "").lower():
            session.display_state = "unknown"
        elif "waiting" in (status.message or "").lower() or (status.message or "").lower() == "turn aborted":
            session.display_state = "waiting"
        else:
            session.display_state = "working"

        if message_type == "status_changed":
            status_message = (status.message or "").lower()
            if status_message == "turn aborted":
                await self.controls.post_session_controls(session)
                return
            elif "compact" in status_message:
                reaction = REACTION_COMPACTING
            else:
                reaction = REACTION_IN_PROGRESS
            await self.interactions.clear_pending_user_inputs(
                session,
                "Agent session is no longer waiting on this prompt.",
            )
            await self.controls.update_session_status_reaction(session, reaction)
            return

        if message_type == "error":
            await self.interactions.clear_pending_user_inputs(
                session,
                "Agent session stopped waiting on this prompt.",
            )
            await self.controls.update_session_status_reaction(session, REACTION_REJECTED)
            return

        if message_type == "turn_complete" and status.assistant_message:
            await self.post_assistant_message(session.thread_id, status.assistant_message)
        if message_type == "turn_complete":
            await self.controls.clear_rejected_command_reactions(session)
            await self.controls.update_active_command_reaction(session, REACTION_FINISHED, clear=True)
            await self.interactions.clear_pending_user_inputs(
                session,
                "Agent session is no longer waiting on this prompt.",
            )
            if status.assistant_message and session.control_interruptions_enabled:
                replaced = await self.controls.spawn_session_controls(
                    session,
                    reaction=None,
                    interruptions_enabled=False,
                )
                if replaced:
                    return
            await self.controls.post_session_controls(session)

    async def handle_user_message(self, user_message: UserMessage) -> None:
        session = self.sessions.get(user_message.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Agent session user message for unknown session: %s", user_message.session_id)
            return
        if user_message.session_epoch != session.session_epoch:
            logger.warning("Agent session user message for stale session epoch: %s", user_message.session_id)
            return
        message = filter_injected_tags(user_message.message)
        if not message:
            return
        channel = self.thread_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return

        await send_agent_session_message(
            channel,
            format_user_message(message)[:DISCORD_MESSAGE_LIMIT],
        )
        session.status_revision += 1
        session.display_state = "working"
        session.last_status_message = "Working on your request."
        await self.controls.spawn_session_controls(
            session,
            reaction=REACTION_IN_PROGRESS,
            interruptions_enabled=True,
        )

    async def post_assistant_message(self, thread_id: int, text: str) -> None:
        channel = self.thread_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        await send_assistant_message(channel, text)

    async def handle_title_changed(self, session: AgentSession, title: object) -> None:
        """Rename the session thread for a title the client learned after hello, such as its latest prompt."""
        if not isinstance(title, str) or not title.strip() or session.thread_id is None:
            return
        session.hello.title = title.strip()
        self.request_thread_name(session)

    def thread_name_for(self, hello: SessionHello) -> str:
        taken = {
            other.thread_name
            for other in self.sessions.live_sessions()
            if other.session_id != hello.session_id and other.thread_name is not None
        }
        return distinct_thread_name(hello, taken)

    def request_thread_name(self, session: AgentSession) -> None:
        """Schedule a rename to the session's current, distinct name; never waits on Discord."""
        if session.thread_id is None:
            return
        session.thread_name = self.thread_name_for(session.hello)
        self.threads.rename(session.thread_id, session.thread_name, session.session_epoch)

    # The thread workers' view of the bridge (ThreadHooks).

    def owned(self, thread_id: int) -> bool:
        return (
            thread_id in self._attaching_threads
            or self.sessions.get_by_thread(thread_id) is not None
            or self.stored_thread_protected(thread_id)
        )

    def rename_target(self, thread_id: int, epoch: str) -> RenameTarget | None:
        """The thread to rename, only while the session epoch that asked still owns it."""
        session_id = self.sessions.by_thread.get(thread_id)
        session = self.sessions.get(session_id) if session_id is not None else None
        if session is None or session.session_epoch != epoch:
            return None
        channel = self.thread_channel(thread_id)
        return channel if isinstance(channel, discord.Thread) else None

    @staticmethod
    async def post_close_notice(thread: discord.Thread) -> None:
        await send_agent_session_message(thread, SESSION_ENDED_NOTICE)

    def bot_user_id(self) -> int | None:
        return self.bot.user.id if self.bot.user is not None else None

    async def add_configured_members(self, thread: discord.Thread) -> None:
        await auto_join_configured_users(self.bot, thread)

    async def post_thread_notice(self, thread_id: int, text: str) -> None:
        channel = self.thread_channel(thread_id)
        if isinstance(channel, discord.Thread):
            await send_agent_session_message(
                channel,
                text[:DISCORD_MESSAGE_LIMIT],
            )

    def thread_channel(self, thread_id: int) -> object | None:
        """discord.py's cached channel, else the thread this bridge attached.

        discord.py caches a reopened thread again only when its gateway update arrives, after the REST reply; an event
        sent right after hello_ack must not be dropped in that window.
        """
        channel = self.bot.get_channel(thread_id)
        channel = channel if channel is not None else self._attached_threads.get(thread_id)
        if channel is not None and getattr(getattr(channel, "guild", None), "id", None) == self.bot.config.discord.guild_id:
            return channel
        return None

    def operator_role_name(self) -> str:
        return self.bot.config.agent_session.operator_role_name or self.bot.config.discord.employee_role_name

    def is_operator(self, user: discord.User | discord.Member) -> bool:
        role_name = self.operator_role_name()
        if not role_name:
            return False
        if not isinstance(user, discord.Member):
            return False
        if user.guild.id != self.bot.config.discord.guild_id:
            return False
        return any(role.name == role_name for role in user.roles)

    def _authorized(self, request: web.Request) -> bool:
        token = self.bot.config.agent_session.token
        if not token:
            return False
        expected = f"Bearer {token}"
        return request.headers.get("Authorization") == expected
