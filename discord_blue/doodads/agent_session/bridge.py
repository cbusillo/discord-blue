from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import weakref
from contextlib import suppress
from functools import partial
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
from aiohttp import WSMsgType, web

from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT
from discord_blue.doodads.agent_session.messages import send_agent_session_message
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
from discord_blue.doodads.agent_session.cleanup import (
    MAINTENANCE_DISCOVERY_TIMEOUT_SECONDS,
    SESSION_FINALIZATION_TIMEOUT_SECONDS,
    SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS,
    SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS,
    SessionCleanups,
)
from discord_blue.doodads.agent_session.session_threads import SessionThreads
from discord_blue.doodads.agent_session.controls import SessionControls
from discord_blue.doodads.agent_session.interactions import SessionInteractions
from discord_blue.doodads.agent_session.session_status import SessionStatusUpdates

from discord_blue.doodads.agent_session.discovery import DiscoveryIndex
from discord_blue.doodads.agent_session.store import SessionState, SessionStore, StoredSession
from discord_blue.doodads.agent_session.thread_worker import RenameTarget, ThreadWorkers
from discord_blue.doodads.agent_session.threads import SessionThread
from discord_blue.doodads.agent_session.threads import auto_join_configured_users
from discord_blue.doodads.agent_session.threads import ThreadCreationStopped
from discord_blue.doodads.agent_session.threads import distinct_thread_name
from discord_blue.doodads.agent_session.threads import session_start_message
from discord_blue.health import health_payload
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)
# A connection that drops without a clean session_end (including a failed hello_ack) keeps its thread untouched
# this long, so a reconnect resumes it without an archive, member changes or notices.
SESSION_DISCONNECT_GRACE_SECONDS = 300
SESSION_STORE_WAIT_TIMEOUT_SECONDS = 1
SESSION_ENDED_NOTICE = "Session ended"

STARTUP_RECONNECT_GRACE_SECONDS = 20
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
        self._grace_tasks: set[asyncio.Task[None]] = set()
        # Every change to a session thread (reopen, join, members, notice, archive, leave, rename) goes through its
        # worker, one request at a time and never cancelled.
        self.threads = ThreadWorkers.for_client(bot, self)
        self.discovery = DiscoveryIndex(self.bot_user_id)
        # No await occurs while resolving the entry, so one event loop turn
        # cannot create two locks for the same session ID.
        self._session_lifecycle_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self.cleanups = SessionCleanups(self, self._session_attach_lock)
        self.session_threads = SessionThreads(self)
        # One attach per session ID; a reconnecting socket joins the one already running instead of starting over.
        self._attach_tasks: dict[str, asyncio.Task[SessionThread]] = {}
        # Each attached thread as the attach resolved it, for handlers while discord.py's cache lacks it.
        self._attached_threads: dict[int, discord.Thread] = {}
        self.controls = SessionControls(self)
        self.interactions = SessionInteractions(self)
        self.status = SessionStatusUpdates(self)
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
        self.session_threads.creation_stopping.set()
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
                await self.session_threads.retry_creation_checks()
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
                if message_type == "hello":
                    if "conversation" in payload:
                        await websocket.close(message=b"use conversation endpoint", drain=False)
                        break
                    if self.bot.config.conversation.enabled and (
                        payload.get("session_id") == self.bot.config.conversation.session_id
                    ):
                        await websocket.close(message=b"conversation cannot attach as mirrored session", drain=False)
                        break
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
                    # The newest connection is the session's from now on; the attach answers only it. It is
                    # registered here so the finally below ends it however the attach turns out.
                    previous = self.sessions.get(hello.session_id)
                    session = opened = AgentSession(hello=hello, websocket=websocket)
                    self.sessions.register(opened)
                    if not await self.accept_hello(opened, previous):
                        break
                    continue
                if (
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
                if message_type == "session_end":
                    # A clean end: close the thread now instead of waiting out a grace period.
                    session.ended = True
                    break
                # Awaited inline, so the session's events are handled in the order they arrive.
                await self.handle_event(session, message_type, payload)
        finally:
            if session is not None:
                await self.end_connection(session)

        return websocket

    async def accept_hello(self, session: AgentSession, previous: AgentSession | None) -> bool:
        """Wait for the session's attach and acknowledge its hello; False when the connection should close."""
        hello = session.hello
        websocket = session.websocket
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
            return False
        except ConnectionError:
            logger.info("Agent session %s left while waiting for a thread", hello.session_id)
            if self.sessions.get(hello.session_id) is session:
                # A dropped socket still owns this attach. Let it bind before end_connection decides
                # between grace and finalization; otherwise it would archive the just-created thread.
                with suppress(discord.DiscordException, ValueError, ThreadCreationStopped):
                    await asyncio.shield(task)
            return False
        except (discord.DiscordException, ValueError):
            await websocket.close(message=b"unable to attach Discord thread", drain=False)
            return False
        if self.sessions.get(hello.session_id) is not session:
            # A newer connection of this session joined the attach while this one waited; it gets the ack.
            await websocket.close(message=b"replaced by a newer connection", drain=False)
            return False
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
            # The client stopped waiting; it will reconnect, so the connection's end starts a grace period.
            logger.info("Agent session %s left before its hello was acknowledged", hello.session_id)
            return False
        # Heartbeats start after the ack, so the watchdog's clock starts here too.
        session.acknowledged = True
        session.touch()
        if self.sessions.get(session.session_id) is session and not self._stopping:
            self.save_session(session, "live")
        # Attached, acknowledged and outside every lock: bring the thread's name up to date in the
        # background. The thread's worker coalesces and rate-limits; nothing here waits on Discord.
        self.request_thread_name(session)
        return True

    async def handle_event(self, session: AgentSession, message_type: object, payload: dict[str, object]) -> None:
        """Route one event from the session's current connection; unknown types are ignored."""
        match message_type:
            case "heartbeat":
                session.touch()
            case "user_message":
                await self.status.handle_user_message(UserMessage.from_payload(payload))
            case "status_changed" | "turn_complete" | "error":
                await self.status.handle_session_status(message_type, SessionStatus.from_payload(payload))
            case "title_changed":
                await self.status.handle_title_changed(session, payload.get("title"))
            case "notice" if session.thread_id is not None:
                if isinstance(notice := payload.get("message"), str) and notice.strip():
                    await self.post_thread_notice(session.thread_id, notice)
            case "approval_resolved" | "request_user_input_resolved":
                await self.interactions.handle_prompt_resolved(message_type, payload)
            case "approval_request":
                await self.interactions.handle_approval_request(RemoteApprovalRequest.from_payload(payload))
            case "request_user_input":
                await self.interactions.handle_request_user_input(RemoteRequestUserInput.from_payload(payload))
            case "approval_decision_ack":
                logger.info("Agent session approval decision ack: %s", payload.get("approval_id"))
                await self.interactions.handle_approval_decision_ack(payload)
            case "approval_decision_reject":
                logger.warning("Agent session approval decision reject: %s", payload)
                await self.interactions.handle_approval_decision_reject(payload)
            case "command_ack":
                logger.info("Agent session command ack: %s", payload.get("command_id"))
                await self.controls.handle_command_ack(payload)
            case "command_reject":
                logger.warning("Agent session command reject: %s", payload)
                await self.controls.handle_command_reject(payload)

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
                session_thread = await self.resume_thread_in_grace(
                    previous, hello
                ) or await self.session_threads.find_or_create_session_thread(hello)
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
                await self.session_threads.backfill_latest_assistant_message(session_thread.thread, hello)
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
            self.session_threads.binding(thread_id)
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
