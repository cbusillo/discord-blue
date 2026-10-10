from __future__ import annotations

from discord_blue.doodads.agent_session.protocol import approval_content_displayable, format_content_approval

import asyncio
import dataclasses
import functools
import json
import logging
import re
import shlex
import time
import uuid
import weakref
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import partial
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import discord
from aiohttp import WSMsgType, web

from discord_blue.doodads.agent_session.cards import STATUS_CARD_COMPONENT_ID, session_status_card
from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT
from discord_blue.doodads.agent_session.chunks import format_assistant_messages
from discord_blue.doodads.agent_session.formatting import advance_code_fence
from discord_blue.doodads.agent_session.formatting import format_user_message
from discord_blue.doodads.agent_session.formatting import is_assistant_message
from discord_blue.doodads.agent_session.messages import edit_agent_session_message
from discord_blue.doodads.agent_session.messages import agent_session_allowed_mentions
from discord_blue.doodads.agent_session.messages import send_agent_session_message
from discord_blue.doodads.agent_session.messages import send_assistant_message
from discord_blue.doodads.agent_session.protocol import (
    APPROVAL_COMMAND_DISPLAY_LIMIT,
    SERVER_FEATURES,
    command_text_displayable,
    RequestUserInputQuestion,
    RemoteApprovalDecision,
    RemoteApprovalRequest,
    RemoteCommand,
    RemoteRequestUserInput,
    SessionHello,
    SessionStatus,
    UserMessage,
)
from discord_blue.doodads.agent_session.sessions import (
    AgentSession,
    AgentSessionRegistry,
    CleanupStep,
    PendingSessionCleanup,
    PendingRemoteApproval,
    PendingRemoteCommand,
    PendingRemoteUserInput,
    RejectedCommandMessage,
)
from discord_blue.doodads.agent_session.discovery import DiscoveryIndex
from discord_blue.doodads.agent_session.store import SessionState, SessionStore, StoredSession
from discord_blue.doodads.agent_session.thread_worker import THREAD_CLOSE_STEPS
from discord_blue.doodads.agent_session.thread_worker import RenameTarget, ThreadWorkers
from discord_blue.doodads.agent_session.threads import SessionThread
from discord_blue.doodads.agent_session.threads import auto_join_configured_users
from discord_blue.doodads.agent_session.threads import ThreadCreationStopped, create_session_thread
from discord_blue.doodads.agent_session.threads import creation_token_suffix
from discord_blue.doodads.agent_session.threads import new_creation_token
from discord_blue.doodads.agent_session.threads import get_agent_session_channel
from discord_blue.doodads.agent_session.threads import session_notification_message
from discord_blue.doodads.agent_session.threads import distinct_thread_name
from discord_blue.doodads.agent_session.threads import session_thread_name
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
REPLY_BEFORE_RECONNECT = (
    "This reply was written before the agent session reconnected (for example after `/clear` or `/resume`), "
    "so it was not delivered. Send it again if it still applies."
)
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
SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS = 2
# How long teardown waits for its thread's close steps. The steps themselves are never cancelled: one still running
# when the wait ends lands later, and the residual record it shares is updated as it does.
SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS = 6
SESSION_FINALIZATION_TIMEOUT_SECONDS = 9
THREAD_LOOKUP_TIMEOUT_SECONDS = 1
THREAD_CLOSE_WAIT_SECONDS = SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS - THREAD_LOOKUP_TIMEOUT_SECONDS
MAINTENANCE_INTERVAL_SECONDS = 300
MAINTENANCE_DISCOVERY_TIMEOUT_SECONDS = 30
PENDING_CLEANUP_LIMIT = 256
PENDING_CLEANUP_MAX_ATTEMPTS = 5
# Notifications deleted recently enough that a history page read before the delete may still list them.
DELETED_NOTIFICATIONS_REMEMBERED = 1024
SHUTDOWN_WEBSOCKET_CLOSE_TIMEOUT_SECONDS = SESSION_WEBSOCKET_CLOSE_TIMEOUT_SECONDS
SHUTDOWN_RUNNER_CLEANUP_TIMEOUT_SECONDS = 5
SHUTDOWN_ATTACH_SETTLE_SECONDS = 10
SHUTDOWN_THREAD_CLEANUP_TIMEOUT_SECONDS = SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS + SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS

AGENT_SESSION_CONNECT_PATH = "/agent-session/connect"
SESSION_START_PREFIX = "Agent session connected"
SESSION_NOTIFICATION_PREFIX = "Agent session connected for "
SESSION_NOTIFICATION_PREFIXES = (
    SESSION_NOTIFICATION_PREFIX,
    "Automated agent session connected for ",
)
CONTINUE_AUTONOMOUSLY_DELIVERED = "Asked the agent session to go ahead until it needs you."
PAUSE_CURRENT_TURN_DELIVERED = "Asked the agent session to pause what it is doing now."
SESSION_NOTIFICATION_THREAD_RE = re.compile(r"<#(?P<thread_id>\d+)>")
REACTION_QUEUED = "⏳"
REACTION_DELIVERED = "📬"
REACTION_IN_PROGRESS = "🔄"
REACTION_COMPACTING = "🧹"
REACTION_FINISHED = "✅"
REACTION_REJECTED = "❌"
STATUS_REACTIONS = {
    REACTION_QUEUED,
    REACTION_DELIVERED,
    REACTION_IN_PROGRESS,
    REACTION_COMPACTING,
    REACTION_FINISHED,
    REACTION_REJECTED,
}
REACTION_CONTROL_CONTINUE = "▶️"
REACTION_CONTROL_STATUS = "\N{INFORMATION SOURCE}\N{VARIATION SELECTOR-16}"
REACTION_CONTROL_PAUSE = "⏸️"
REACTION_CONTROL_END = "⏹️"
REACTION_APPROVAL_APPROVE = "✅"
REACTION_APPROVAL_DENY = "✖️"
CONTROL_REACTIONS = {
    REACTION_CONTROL_CONTINUE,
    REACTION_CONTROL_STATUS,
    REACTION_CONTROL_PAUSE,
    REACTION_CONTROL_END,
}
TRANSIENT_REACTIONS = STATUS_REACTIONS | CONTROL_REACTIONS


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


@dataclasses.dataclass(slots=True)
class ReactionWrites:
    """The bot's reaction changes to one message: one request at a time, and the latest change supersedes the rest."""

    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    latest: int = 0


HELLO_PENDING_INTERVAL_SECONDS = 15


class BridgeStopping(ThreadCreationStopped):
    """The bridge is shutting down, so an attach does not start."""


class RequestUserInputSelect(discord.ui.Select[discord.ui.View]):
    def __init__(
        self,
        parent_view: RequestUserInputView,
        question: RequestUserInputQuestion,
    ) -> None:
        options = [
            discord.SelectOption(
                label=option.label[:100],
                value=option.label[:100],
                description=(option.description[:100] or None),
            )
            for option in question.options[:25]
        ]
        if question.is_other and len(options) < 25:
            options.append(
                discord.SelectOption(
                    label="Other...",
                    value="__other__",
                    description="Provide a custom answer",
                )
            )
        placeholder = (question.header or question.question or "Choose an answer")[:150]
        super().__init__(placeholder=placeholder, options=options)
        self.parent_view = parent_view
        self.question = question

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.values:
            return
        value = self.values[0]
        if value == "__other__":
            await interaction.response.send_modal(RequestUserInputAnswerModal(self.parent_view, self.question))
            return
        await self.parent_view.edit_answer(interaction, self.question.id, value)


class RequestUserInputAnswerModal(discord.ui.Modal):
    def __init__(
        self,
        parent_view: RequestUserInputView,
        question: RequestUserInputQuestion,
    ) -> None:
        title = (question.header or question.question or "Agent session input")[:45]
        super().__init__(title=title)
        self.parent_view = parent_view
        self.question = question
        self.answer = cast(
            discord.ui.TextInput[RequestUserInputAnswerModal],
            discord.ui.TextInput(
                label=(question.header or question.question or "Answer")[:45],
                placeholder=(question.question or None),
                style=discord.TextStyle.paragraph if not question.options else discord.TextStyle.short,
                max_length=1500,
            ),
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.parent_view.edit_answer(interaction, self.question.id, self.answer.value.strip())


class RequestUserInputAnswerButton(discord.ui.Button[discord.ui.View]):
    def __init__(
        self,
        parent_view: RequestUserInputView,
        question: RequestUserInputQuestion,
    ) -> None:
        label = (question.header or question.question or "Answer")[:80]
        super().__init__(
            label=label,
            emoji="✏️",
        )
        self.parent_view = parent_view
        self.question = question

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RequestUserInputAnswerModal(self.parent_view, self.question))


class RequestUserInputSubmitButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, parent_view: RequestUserInputView) -> None:
        super().__init__(label="Submit", style=discord.ButtonStyle.primary, emoji="✅")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.submit(cast(discord.Interaction[BlueBot], interaction))


class RequestUserInputCancelButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, parent_view: RequestUserInputView) -> None:
        super().__init__(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.cancel(cast(discord.Interaction[BlueBot], interaction))


class RequestUserInputView(discord.ui.View):
    def __init__(
        self,
        bridge: AgentSessionBridge,
        session_id: str,
        request: RemoteRequestUserInput,
    ) -> None:
        super().__init__(timeout=3600)
        self.bridge = bridge
        self.session_id = session_id
        self.request = request
        self.answers: dict[str, str] = {}
        self.message_id: int | None = None

        for question in request.questions[:4]:
            if question.options:
                self.add_item(RequestUserInputSelect(self, question))
            else:
                self.add_item(RequestUserInputAnswerButton(self, question))
        self.add_item(RequestUserInputSubmitButton(self))
        self.add_item(RequestUserInputCancelButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return (
            await self.bridge.user_input_context(
                cast(discord.Interaction[BlueBot], interaction),
                self.session_id,
                self.request.session_epoch,
                self.request.call_id,
                self.request.turn_id,
                self.message_id,
            )
            is not None
        )

    async def edit_answer(self, interaction: discord.Interaction, question_id: str, answer: str) -> None:
        context = await self.bridge.user_input_context(
            cast(discord.Interaction[BlueBot], interaction),
            self.session_id,
            self.request.session_epoch,
            self.request.call_id,
            self.request.turn_id,
            self.message_id,
        )
        if context is None:
            return
        _, pending = context
        async with pending.ui_lock:
            if not await self.interaction_check(interaction):
                return
            self.set_answer(question_id, answer)
            await interaction.response.edit_message(
                content=self.format_prompt(),
                view=self,
                allowed_mentions=agent_session_allowed_mentions(),
            )

    def set_answer(self, question_id: str, answer: str) -> None:
        self.answers[question_id] = answer

    def response_payload(self) -> dict[str, object]:
        return {"answers": {question.id: {"answers": [self.answers.get(question.id, "")]} for question in self.request.questions}}

    def format_prompt(self) -> str:
        return self.bridge.format_request_user_input(self.request, self.answers)

    async def submit(self, interaction: discord.Interaction[BlueBot]) -> None:
        missing = [
            question.header or question.id or "Question"
            for question in self.request.questions
            if not self.answers.get(question.id, "").strip()
        ]
        if missing:
            await interaction.response.send_message(
                "Please answer before submitting: " + ", ".join(missing[:4]),
                ephemeral=True,
            )
            return
        await self.bridge.handle_request_user_input_interaction(
            interaction,
            self.session_id,
            self.request.call_id,
            self.request.turn_id,
            self.response_payload(),
            session_epoch=self.request.session_epoch,
            message_id=self.message_id,
        )

    async def cancel(self, interaction: discord.Interaction[BlueBot]) -> None:
        await self.bridge.handle_request_user_input_interaction(
            interaction,
            self.session_id,
            self.request.call_id,
            self.request.turn_id,
            {"answers": {}},
            cancelled=True,
            session_epoch=self.request.session_epoch,
            message_id=self.message_id,
        )


class AgentSessionBridge:
    def __init__(self, bot: BlueBot, *, store_path: Path | None = None) -> None:
        self.bot = bot
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
        self._pending_cleanups: dict[tuple[str, str, int | None], PendingSessionCleanup] = {}
        # Notifications with a DELETE sent (a count, as sweeps and cleanups can overlap) or already done: an attach or
        # sweep never adopts one, even from a history page read before the delete landed.
        self._notification_deletes: Counter[int] = Counter()
        self._deleted_notifications: dict[int, None] = {}
        # Threads an attach is reopening and has not bound yet; no close may touch them meanwhile.
        self._attaching_threads: Counter[int] = Counter()
        # One attach per session ID; a reconnecting socket joins the one already running instead of starting over.
        self._attach_tasks: dict[str, asyncio.Task[SessionThread]] = {}
        # Each attached thread as the attach resolved it, for handlers while discord.py's cache lacks it.
        self._attached_threads: dict[int, discord.Thread] = {}
        # By message ID, while the bot is changing that message's reactions.
        self._reaction_writes: dict[int, ReactionWrites] = {}
        self._finalizing_cleanups: set[tuple[str, str, int | None]] = set()
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

    async def handle_health(self, _request: web.Request) -> web.Response:
        discord_ready = self.discord_ready()
        monitor = self.agent_session_monitor_health()
        monitor_healthy = not self.bot.config.agent_session.enabled or monitor["status"] not in {"dead", "stalled"}
        return web.json_response(
            health_payload(
                discord_status="ok" if discord_ready else "unhealthy",
                agent_session_enabled=self.bot.config.agent_session.enabled,
                active_agent_sessions=len(self.sessions.live_sessions()),
                disconnected_agent_sessions=len(self.sessions.disconnected_sessions()),
                pending_agent_session_cleanups=len(self._pending_cleanups),
                agent_session_monitor=monitor,
            ),
            status=200 if discord_ready and monitor_healthy else 503,
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

            fallback_cleanup = self.pending_cleanup_for_session(removed)
            self.save_session(removed, "closing")
            self._finalizing_cleanups.add(fallback_cleanup.key)
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
                        residual = await self.close_session_thread(removed, fallback_cleanup)
                except TimeoutError:
                    logger.warning(
                        "Agent session thread cleanup for %s timed out%s",
                        removed.session_id,
                        " during shutdown" if shutdown else "",
                    )
                    residual = fallback_cleanup
                if residual is not None:
                    self.save_cleanup(residual)
                    self.remember_pending_cleanup(residual)
                else:
                    self.save_session(removed, "closed")
                return True
            except asyncio.CancelledError:
                self.save_cleanup(fallback_cleanup)
                self.remember_pending_cleanup(fallback_cleanup)
                raise
            except Exception:
                self.save_cleanup(fallback_cleanup)
                self.remember_pending_cleanup(fallback_cleanup)
                raise
            finally:
                self._finalizing_cleanups.discard(fallback_cleanup.key)

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

    @staticmethod
    def payload_string(payload: dict[str, object], key: str, default: str = "") -> str:
        value = payload.get(key)
        if isinstance(value, str):
            return value or default
        if value is None:
            return default
        return str(value)

    async def close_active_sessions(self) -> None:
        await self.disconnect_active_sessions()

    async def cleanup_stale_sessions(self) -> None:
        await asyncio.sleep(STARTUP_RECONNECT_GRACE_SECONDS)
        while True:
            self.record_maintenance_progress()
            try:
                self.store.retry_failed_write()
                await self.recover_stored_cleanups()
                await self.retry_pending_cleanups()
                await self.retry_creation_checks()
                if time.monotonic() - self._monitor_started_at >= STARTUP_SWEEP_HOLD_SECONDS:
                    await self.cleanup_stale_session_notifications()
                    await self.cleanup_stale_session_threads()
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

    async def cleanup_stale_session_notifications(self) -> None:
        # An in-flight attachment may have published a notice before binding it.
        # Defer this sweep rather than pinning maintenance behind its network I/O.
        if self.attaching():
            return
        await self.cleanup_stale_session_notifications_locked()

    async def cleanup_stale_session_notifications_locked(self) -> None:
        # Historical name retained for callers; mutation uses per-thread locks.
        try:
            channel = await asyncio.wait_for(get_agent_session_channel(self.bot), timeout=3)
        except Exception:
            logger.warning("Unable to clean Agent session notifications: channel is unavailable", exc_info=True)
            return
        bot_user = self.bot.user
        if bot_user is None:
            return

        deferred: dict[int, list[discord.Message]] = {}
        observed: dict[int, tuple[AgentSession, int | None]] = {}
        messages = channel.history(limit=None).__aiter__()
        while True:
            self.record_maintenance_progress()
            try:
                message = await self.threads.bounded(anext(messages), MAINTENANCE_DISCOVERY_TIMEOUT_SECONDS)
            except StopAsyncIteration:
                break
            except Exception:
                logger.warning("Unable to scan Agent session channel for stale notifications", exc_info=True)
                return
            if message.author.id != bot_user.id or not message.content.startswith(SESSION_NOTIFICATION_PREFIXES):
                continue
            thread_id = self.notification_thread_id(message.content)
            if thread_id is None:
                await self.delete_discovered_notification(message, None)
                continue
            if self.threads.busy(thread_id) or self.stored_thread_protected(thread_id):
                continue
            session = self.sessions.get_by_thread(thread_id)
            if session is None:
                await self.delete_discovered_notification(message, thread_id)
            else:
                deferred.setdefault(thread_id, []).append(message)
                observed.setdefault(thread_id, (session, session.notification_message_id))

        for thread_id, notices in deferred.items():
            self.record_maintenance_progress()
            if self.threads.busy(thread_id) or self.attaching():
                continue
            current = self.sessions.get_by_thread(thread_id)
            old_session, old_notice = observed[thread_id]
            if current is old_session and current.notification_message_id == old_notice:
                ids = {notice.id for notice in notices}
                notices = [notice for notice in notices if not self.notification_going(notice.id)]
                if notices and current.notification_message_id not in ids:
                    # Only adopt if neither the connection nor its notice
                    # changed during discovery. Reconnect always wins.
                    current.notification_message_id = notices[0].id
                    if record := self.store.records.get(current.session_id):
                        self.save_session(current, record.status, grace_until=record.grace_until)
            for notice in notices:
                self.record_maintenance_progress()
                if self.threads.busy(thread_id):
                    break
                current = self.sessions.get_by_thread(thread_id)
                if current is not None and current.notification_message_id == notice.id:
                    continue
                await self.delete_discovered_notification(notice, thread_id)

    async def delete_discovered_notification(self, message: discord.Message, thread_id: int | None) -> None:
        if self.attaching() or (thread_id is not None and self.stored_thread_protected(thread_id)):
            # A newly created thread can have a notice before bind_thread runs.
            return
        try:
            await self.threads.bounded(self.delete_notification_message(message), SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS)
        except discord.NotFound:
            return
        except Exception:
            logger.warning("Unable to delete stale Agent session notification %s", message.id, exc_info=True)
            self.remember_pending_cleanup(
                PendingSessionCleanup(
                    session_id=f"orphan-notification-{message.id}",
                    session_epoch="orphan",
                    thread_id=thread_id,
                    notification_message_id=message.id,
                    pending_steps={"notification"},
                )
            )

    async def cleanup_stale_session_threads(self) -> None:
        self.record_maintenance_progress()
        try:
            channel = await asyncio.wait_for(get_agent_session_channel(self.bot), timeout=3)
        except Exception:
            logger.warning("Unable to clean Agent session threads: channel is unavailable", exc_info=True)
            return
        self.record_maintenance_progress()
        try:
            candidates = await self.threads.bounded(self.session_thread_candidates(channel), MAINTENANCE_DISCOVERY_TIMEOUT_SECONDS)
        except Exception:
            logger.warning("Unable to discover archived Agent session threads; checking active threads", exc_info=True)
            candidates = list(channel.threads)

        seen: set[int] = set()
        for thread in candidates:
            self.record_maintenance_progress()
            if thread.id in seen:
                continue
            seen.add(thread.id)
            if self.has_pending_cleanup_for_thread(thread.id):
                continue
            # Discovery must not reopen completed threads or repost a disconnect
            # notice each sweep. Explicit residual work is retried separately.
            if thread.archived and thread.locked:
                continue
            if thread.id in self.sessions.by_thread or self.stored_thread_protected(thread.id):
                continue
            try:
                matches = await self.threads.bounded(self.is_agent_session_session_thread(thread), 3)
            except Exception:
                logger.warning("Unable to inspect stale Agent session thread %s", thread.id, exc_info=True)
                continue
            if not matches:
                continue
            if (
                self.threads.busy(thread.id)
                or self.stored_thread_protected(thread.id)
                or thread.id in self.sessions.by_thread
                or self.attaching()
                or self.has_pending_cleanup_for_thread(thread.id)
            ):
                # Creation may have published a marker before binding its
                # thread. Defer rather than racing an in-flight attachment.
                continue
            cleanup = PendingSessionCleanup(
                session_id=f"orphan-thread-{thread.id}",
                session_epoch="orphan",
                thread_id=thread.id,
                notification_message_id=None,
                pending_steps={"disconnect_notice", "members", "archive", "leave"},
            )
            # Remembered first and shared with the worker, which shrinks it as each step lands.
            self.remember_pending_cleanup(cleanup)
            await self.close_thread(thread, cleanup.pending_steps, timeout=SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS)
            self.record_maintenance_progress()

    async def is_agent_session_session_thread(self, thread: discord.Thread) -> bool:
        bot_user = self.bot.user
        if bot_user is None:
            return False
        try:
            async for message in thread.history(limit=10, oldest_first=True):
                if message.author.id != bot_user.id:
                    continue
                if message.content.startswith(SESSION_START_PREFIX):
                    return True
        except discord.DiscordException:
            logger.warning("Unable to inspect Agent session thread %s", thread.id)
        return False

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
                    await self.handle_prompt_resolved(message_type, payload)
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
                    await self.handle_approval_request(approval)
                elif message_type == "request_user_input":
                    request_user_input = RemoteRequestUserInput.from_payload(payload)
                    await self.handle_request_user_input(request_user_input)
                elif message_type == "approval_decision_ack":
                    logger.info("Agent session approval decision ack: %s", payload.get("approval_id"))
                    await self.handle_approval_decision_ack(payload)
                elif message_type == "approval_decision_reject":
                    logger.warning("Agent session approval decision reject: %s", payload)
                    await self.handle_approval_decision_reject(payload)
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
                    await self.handle_command_ack(payload)
                elif message_type == "command_reject":
                    logger.warning("Agent session command reject: %s", payload)
                    await self.handle_command_reject(payload)
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
                channel = await get_agent_session_channel(self.bot)
                if notification_id is not None:
                    try:
                        notification = await channel.fetch_message(notification_id)
                        if (
                            notification.author.id != self.bot_user_id()
                            or self.notification_thread_id(notification.content) != thread.id
                            or self.notification_going(notification_id)
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

    async def recover_stored_cleanups(self) -> None:
        """Expire startup grace using the same ownership checks and workers as a disconnected socket."""
        for session_id, record in list(self.store.records.items()):
            if record.status != "closing" and not (record.status == "grace" and record.grace_until <= time.time()):
                continue
            if any(cleanup.session_id == session_id for cleanup in self._pending_cleanups.values()):
                continue
            try:
                await self.recover_stored_cleanup(session_id, record)
            except Exception:
                logger.warning("Unable to recover Agent session cleanup %s; continuing maintenance", session_id, exc_info=True)
                if self.store.records.get(session_id) is record and self.sessions.get(session_id) is None:
                    attempts = record.recovery_attempts + 1
                    self.store.put(
                        session_id,
                        dataclasses.replace(
                            record,
                            recovery_attempts=attempts,
                            status="closed" if attempts >= PENDING_CLEANUP_MAX_ATTEMPTS else record.status,
                            updated_at=time.time(),
                        ),
                    )

    async def recover_stored_cleanup(self, session_id: str, record: StoredSession) -> None:
        lock = self.session_lifecycle_lock(session_id)
        if lock.locked() or self.attaching() or self.sessions.get(session_id) is not None:
            return
        async with lock:
            if self.store.records.get(session_id) is not record or self.sessions.get(session_id) is not None:
                return
            thread = await self.threads.bounded(self.validated_stored_thread(session_id, record), THREAD_LOOKUP_TIMEOUT_SECONDS)
            if thread is None:
                return
            notification_id = record.notification_id
            if notification_id is not None:
                try:
                    channel = await get_agent_session_channel(self.bot)
                    notification = await self.threads.bounded(
                        channel.fetch_message(notification_id), SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS
                    )
                    if (
                        notification.author.id != self.bot_user_id()
                        or self.notification_thread_id(notification.content) != thread.id
                    ):
                        notification_id = None
                except (discord.DiscordException, TimeoutError, ValueError):
                    notification_id = None  # Notification cleanup remains best-effort; the thread can still close.
            # A hello can register while the Discord reads run, then wait on this lock. Its newer intent wins.
            if self.sessions.get(session_id) is not None or self.attaching():
                return
            self.store.put(session_id, dataclasses.replace(record, status="closing", updated_at=time.time()))
            cleanup = PendingSessionCleanup(
                session_id,
                "restart",
                record.thread_id,
                notification_id,
                set(record.pending_steps) if record.pending_steps is not None else {"notification", *THREAD_CLOSE_STEPS},
            )
            residual = await self.cleanup_session_artifacts(cleanup)
            if residual is not None:
                self.save_cleanup(residual)
                self.remember_pending_cleanup(residual)
            else:
                self.store.put(session_id, dataclasses.replace(record, status="closed", updated_at=time.time()))

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
            channel = await get_agent_session_channel(self.bot)
            message = await send_agent_session_message(channel, session_notification_message(hello, thread))
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to create Agent session notification for thread %s", thread.id)
            return None
        return message.id

    async def find_session_notification_for_thread(self, thread_id: int) -> int | None:
        try:
            channel = await get_agent_session_channel(self.bot)
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
                if self.notification_going(message.id):
                    continue  # Going away: the attach posts a new one instead.
                if self.notification_thread_id(message.content) == thread_id:
                    return message.id
        except discord.DiscordException:
            logger.warning("Unable to scan Agent session notifications for thread %s", thread_id)
        return None

    @staticmethod
    def notification_thread_id(content: str) -> int | None:
        match = SESSION_NOTIFICATION_THREAD_RE.search(content)
        if match is None:
            return None
        return int(match.group("thread_id"))

    async def find_existing_session_thread(self, hello: SessionHello) -> discord.Thread | None:
        """The session's thread from the shared discovery index; None only when a complete index has no match.

        An incomplete index (a listing page or a candidate's read failed) is refreshed a few times; if it stays
        incomplete the attach fails with DiscoveryIncomplete rather than creating a second thread.
        """
        try:
            channel = await get_agent_session_channel(self.bot)
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

    @staticmethod
    async def session_thread_candidates(channel: discord.TextChannel) -> list[discord.Thread]:
        candidates = list(channel.threads)
        try:
            async for thread in channel.archived_threads(
                limit=50,
            ):
                candidates.append(thread)
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to scan public archived Agent session threads")
        try:
            async for thread in channel.archived_threads(
                private=True,
                joined=True,
                limit=50,
            ):
                candidates.append(thread)
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to scan joined private archived Agent session threads")
        return candidates

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
        for message in format_assistant_messages(assistant_message):
            await send_assistant_message(thread, message)

    def is_bot_assistant_message(self, message: discord.Message) -> bool:
        bot_user = self.bot.user
        return bot_user is not None and message.author.id == bot_user.id and is_assistant_message(message.content)

    async def thread_has_assistant_message(self, thread: discord.Thread) -> bool:
        try:
            async for message in thread.history(limit=50):
                if self.is_bot_assistant_message(message):
                    return True
        except discord.DiscordException:
            logger.warning("Unable to inspect Agent session assistant history %s", thread.id)
        return False

    def dispatch_error(self, session: AgentSession, action: str) -> str | None:
        if self.sessions.get(session.session_id) is not session or session.websocket.closed:
            return "Agent session is offline; action was not delivered."
        if not session.hello.supports(action):
            label = {
                "reply": "replies",
                "status_request": "status requests",
                "pause_current_turn": "pausing turns",
                "end_session": "ending sessions",
                "new_session": "starting new sessions",
                "continue_autonomously": "autonomous continuation",
                "request_user_input_response": "answering prompts",
                "approval_decision": "approval decisions",
            }.get(action, "this action")
            return f"This client does not support {label} from Discord. Use the native TUI."
        return None

    async def dispatch_command(
        self,
        session: AgentSession,
        command: RemoteCommand,
        pending: PendingRemoteCommand,
        *,
        input_pending: PendingRemoteUserInput | None = None,
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> str | None:
        if error := self.dispatch_error(session, command.kind):
            return error
        if input_pending is not None:
            input_pending.submitted = True
        session.pending_commands[command.command_id] = pending
        delivered = False
        try:
            if before_send is not None:
                try:
                    await before_send()
                except (OSError, discord.DiscordException):
                    return "Discord could not prepare the reply controls; reply was not sent."
            if error := self.dispatch_error(session, command.kind):
                return error
            try:
                await session.websocket.send_json(command.to_message())
            except (OSError, RuntimeError):
                return "Agent session connection failed; action delivery could not be confirmed. Check the native TUI."
            delivered = True
            return None
        finally:
            if not delivered:
                session.pending_commands.pop(command.command_id, None)
                if input_pending is not None:
                    input_pending.submitted = False

    async def dispatch_approval(
        self,
        session: AgentSession,
        pending: PendingRemoteApproval,
        approval_id: str,
        decision: Literal["approved", "denied"],
        user_id: int,
    ) -> str | None:
        if error := self.dispatch_error(session, "approval_decision"):
            return error
        if pending.decision is not None:
            return "This approval has already been answered."
        pending.decision, pending.decided_by = decision, user_id
        delivered = False
        try:
            await session.websocket.send_json(
                RemoteApprovalDecision(
                    approval_id=approval_id,
                    session_id=session.session_id,
                    session_epoch=session.session_epoch,
                    decision=decision,
                ).to_message()
            )
            delivered = True
        except (OSError, RuntimeError):
            return "Agent session connection failed; approval delivery could not be confirmed. Check the native TUI."
        finally:
            if not delivered:
                pending.decision, pending.decided_by = None, None
        return None

    async def send_thread_reply(self, message: discord.Message) -> bool:
        session = self.sessions.get_by_thread(message.channel.id)
        if session is None:
            return False
        # MESSAGE_CREATE can arrive before the reopened thread returns to discord.py's cache.
        # Its PartialMessageable still identifies the thread already bound by our acknowledged hello.
        channel = self.thread_channel(message.channel.id)
        if not isinstance(channel, discord.Thread):
            return False
        thread: discord.Thread = channel
        if session.websocket.closed:
            await message.reply("Agent session is offline; reply was not delivered.", mention_author=False)
            return True
        text = message.content.strip()
        if not text or text.startswith("!"):
            return False
        if message.created_at < session.attached_at:
            # The client reconnected since (for Claude Code, after /clear or /resume); never deliver it to the new epoch.
            await message.reply(REPLY_BEFORE_RECONNECT, mention_author=False)
            return True
        if not session.acknowledged:
            await message.reply("Agent session is reconnecting; send the reply again in a moment.", mention_author=False)
            return True

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="reply",
            text=text,
            issued_by=str(message.author.id),
        )
        pending = PendingRemoteCommand(
            thread_id=message.channel.id,
            message_id=message.id,
            kind="reply",
        )
        queued = False

        async def show_queued() -> None:
            nonlocal queued
            queued = True
            await self.set_message_reaction(thread.id, message.id, REACTION_QUEUED)
            await self.show_active_session_controls(session, thread, REACTION_QUEUED)

        delivered = False
        try:
            error = await self.dispatch_command(session, command, pending, before_send=show_queued)
            delivered = error is None
        finally:
            if queued and not delivered:
                await self.clear_message_transient_reactions(thread.id, message.id)
                if self.sessions.get(session.session_id) is session and session.control_status_reaction == REACTION_QUEUED:
                    session.display_state = "failed"
                    session.last_status_message = "Reply delivery could not be confirmed. Check the native terminal."
                    await self.post_session_controls(session)
        if error is not None:
            await message.reply(error, mention_author=False)
        return True

    async def send_continue_autonomously(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code go-ahead` inside an agent session thread."
        if not self.is_operator(user):
            return "Only agent session operators can ask a session to continue."

        session = self.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live agent session."
        if session.websocket.closed:
            return "Agent session is offline; go-ahead was not delivered."

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="continue_autonomously",
            issued_by=str(user.id),
        )
        pending = PendingRemoteCommand(
            thread_id=channel.id,
            message_id=session.control_message_id,
            kind="continue_autonomously",
            reject_notice="Agent session could not go ahead",
        )
        if error := await self.dispatch_command(session, command, pending):
            return error
        await self.show_active_session_controls(session, channel, REACTION_QUEUED)
        return CONTINUE_AUTONOMOUSLY_DELIVERED

    async def send_pause_current_turn(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code pause` inside an agent session thread."
        if not self.is_operator(user):
            return "Only agent session operators can pause a turn."

        session = self.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live agent session."
        if session.websocket.closed:
            return "Agent session is offline; pause was not delivered."

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="pause_current_turn",
            issued_by=str(user.id),
        )
        pending = PendingRemoteCommand(
            thread_id=channel.id,
            message_id=session.control_message_id,
            kind="pause_current_turn",
            reject_notice="Agent session could not pause the current turn",
        )
        if error := await self.dispatch_command(session, command, pending):
            return error
        await self.show_active_session_controls(session, channel, REACTION_QUEUED)
        session.last_status_message = "Pause requested; waiting for the native session."
        await self.refresh_session_controls(session, channel)
        return PAUSE_CURRENT_TURN_DELIVERED

    async def send_new_session(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code new` inside an agent session thread."
        if not self.is_operator(user):
            return "Only agent session operators can start a new session."

        session = self.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live agent session."
        if session.websocket.closed:
            return "Agent session is offline; new session was not started."

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="new_session",
            issued_by=str(user.id),
        )
        pending = PendingRemoteCommand(
            thread_id=channel.id,
            message_id=session.control_message_id,
            kind="new_session",
            reject_notice="Agent session could not start a new session",
        )
        if error := await self.dispatch_command(session, command, pending):
            return error
        return "Asked the agent session to start a new session in this folder."

    async def send_end_session(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code end-session` inside an agent session thread."
        if not self.is_operator(user):
            return "Only agent session operators can end a session."

        session = self.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live agent session."
        if session.websocket.closed:
            return "Agent session is already offline."

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="end_session",
            issued_by=str(user.id),
        )
        pending = PendingRemoteCommand(
            thread_id=channel.id,
            message_id=session.control_message_id,
            kind="end_session",
            reject_notice="Agent session could not end the session",
        )
        if error := await self.dispatch_command(session, command, pending):
            return error
        session.display_state = "working"
        session.last_status_message = "Session end requested; waiting for the native session."
        session.control_status_reaction = REACTION_QUEUED
        await self.show_or_refresh_session_controls(session, channel)
        return "Asked the agent session to end this session."

    async def handle_go_ahead_interaction(
        self,
        interaction: discord.Interaction[BlueBot],
    ) -> None:
        response = await self.send_continue_autonomously(
            interaction.channel,
            interaction.user,
        )
        await interaction.response.send_message(response, ephemeral=True)
        if response != CONTINUE_AUTONOMOUSLY_DELIVERED:
            return
        message = interaction.message
        if message is None or not isinstance(interaction.channel, discord.Thread):
            return
        session = self.sessions.get_by_thread(interaction.channel.id)
        if session is None or session.control_message_id != message.id:
            return
        await self.replace_message_reactions(
            interaction.channel,
            message.id,
            [REACTION_QUEUED],
        )

    async def handle_status_interaction(
        self,
        interaction: discord.Interaction[BlueBot],
    ) -> None:
        await interaction.response.send_message(
            self.session_status_summary(interaction.channel, interaction.user),
            ephemeral=True,
        )

    def active_sessions_summary(self) -> str:
        sessions = self.sessions.live_sessions()
        if not sessions:
            return "No live agent sessions."

        lines = ["Live agent sessions:"]
        for session in sessions:
            title = session_thread_name(session.hello)
            thread = f" <#{session.thread_id}>" if session.thread_id is not None else ""
            lines.append(f"- `{title}` (online, {session.hello.host_label}){thread}")
        return "\n".join(lines)

    def session_status_summary(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code status` inside an agent session thread."
        if not self.is_operator(user):
            return "Only agent session operators can inspect session status."

        session = self.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live agent session."

        title = session_thread_name(session.hello)
        state = "offline" if session.websocket.closed else "online"
        status = session.last_status_message or "No status update received yet."
        return "\n".join(
            [
                f"Agent session `{title}`",
                f"state: {state}",
                f"host: {session.hello.host_label}",
                f"status: {status}",
            ]
        )

    async def handle_command_ack(self, payload: dict[str, object]) -> None:
        command_context = self.command_context(payload)
        if command_context is None:
            return
        command_id, session = command_context
        command = session.pending_commands.get(command_id)
        if command is not None:
            session.active_command_id = command_id
            await self.update_command_message_reaction(session, command, REACTION_DELIVERED)

    async def handle_command_reject(self, payload: dict[str, object]) -> None:
        command_context = self.command_context(payload)
        if command_context is None:
            return
        command_id, session = command_context
        command = session.pending_commands.pop(command_id, None)
        if session.active_command_id == command_id:
            session.active_command_id = None
        if command is not None:
            session.display_state = "failed"
            session.last_status_message = self.payload_string(payload, "reason", "Request was rejected; check the native terminal.")
            await self.update_command_message_reaction(session, command, REACTION_REJECTED)
            if command.message_id is not None:
                session.rejected_command_messages.append(
                    RejectedCommandMessage(
                        thread_id=command.thread_id,
                        message_id=command.message_id,
                    )
                )
            await self.post_session_controls(session)
            if command.reject_notice is not None:
                reason = self.payload_string(payload, "reason", "command was rejected")
                await self.post_thread_notice(command.thread_id, f"{command.reject_notice}: {reason}")

    def command_context(self, payload: dict[str, object]) -> tuple[str, AgentSession] | None:
        command_id = self.payload_string(payload, "command_id")
        session_id = self.payload_string(payload, "session_id")
        if not command_id or not session_id:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            return None
        return command_id, session

    async def update_command_message_reaction(
        self,
        session: AgentSession,
        command: PendingRemoteCommand,
        reaction: str,
    ) -> None:
        if command.input_prompt is not None:
            async with command.input_prompt.ui_lock:
                if command.input_prompt.retired:
                    return
                await self.set_message_reaction(command.thread_id, command.input_prompt.message_id, reaction)
            return
        if command.message_id is None:
            return
        if command.message_id == session.control_message_id:
            session.control_status_reaction = None if reaction == REACTION_REJECTED else reaction
            channel = self.thread_channel(command.thread_id)
            if isinstance(channel, discord.Thread):
                await self.refresh_session_controls(session, channel)
            return
        if command.kind == "reply" and reaction != REACTION_REJECTED:
            await self.clear_message_transient_reactions(command.thread_id, command.message_id)
            return
        await self.set_message_reaction(command.thread_id, command.message_id, reaction)

    async def handle_approval_request(self, approval: RemoteApprovalRequest) -> None:
        session = self.sessions.get(approval.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Agent session approval for unknown session: %s", approval.session_id)
            return
        if approval.session_epoch != session.session_epoch:
            logger.warning("Agent session approval for stale session epoch: %s", approval.session_id)
            return

        if not session.hello.supports("approval_decision"):
            await self.post_thread_notice(
                session.thread_id, "Action required in the native TUI; this client does not support answering from Discord."
            )
            return
        if approval.approval_kind != "command" or approval.content_text is not None:
            if approval.content_text is None or not approval_content_displayable(approval.approval_kind, approval.content_text):
                await self.post_thread_notice(
                    session.thread_id, "Action required in the native TUI; Discord cannot show this request in full."
                )
                return
        content = self.format_approval_request(approval)
        if approval.command_text is not None and (
            not command_text_displayable(approval.command_text)
            # The reason may be cut short; the command and directory may not.
            or len(self.format_approval_request(dataclasses.replace(approval, reason=None))) > DISCORD_MESSAGE_LIMIT
        ):
            # Never offer to approve a command Discord cannot show exactly and whole.
            await self.post_thread_notice(
                session.thread_id, "Action required in the native TUI; Discord cannot show this command in full."
            )
            return

        channel = self.thread_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return

        message = await send_agent_session_message(channel, content[:DISCORD_MESSAGE_LIMIT])
        # Answerable from the moment it is posted: Discord shows the reactions one at a time, and a tap on the first
        # one while the bot still adds the next must count.
        session.pending_approvals[approval.approval_id] = PendingRemoteApproval(
            thread_id=session.thread_id,
            message_id=message.id,
        )
        await self.add_message_reactions(
            message,
            [REACTION_APPROVAL_APPROVE, REACTION_APPROVAL_DENY],
        )
        session.display_state = "waiting"
        session.last_status_message = "Approval required. Review the request in this thread."
        if session.control_message_id is not None:
            await self.show_or_refresh_session_controls(session, channel)

    async def handle_request_user_input(self, request: RemoteRequestUserInput) -> None:
        session = self.sessions.get(request.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Agent session request_user_input for unknown session: %s", request.session_id)
            return
        if request.session_epoch != session.session_epoch:
            logger.warning(
                "Agent session request_user_input for stale session epoch: %s",
                request.session_id,
            )
            return

        await self.clear_pending_user_inputs(
            session,
            "Agent session is waiting on a newer prompt.",
        )

        if not session.hello.supports("request_user_input_response"):
            await self.post_thread_notice(
                session.thread_id, "Action required in the native TUI; this client does not support answering from Discord."
            )
            return

        channel = self.thread_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return

        view = self.request_user_input_view(session.session_id, request)
        message = await send_agent_session_message(
            channel,
            self.format_request_user_input(request, {}),
            view=view,
        )
        view.message_id = message.id
        session.pending_user_inputs[request.call_id] = PendingRemoteUserInput(
            thread_id=session.thread_id,
            message_id=message.id,
            turn_id=request.turn_id,
            call_id=request.call_id,
        )
        session.display_state = "waiting"
        session.last_status_message = "Answer required. Use the question controls in this thread."
        if session.control_message_id is not None:
            await self.show_or_refresh_session_controls(session, channel)

    async def user_input_context(
        self,
        interaction: discord.Interaction[BlueBot],
        session_id: str,
        session_epoch: str,
        call_id: str,
        turn_id: str,
        message_id: int | None,
    ) -> tuple[AgentSession, PendingRemoteUserInput] | None:
        if not self.is_operator(interaction.user):
            await interaction.response.send_message(
                "Only Agent session operators can answer prompts.",
                ephemeral=True,
            )
            return None
        session = self.sessions.get(session_id)
        if session is None or session.websocket.closed:
            await interaction.response.send_message(
                "Agent session is offline; answer was not delivered.",
                ephemeral=True,
            )
            return None
        if error := self.dispatch_error(session, "request_user_input_response"):
            await interaction.response.send_message(error, ephemeral=True)
            return None
        pending = session.pending_user_inputs.get(call_id)
        if (
            session.session_epoch != session_epoch
            or pending is None
            or pending.turn_id != turn_id
            or pending.message_id != message_id
            or getattr(interaction.channel, "id", None) != pending.thread_id
            or (interaction.message is not None and interaction.message.id != message_id)
            or pending.submitted
            or pending.retired
        ):
            await interaction.response.send_message(
                "This prompt is no longer active.",
                ephemeral=True,
            )
            return None
        return session, pending

    async def handle_request_user_input_interaction(
        self,
        interaction: discord.Interaction[BlueBot],
        session_id: str,
        call_id: str,
        turn_id: str,
        response: dict[str, object],
        *,
        session_epoch: str,
        message_id: int | None,
        cancelled: bool = False,
    ) -> None:
        context = await self.user_input_context(
            interaction,
            session_id,
            session_epoch,
            call_id,
            turn_id,
            message_id,
        )
        if context is None:
            return
        session, pending = context
        command_id = str(uuid.uuid4())
        pending_command = PendingRemoteCommand(
            thread_id=pending.thread_id,
            message_id=pending.message_id,
            kind="request_user_input_response",
            input_prompt=pending,
        )
        command = RemoteCommand(
            command_id=command_id,
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="request_user_input_response",
            call_id=call_id,
            turn_id=turn_id,
            response=response,
            issued_by=str(interaction.user.id),
        )
        if error := await self.dispatch_command(session, command, pending_command, input_pending=pending):
            await interaction.response.send_message(error, ephemeral=True)
            return
        async with pending.ui_lock:
            if (
                pending.retired
                or self.sessions.get(session_id) is not session
                or session.pending_user_inputs.get(call_id) is not pending
            ):
                await interaction.response.send_message(
                    "Answer sent; this prompt is no longer active. Check the native TUI for its outcome.", ephemeral=True
                )
                return
            await interaction.response.edit_message(
                content=self.format_request_user_input_pending(interaction.user, cancelled=cancelled),
                view=None,
                allowed_mentions=agent_session_allowed_mentions(),
            )

    async def handle_approval_interaction(
        self,
        interaction: discord.Interaction[BlueBot],
        session_id: str,
        approval_id: str,
        decision: Literal["approved", "denied"],
        *,
        session_epoch: str,
    ) -> None:
        if not self.is_operator(interaction.user):
            await interaction.response.send_message(
                "Only Agent session operators can respond to approvals.",
                ephemeral=True,
            )
            return

        session = self.sessions.get(session_id)
        if session is None or session.websocket.closed:
            await interaction.response.send_message(
                "Agent session is offline; approval was not delivered.",
                ephemeral=True,
            )
            return

        pending = session.pending_approvals.get(approval_id)
        if (
            pending is None
            or session.session_epoch != session_epoch
            or interaction.message is None
            or interaction.message.id != pending.message_id
            or getattr(interaction.channel, "id", None) != pending.thread_id
        ):
            await interaction.response.send_message(
                "This approval is no longer active.",
                ephemeral=True,
            )
            return

        if pending.decision is not None:
            await interaction.response.send_message("This approval has already been answered.", ephemeral=True)
            return

        if error := await self.dispatch_approval(session, pending, approval_id, decision, interaction.user.id):
            await interaction.response.send_message(error, ephemeral=True)
            return
        async with pending.ui_lock:
            if pending.retired or self.sessions.get(session_id) is not session:
                await interaction.response.send_message(
                    "Decision sent; this approval is no longer active. Check the native TUI for its outcome.", ephemeral=True
                )
                return
            await interaction.response.edit_message(
                content=self.format_approval_pending(decision, interaction.user),
                view=None,
                allowed_mentions=agent_session_allowed_mentions(),
            )

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
            return await self.handle_session_control_reaction(session, thread, message_id, emoji, user)

        for approval_id, pending in session.pending_approvals.items():
            if pending.message_id == message_id:
                return await self.handle_approval_reaction(
                    session,
                    thread,
                    approval_id,
                    emoji,
                    user,
                )
        return False

    async def handle_session_control_reaction(
        self,
        session: AgentSession,
        thread: discord.Thread,
        message_id: int,
        emoji: str,
        user: discord.User | discord.Member,
    ) -> bool:
        if session.pending_control_confirmation is not None:
            return await self.handle_pending_control_confirmation(
                session,
                thread,
                message_id,
                emoji,
                user,
            )

        if emoji == REACTION_CONTROL_CONTINUE:
            response = await self.send_continue_autonomously(thread, user)
            if response == CONTINUE_AUTONOMOUSLY_DELIVERED:
                await self.replace_message_reactions(
                    thread,
                    message_id,
                    [REACTION_QUEUED],
                    remove_user_reaction=(emoji, user),
                )
            else:
                await self.remove_message_reaction(thread, message_id, emoji, user)
                await self.post_thread_notice(thread.id, response)
            return True
        if emoji == REACTION_CONTROL_STATUS:
            await self.remove_message_reaction(thread, message_id, emoji, user)
            await self.post_thread_notice(
                thread.id,
                self.session_status_summary(thread, user),
            )
            return True
        if emoji == REACTION_CONTROL_PAUSE:
            response = await self.send_pause_current_turn(thread, user)
            if response == PAUSE_CURRENT_TURN_DELIVERED:
                await self.replace_message_reactions(
                    thread,
                    message_id,
                    [REACTION_QUEUED],
                    remove_user_reaction=(emoji, user),
                )
            else:
                await self.remove_message_reaction(thread, message_id, emoji, user)
                await self.post_thread_notice(thread.id, response)
            return True
        if emoji == REACTION_CONTROL_END:
            if error := self.dispatch_error(session, "end_session"):
                await self.remove_message_reaction(thread, message_id, emoji, user)
                await self.post_thread_notice(thread.id, error)
                return True
            session.pending_control_confirmation = "end_session"
            await self.refresh_session_controls(
                session,
                thread,
                remove_user_reaction=(emoji, user),
            )
            return True
        return False

    async def handle_pending_control_confirmation(
        self,
        session: AgentSession,
        thread: discord.Thread,
        message_id: int,
        emoji: str,
        user: discord.User | discord.Member,
    ) -> bool:
        if emoji == REACTION_APPROVAL_DENY:
            session.pending_control_confirmation = None
            await self.refresh_session_controls(
                session,
                thread,
                remove_user_reaction=(emoji, user),
            )
            return True
        if emoji != REACTION_APPROVAL_APPROVE:
            return False

        pending_confirmation = session.pending_control_confirmation
        session.pending_control_confirmation = None
        if pending_confirmation != "end_session":
            await self.refresh_session_controls(
                session,
                thread,
                remove_user_reaction=(emoji, user),
            )
            return True

        response = await self.send_end_session(thread, user)
        if response == "Asked the agent session to end this session.":
            session.control_status_reaction = None
            await self.replace_message_reactions(
                thread,
                message_id,
                [REACTION_QUEUED],
                remove_user_reaction=(emoji, user),
            )
        else:
            await self.refresh_session_controls(
                session,
                thread,
                remove_user_reaction=(emoji, user),
            )
            await self.post_thread_notice(thread.id, response)
        return True

    async def handle_approval_reaction(
        self,
        session: AgentSession,
        thread: discord.Thread,
        approval_id: str,
        emoji: str,
        user: discord.User | discord.Member,
    ) -> bool:
        if emoji not in {REACTION_APPROVAL_APPROVE, REACTION_APPROVAL_DENY}:
            return False

        pending = session.pending_approvals.get(approval_id)
        if pending is None:
            await self.post_thread_notice(thread.id, "This approval is no longer active.")
            return True
        if pending.decision is not None:
            await self.remove_message_reaction(thread, pending.message_id, emoji, user)
            return True
        if session.websocket.closed:
            await self.remove_message_reaction(thread, pending.message_id, emoji, user)
            await self.post_thread_notice(thread.id, "Agent session is offline; approval was not delivered.")
            return True

        decision: Literal["approved", "denied"]
        if emoji == REACTION_APPROVAL_APPROVE:
            decision = "approved"
        else:
            decision = "denied"

        if error := await self.dispatch_approval(session, pending, approval_id, decision, user.id):
            await self.post_thread_notice(thread.id, error)
            return True
        await self.edit_approval_message(
            pending,
            self.format_approval_pending(decision, user),
            only_active=True,
        )
        return True

    async def handle_approval_decision_ack(self, payload: dict[str, object]) -> None:
        session_id = self.payload_string(payload, "session_id")
        approval_id = self.payload_string(payload, "approval_id")
        session = self.sessions.get(session_id)
        if session is None or not approval_id:
            return
        pending = session.pending_approvals.pop(approval_id, None)
        if pending is None:
            return
        pending.retired = True
        await self.edit_approval_message(
            pending,
            self.format_approval_finished(pending.decision, pending.decided_by),
        )

    async def handle_approval_decision_reject(self, payload: dict[str, object]) -> None:
        session_id = self.payload_string(payload, "session_id")
        approval_id = self.payload_string(payload, "approval_id")
        reason = self.payload_string(payload, "reason", "approval was rejected")
        session = self.sessions.get(session_id)
        if session is None or not approval_id:
            return
        pending = session.pending_approvals.pop(approval_id, None)
        if pending is None:
            return
        pending.retired = True
        await self.edit_approval_message(pending, f"**Approval expired**\n{reason}")

    async def edit_approval_message(self, pending: PendingRemoteApproval, content: str, *, only_active: bool = False) -> None:
        async with pending.ui_lock:
            if only_active and pending.retired:
                return
            channel = self.thread_channel(pending.thread_id)
            if not isinstance(channel, discord.Thread):
                return
            try:
                message = await channel.fetch_message(pending.message_id)
                await edit_agent_session_message(
                    message,
                    content=content[:DISCORD_MESSAGE_LIMIT],
                )
                await self.clear_message_reactions(message)
            except discord.DiscordException:
                logger.warning("Unable to edit Agent session approval message %s", pending.message_id)

    async def handle_session_status(self, message_type: str, status: SessionStatus) -> None:
        session = self.sessions.get(status.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Agent session status for unknown session: %s", status.session_id)
            return
        if status.session_epoch != session.session_epoch:
            logger.warning("Agent session status for stale session epoch: %s", status.session_id)
            return
        session.last_status_message = status.message
        if message_type == "turn_complete":
            session.display_state = "done"
        elif message_type == "error":
            session.display_state = "failed"
        elif "waiting" in (status.message or "").lower() or (status.message or "").lower() == "turn aborted":
            session.display_state = "waiting"
        else:
            session.display_state = "working"

        if message_type == "status_changed":
            status_message = (status.message or "").lower()
            if status_message == "turn aborted":
                await self.post_session_controls(session)
                return
            elif "compact" in status_message:
                reaction = REACTION_COMPACTING
            else:
                reaction = REACTION_IN_PROGRESS
            await self.clear_pending_user_inputs(
                session,
                "Agent session is no longer waiting on this prompt.",
            )
            await self.update_session_status_reaction(session, reaction)
            return

        if message_type == "error":
            await self.clear_pending_user_inputs(
                session,
                "Agent session stopped waiting on this prompt.",
            )
            await self.update_session_status_reaction(session, REACTION_REJECTED)
            return

        if message_type == "turn_complete" and status.assistant_message:
            await self.post_assistant_message(session.thread_id, status.assistant_message)
        if message_type == "turn_complete":
            await self.clear_rejected_command_reactions(session)
            await self.update_active_command_reaction(session, REACTION_FINISHED, clear=True)
            await self.clear_pending_user_inputs(
                session,
                "Agent session is no longer waiting on this prompt.",
            )
            if status.assistant_message and session.control_interruptions_enabled:
                replaced = await self.spawn_session_controls(
                    session,
                    reaction=None,
                    interruptions_enabled=False,
                )
                if replaced:
                    return
            await self.post_session_controls(session)

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
            self.format_user_message_notice(message)[:DISCORD_MESSAGE_LIMIT],
        )
        session.display_state = "working"
        session.last_status_message = "Working on your request."
        await self.spawn_session_controls(
            session,
            reaction=REACTION_IN_PROGRESS,
            interruptions_enabled=True,
        )

    async def update_active_command_reaction(
        self,
        session: AgentSession,
        reaction: str,
        *,
        clear: bool = False,
    ) -> None:
        command_id = session.active_command_id
        if command_id is None:
            return
        command = session.pending_commands.get(command_id)
        if command is None:
            return
        await self.update_command_message_reaction(session, command, reaction)
        if command.message_id != session.control_message_id:
            await self.update_control_anchor_status(session, command.thread_id, reaction)
        if clear:
            session.pending_commands.pop(command_id, None)
            session.active_command_id = None
            if command.message_id == session.control_message_id:
                session.control_status_reaction = None

    async def clear_rejected_command_reactions(self, session: AgentSession) -> None:
        rejected_messages = session.rejected_command_messages
        session.rejected_command_messages = []
        for rejected_message in rejected_messages:
            await self.clear_message_transient_reactions(rejected_message.thread_id, rejected_message.message_id)

    async def update_session_status_reaction(
        self,
        session: AgentSession,
        reaction: str,
    ) -> None:
        if session.active_command_id is not None:
            await self.update_active_command_reaction(session, reaction)
            return
        if session.thread_id is None:
            return
        await self.update_control_anchor_status(session, session.thread_id, reaction)

    async def update_control_anchor_status(
        self,
        session: AgentSession,
        thread_id: int,
        reaction: str,
    ) -> None:
        channel = self.thread_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        session.control_status_reaction = reaction
        await self.show_or_refresh_session_controls(session, channel)

    async def show_active_session_controls(
        self,
        session: AgentSession,
        thread: discord.Thread,
        reaction: str,
    ) -> None:
        session.display_state = "working"
        session.last_status_message = "Your request is queued." if reaction == REACTION_QUEUED else "Working on your request."
        session.control_status_reaction = reaction
        session.control_interruptions_enabled = True
        await self.show_or_refresh_session_controls(session, thread)

    async def previous_status_card(self, thread: discord.Thread) -> int | None:
        if self.bot.user is None:
            return None
        try:
            async for message in thread.history(limit=50):
                if message.author.id == self.bot.user.id and any(
                    isinstance(component, discord.Container) and component.id == STATUS_CARD_COMPONENT_ID
                    for component in getattr(message, "components", [])
                ):
                    return message.id
        except discord.DiscordException:
            logger.warning("Unable to recover Agent session status card in %s", thread.id)
        return None

    async def show_or_refresh_session_controls(
        self,
        session: AgentSession,
        thread: discord.Thread,
    ) -> None:
        if session.control_message_id is not None:
            await self.refresh_session_controls(session, thread)
            if session.control_message_id is not None:
                return
        await self.spawn_session_controls(
            session,
            reaction=session.control_status_reaction,
            interruptions_enabled=session.control_interruptions_enabled,
        )

    async def set_message_reaction(self, thread_id: int, message_id: int, reaction: str) -> None:
        channel = self.thread_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.DiscordException:
            logger.warning("Unable to fetch Agent session reply message %s", message_id)
            return

        bot_user = self.bot.user
        try:
            await message.add_reaction(reaction)
            if bot_user is not None:
                for existing in TRANSIENT_REACTIONS - {reaction}:
                    with suppress(discord.DiscordException):
                        await message.remove_reaction(existing, bot_user)
        except discord.DiscordException:
            logger.warning("Unable to update Agent session reply reaction %s", message_id)

    async def clear_message_transient_reactions(self, thread_id: int, message_id: int) -> None:
        channel = self.thread_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        bot_user = self.bot.user
        if bot_user is None:
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.DiscordException:
            logger.warning("Unable to fetch Agent session reply message %s", message_id)
            return

        for existing in TRANSIENT_REACTIONS:
            with suppress(discord.DiscordException):
                await message.remove_reaction(existing, bot_user)

    async def post_assistant_message(self, thread_id: int, text: str) -> None:
        channel = self.thread_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        for message in format_assistant_messages(text):
            await send_assistant_message(channel, message)

    async def post_session_controls(self, session: AgentSession) -> None:
        if session.thread_id is None:
            return
        channel = self.thread_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return
        session.pending_control_confirmation = None
        session.control_status_reaction = None
        session.control_interruptions_enabled = False
        if session.control_message_id is not None:
            replaced = await self.refresh_control_card(
                session,
                channel,
                session.control_message_id,
                self.session_control_reactions(session),
            )
            if replaced:
                return
            session.control_message_id = None

        await self.spawn_session_controls(session, reaction=None, interruptions_enabled=False)

    async def retire_user_input(self, session: AgentSession, pending: PendingRemoteUserInput, content: str) -> None:
        pending.retired = True
        for command_id, command in list(session.pending_commands.items()):
            if command.input_prompt is pending:
                session.pending_commands.pop(command_id, None)
                if session.active_command_id == command_id:
                    session.active_command_id = None
        async with pending.ui_lock:
            channel = self.thread_channel(pending.thread_id)
            if not isinstance(channel, discord.Thread):
                return
            try:
                message = await channel.fetch_message(pending.message_id)
                await edit_agent_session_message(message, content=content[:DISCORD_MESSAGE_LIMIT])
                await self.clear_message_reactions(message)
            except discord.DiscordException:
                logger.warning("Unable to retire Agent session input message %s", pending.message_id)

    async def clear_pending_user_inputs(self, session: AgentSession, content: str) -> None:
        pending_items = list(session.pending_user_inputs.values())
        session.pending_user_inputs.clear()
        for pending in pending_items:
            await self.retire_user_input(session, pending, content)

    async def handle_prompt_resolved(self, message_type: str, payload: dict[str, object]) -> None:
        session_id, epoch = payload.get("session_id"), payload.get("session_epoch")
        if not isinstance(session_id, str) or not isinstance(epoch, str):
            return
        session = self.sessions.get(session_id)
        if session is None or session.session_epoch != epoch:
            return
        # The websocket event loop awaits each handler. Requests finish registration
        # before their following resolution event; do not dispatch frames as tasks.
        if message_type == "approval_resolved":
            approval_id = payload.get("approval_id")
            if not isinstance(approval_id, str) or not approval_id:
                return
            approval = session.pending_approvals.pop(approval_id, None)
            if approval is None:
                return
            approval.retired = True
            await self.edit_approval_message(approval, "**Resolved**")
        elif message_type == "request_user_input_resolved":
            call_id, turn_id = payload.get("call_id"), payload.get("turn_id")
            if not isinstance(call_id, str) or not call_id or not isinstance(turn_id, str):
                return
            pending = session.pending_user_inputs.get(call_id)
            if pending is None or pending.turn_id != turn_id:
                return
            session.pending_user_inputs.pop(call_id)
            await self.retire_user_input(session, pending, "**Resolved**")

    async def clear_session_controls(self, session: AgentSession) -> None:
        if session.thread_id is None or session.control_message_id is None:
            return
        channel = self.thread_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return
        try:
            message = await channel.fetch_message(session.control_message_id)
            await message.delete()
        except discord.NotFound:
            session.control_message_id = None
        except discord.DiscordException:
            logger.warning(
                "Unable to clear Agent session control message %s",
                session.control_message_id,
            )
        else:
            session.control_message_id = None
        session.pending_control_confirmation = None
        session.control_status_reaction = None
        session.control_interruptions_enabled = False

    async def delete_session_message(self, thread_id: int, message_id: int) -> None:
        channel = self.thread_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        try:
            message = await channel.fetch_message(message_id)
            await self.clear_message_reactions(message)
            await message.delete()
        except discord.NotFound:
            return
        except discord.DiscordException:
            logger.warning("Unable to clear Agent session control message %s", message_id)

    async def spawn_session_controls(
        self,
        session: AgentSession,
        *,
        reaction: str | None,
        interruptions_enabled: bool,
    ) -> bool:
        if session.thread_id is None:
            return False
        channel = self.thread_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return False

        old_control_message_id = session.control_message_id
        if old_control_message_id is None:
            old_control_message_id = await self.previous_status_card(channel)
        old_pending_control_confirmation = session.pending_control_confirmation
        old_control_status_reaction = session.control_status_reaction
        old_control_interruptions_enabled = session.control_interruptions_enabled

        session.pending_control_confirmation = None
        session.control_status_reaction = reaction
        session.control_interruptions_enabled = interruptions_enabled

        try:
            message = await send_agent_session_message(
                channel,
                view=session_status_card(session, self.session_control_reactions(session)),
            )
        except discord.DiscordException:
            session.pending_control_confirmation = old_pending_control_confirmation
            session.control_status_reaction = old_control_status_reaction
            session.control_interruptions_enabled = old_control_interruptions_enabled
            return False

        session.control_message_id = message.id  # Before its reactions, so a tap on the first one counts.
        replaced = old_control_message_id if old_control_message_id != message.id else None
        if replaced is not None:
            self.rebind_session_control_commands(session, replaced, message.id)
        await self.add_message_reactions(message, self.session_control_reactions(session))
        if replaced is not None:
            await self.delete_session_message(session.thread_id, replaced)
        return True

    @staticmethod
    def rebind_session_control_commands(
        session: AgentSession,
        old_message_id: int,
        new_message_id: int,
    ) -> None:
        for command in session.pending_commands.values():
            if command.message_id == old_message_id:
                command.message_id = new_message_id

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

    @staticmethod
    def pending_cleanup_for_session(session: AgentSession) -> PendingSessionCleanup:
        pending_steps: set[CleanupStep] = set()
        if session.notification_message_id is not None or session.thread_id is not None:
            pending_steps.add("notification")
        if session.thread_id is not None:
            pending_steps.update(THREAD_CLOSE_STEPS)
        return PendingSessionCleanup(
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            thread_id=session.thread_id,
            notification_message_id=session.notification_message_id,
            pending_steps=pending_steps,
        )

    def save_cleanup(self, cleanup: PendingSessionCleanup, *, exhausted: bool = False) -> None:
        record = self.store.records.get(cleanup.session_id)
        if record is not None and record.status == "closing" and record.thread_id == cleanup.thread_id:
            self.store.put(
                cleanup.session_id,
                dataclasses.replace(
                    record,
                    status="closed" if exhausted or not cleanup.pending_steps else "closing",
                    pending_steps=tuple(sorted(cleanup.pending_steps)),
                    updated_at=time.time(),
                ),
            )

    def remember_pending_cleanup(self, cleanup: PendingSessionCleanup) -> None:
        if not cleanup.pending_steps:
            return
        existing = self._pending_cleanups.get(cleanup.key)
        if existing is not None:
            if existing is cleanup:
                return
            # Reconnect preserves session/epoch/thread identity, but a later
            # teardown has new work and possibly a new notification. Callers
            # share their live residual record even on timeout/cancellation;
            # a distinct record is the newer authoritative observation.
            self._pending_cleanups[cleanup.key] = cleanup
            return
        if len(self._pending_cleanups) >= PENDING_CLEANUP_LIMIT:
            self.save_cleanup(cleanup, exhausted=True)
            logger.warning(
                "Dropping Agent session cleanup retry for %s/%s because the %s-record limit was reached; "
                "periodic orphan reconciliation remains enabled",
                cleanup.session_id,
                cleanup.session_epoch,
                PENDING_CLEANUP_LIMIT,
            )
            return
        self._pending_cleanups[cleanup.key] = cleanup

    def has_pending_cleanup_for_thread(self, thread_id: int) -> bool:
        if any(key[2] == thread_id for key in self._finalizing_cleanups):
            return True
        thread_steps = {"disconnect_notice", "members", "archive", "leave"}
        return any(
            cleanup.thread_id == thread_id and bool(cleanup.pending_steps & thread_steps)
            for cleanup in self._pending_cleanups.values()
        )

    async def retry_pending_cleanups(self) -> None:
        for key, cleanup in list(self._pending_cleanups.items()):
            self.record_maintenance_progress()
            if self._pending_cleanups.get(key) is not cleanup or self._session_attach_lock.locked():
                continue
            lifecycle_lock = self.session_lifecycle_lock(cleanup.session_id)
            if lifecycle_lock.locked():
                continue
            async with lifecycle_lock:
                if self._session_attach_lock.locked():
                    continue
                current = self.sessions.get(cleanup.session_id)
                if current is not None and current.thread_id == cleanup.thread_id:
                    self._pending_cleanups.pop(key, None)
                    continue
                if cleanup.thread_id is not None and self.threads.busy(cleanup.thread_id):
                    continue  # Its worker is still sending an earlier request for this thread.
                cleanup.attempts += 1
                try:
                    async with asyncio.timeout(SESSION_FINALIZATION_TIMEOUT_SECONDS):
                        residual = await self.cleanup_session_artifacts(cleanup)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    logger.warning(
                        "Agent session cleanup retry timed out for %s/%s",
                        cleanup.session_id,
                        cleanup.session_epoch,
                    )
                    residual = cleanup
                except Exception:
                    logger.exception(
                        "Agent session cleanup retry failed for %s/%s",
                        cleanup.session_id,
                        cleanup.session_epoch,
                    )
                    residual = cleanup
                self.save_cleanup(cleanup, exhausted=cleanup.attempts >= PENDING_CLEANUP_MAX_ATTEMPTS)
                if residual is None and self._pending_cleanups.get(key) is cleanup:
                    self._pending_cleanups.pop(key, None)
                    record = self.store.records.get(cleanup.session_id)
                    if record is not None and record.status == "closing" and record.thread_id == cleanup.thread_id:
                        self.store.put(cleanup.session_id, dataclasses.replace(record, status="closed", updated_at=time.time()))
                elif cleanup.attempts >= PENDING_CLEANUP_MAX_ATTEMPTS:
                    if self._pending_cleanups.get(key) is cleanup:
                        self._pending_cleanups.pop(key, None)
                    logger.warning(
                        "Dropping exhausted Agent session cleanup retry for %s/%s steps=%s; "
                        "periodic orphan reconciliation remains enabled",
                        cleanup.session_id,
                        cleanup.session_epoch,
                        sorted(cleanup.pending_steps),
                    )

    async def close_session_thread(
        self,
        session: AgentSession,
        cleanup: PendingSessionCleanup | None = None,
    ) -> PendingSessionCleanup | None:
        return await self.cleanup_session_artifacts(cleanup or self.pending_cleanup_for_session(session))

    async def cleanup_session_artifacts(self, cleanup: PendingSessionCleanup) -> PendingSessionCleanup | None:
        if not cleanup.pending_steps:
            return None
        owner = self.sessions.get_by_thread(cleanup.thread_id) if cleanup.thread_id is not None else None
        if owner is not None:
            duplicate_notice = (
                "notification" in cleanup.pending_steps
                and cleanup.notification_message_id is not None
                and cleanup.notification_message_id != owner.notification_message_id
            )
            if not duplicate_notice:
                return None
            cleanup.pending_steps.intersection_update({"notification"})
        if "notification" in cleanup.pending_steps:
            try:
                if cleanup.notification_message_id is not None:
                    deleted = await self.threads.bounded(
                        self.delete_session_notification(cleanup.notification_message_id),
                        SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS,
                    )
                elif cleanup.thread_id is not None:
                    deleted = await self.threads.bounded(
                        self.delete_session_notification_for_thread(cleanup.thread_id),
                        SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS,
                    )
                else:
                    deleted = True
            except TimeoutError:
                deleted = False
                logger.warning("Agent session notification cleanup timed out for %s", cleanup.session_id)
            except Exception:
                deleted = False
                logger.warning("Agent session notification cleanup failed for %s", cleanup.session_id, exc_info=True)
            if deleted:
                cleanup.pending_steps.discard("notification")

        thread_steps = cleanup.pending_steps & {"disconnect_notice", "members", "archive", "leave"}
        if cleanup.thread_id is not None and thread_steps:
            try:
                thread, resolved = await self.threads.bounded(
                    self.get_thread_for_cleanup(cleanup.thread_id), THREAD_LOOKUP_TIMEOUT_SECONDS
                )
            except TimeoutError:
                thread, resolved = None, False
                logger.warning("Agent session thread lookup timed out for %s", cleanup.thread_id)
            if resolved and thread is None:
                cleanup.pending_steps.difference_update(thread_steps)
            elif thread is not None:
                await self.close_thread(thread, cleanup.pending_steps, timeout=THREAD_CLOSE_WAIT_SECONDS)

        return cleanup if cleanup.pending_steps else None

    async def close_thread(
        self,
        thread: discord.Thread,
        pending_steps: set[CleanupStep] | None = None,
        *,
        timeout: float | None = None,
    ) -> set[CleanupStep]:
        """Ask the thread's worker for one pass over the close steps and wait up to `timeout` for it.

        The worker removes each step from the supplied set as it lands, including after the wait ends, so a caller
        keeping that set as its residual record never repeats a step. The return value is that set.
        """
        steps: set[CleanupStep] = (
            pending_steps if pending_steps is not None else {"disconnect_notice", "members", "archive", "leave"}
        )
        return await self.threads.close(thread, steps, timeout=timeout)

    async def delete_session_notification(self, message_id: int) -> bool:
        try:
            channel = await get_agent_session_channel(self.bot)
            message = await channel.fetch_message(message_id)
            if any(session.notification_message_id == message_id for session in self.sessions.by_session.values()):
                return True  # A reconnect adopted it while this cleanup (perhaps no longer awaited) fetched it.
            await self.delete_notification_message(message)
        except discord.NotFound:
            return True
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to delete Agent session notification message %s", message_id)
            return False
        return True

    async def delete_notification_message(self, message: discord.Message) -> None:
        """Delete a notification; from the request on (a rate limit can hold it a while), no attach adopts it."""
        self._notification_deletes[message.id] += 1
        try:
            await message.delete()
        except discord.NotFound:
            self.remember_deleted_notification(message.id)
            raise
        else:
            self.remember_deleted_notification(message.id)
        finally:
            self._notification_deletes[message.id] -= 1
            if self._notification_deletes[message.id] <= 0:
                del self._notification_deletes[message.id]

    def remember_deleted_notification(self, message_id: int) -> None:
        self._deleted_notifications[message_id] = None
        while len(self._deleted_notifications) > DELETED_NOTIFICATIONS_REMEMBERED:
            del self._deleted_notifications[next(iter(self._deleted_notifications))]

    def notification_going(self, message_id: int) -> bool:
        return message_id in self._notification_deletes or message_id in self._deleted_notifications

    async def delete_session_notification_for_thread(self, thread_id: int) -> bool:
        try:
            channel = await get_agent_session_channel(self.bot)
        except ValueError:
            logger.warning("Unable to delete Agent session notification for thread %s: channel is unavailable", thread_id)
            return False

        bot_user = self.bot.user
        if bot_user is None:
            return False

        mention = f"<#{thread_id}>"
        try:
            async for message in channel.history(limit=None):
                if message.author.id != bot_user.id:
                    continue
                if not message.content.startswith(SESSION_NOTIFICATION_PREFIXES):
                    continue
                if mention not in message.content:
                    continue
                if self.sessions.get_by_thread(thread_id) is not None:
                    return True  # A reconnect took the thread, and its notification, during the scan.
                await self.delete_notification_message(message)
                return True
        except discord.DiscordException:
            logger.warning("Unable to delete Agent session notification for thread %s", thread_id)
            return False
        return True

    async def get_thread_for_cleanup(self, thread_id: int) -> tuple[discord.Thread | None, bool]:
        channel = self.thread_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel, True
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except discord.NotFound:
            return None, True
        except discord.DiscordException:
            logger.warning("Unable to fetch Agent session thread %s for cleanup", thread_id)
            return None, False
        return (fetched, True) if isinstance(fetched, discord.Thread) else (None, True)

    def thread_channel(self, thread_id: int) -> object | None:
        """discord.py's cached channel, else the thread this bridge attached.

        discord.py caches a reopened thread again only when its gateway update arrives, after the REST reply; an event
        sent right after hello_ack must not be dropped in that window.
        """
        channel = self.bot.get_channel(thread_id)
        return channel if channel is not None else self._attached_threads.get(thread_id)

    async def get_thread(self, thread_id: int) -> discord.Thread | None:
        channel = self.thread_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except discord.DiscordException:
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    def operator_role_name(self) -> str:
        return self.bot.config.agent_session.operator_role_name or self.bot.config.discord.employee_role_name

    def is_operator(self, user: discord.User | discord.Member) -> bool:
        role_name = self.operator_role_name()
        if not role_name:
            return False
        if not isinstance(user, discord.Member):
            return False
        return any(role.name == role_name for role in user.roles)

    def request_user_input_view(
        self,
        session_id: str,
        request: RemoteRequestUserInput,
    ) -> RequestUserInputView:
        return RequestUserInputView(self, session_id, request)

    @staticmethod
    def can_render_request_user_input_as_select(request: RemoteRequestUserInput) -> bool:
        return len(request.questions) == 1 and bool(request.questions[0].options) and len(request.questions[0].options) <= 25

    @staticmethod
    def format_approval_request(approval: RemoteApprovalRequest) -> str:
        """The approval message, untruncated; a raw command_text is shown verbatim instead of the re-quoted argv."""
        if approval.content_text is not None:
            return format_content_approval(approval.approval_kind, approval.content_text)
        if approval.command_text is not None:
            command = approval.command_text
        else:
            command = shlex.join(approval.command) if approval.command else ""
        parts = [
            "**Approval requested**",
            "Quick review: `✅` approve · `✖️` deny",
            "",
            f"```sh\n{command[:APPROVAL_COMMAND_DISPLAY_LIMIT]}\n```",
        ]
        if approval.cwd:
            parts.append(f"cwd: `{approval.cwd}`")
        if approval.reason:
            parts.extend(["", approval.reason[:500]])
        return "\n".join(parts)

    @staticmethod
    def format_approval_pending(
        decision: Literal["approved", "denied"],
        user: discord.User | discord.Member,
    ) -> str:
        label = "Approval sent" if decision == "approved" else "Denial sent"
        return f"**{label}**\nWaiting for local agent to accept the decision.\nby: `{user}`"

    @staticmethod
    def format_approval_finished(decision: str | None, decided_by: int | None) -> str:
        label = f"Submitted: {decision}" if decision in {"approved", "denied"} else "Decision acknowledged"
        by = f"\nby: `{decided_by}`" if decided_by is not None else ""
        return f"**{label}**{by}"

    @staticmethod
    def format_request_user_input(
        request: RemoteRequestUserInput,
        answers: dict[str, str] | None = None,
    ) -> str:
        answers = answers or {}
        parts = ["**Need input**", "Use the controls below, then press **Submit**."]
        for question in request.questions:
            header = question.header or question.id or "Question"
            answer = answers.get(question.id, "").strip()
            status = "✅" if answer else "⬜"
            parts.extend(["", f"{status} **{header}**"])
            if question.question:
                parts.append(question.question)
            if answer:
                value = "[hidden]" if question.is_secret else answer
                parts.append(f"Selected: `{value[:200]}`")
            if question.options:
                for option in question.options:
                    line = f"- {option.label}"
                    if option.description:
                        line = f"{line}: {option.description}"
                    parts.append(line[:200])
            elif question.is_secret:
                parts.append("Respond privately through the attached form.")
        return "\n".join(parts)[:DISCORD_MESSAGE_LIMIT]

    @staticmethod
    def format_request_user_input_pending(
        user: discord.User | discord.Member,
        *,
        cancelled: bool = False,
    ) -> str:
        label = "Answer cancelled" if cancelled else "Answer sent"
        return f"**{label}**\nWaiting for local agent to accept the response.\nby: `{user}`"

    @staticmethod
    def format_user_message_notice(message: str) -> str:
        return format_user_message(message)

    @staticmethod
    def session_control_reactions(session: AgentSession) -> list[str]:
        if session.pending_control_confirmation is not None and session.hello.supports("end_session"):
            return [REACTION_APPROVAL_APPROVE, REACTION_APPROVAL_DENY]
        if session.control_status_reaction is not None:
            active_command = (
                session.pending_commands.get(session.active_command_id) if session.active_command_id is not None else None
            )
            command_can_be_interrupted = active_command is not None and active_command.kind in {
                "continue_autonomously",
                "reply",
            }
            status_can_be_interrupted = session.control_status_reaction in {
                REACTION_QUEUED,
                REACTION_DELIVERED,
                REACTION_IN_PROGRESS,
                REACTION_COMPACTING,
            }
            if status_can_be_interrupted and (session.control_interruptions_enabled or command_can_be_interrupted):
                return [session.control_status_reaction] + [
                    emoji
                    for emoji, action in [(REACTION_CONTROL_PAUSE, "pause_current_turn"), (REACTION_CONTROL_END, "end_session")]
                    if session.hello.supports(action)
                ]
            return [session.control_status_reaction]
        return [
            emoji
            for emoji, action in [
                (REACTION_CONTROL_CONTINUE, "continue_autonomously"),
                (REACTION_CONTROL_STATUS, None),
                (REACTION_CONTROL_END, "end_session"),
            ]
            if action is None or session.hello.supports(action)
        ]

    async def refresh_control_card(
        self,
        session: AgentSession,
        thread: discord.Thread,
        message_id: int,
        reactions: list[str],
        *,
        remove_user_reaction: tuple[str, discord.User | discord.Member] | None = None,
    ) -> bool:
        async with session.control_card_lock:
            try:
                message = await thread.fetch_message(message_id)
                await message.edit(
                    content=None,
                    embeds=[],
                    view=session_status_card(session, reactions),
                    allowed_mentions=agent_session_allowed_mentions(),
                )
            except discord.NotFound:
                return False
            except discord.DiscordException:
                # Retain a reachable anchor on an edit failure; don't duplicate it.
                logger.warning("Unable to update Agent session status card %s", message_id)
            await self.write_message_reactions(message, reactions, clear=True, remove_user_reaction=remove_user_reaction)
            return True

    async def refresh_session_controls(
        self,
        session: AgentSession,
        thread: discord.Thread,
        *,
        remove_user_reaction: tuple[str, discord.User | discord.Member] | None = None,
    ) -> None:
        if session.control_message_id is None:
            return
        replaced = await self.refresh_control_card(
            session,
            thread,
            session.control_message_id,
            self.session_control_reactions(session),
            remove_user_reaction=remove_user_reaction,
        )
        if replaced:
            return
        session.control_message_id = None
        message = await send_agent_session_message(
            thread,
            view=session_status_card(session, self.session_control_reactions(session)),
        )
        session.control_message_id = message.id  # Before its reactions, so a tap on the first one counts.
        await self.add_message_reactions(message, self.session_control_reactions(session))

    def _authorized(self, request: web.Request) -> bool:
        token = self.bot.config.agent_session.token
        if not token:
            return False
        expected = f"Bearer {token}"
        return request.headers.get("Authorization") == expected

    async def add_message_reactions(
        self,
        message: discord.Message,
        reactions: list[str],
    ) -> None:
        await self.write_message_reactions(message, reactions)

    async def clear_message_reactions(self, message: discord.Message) -> None:
        await self.write_message_reactions(message, [], clear=True)

    async def write_message_reactions(
        self,
        message: discord.Message,
        reactions: list[str],
        *,
        clear: bool = False,
        remove_user_reaction: tuple[str, discord.User | discord.Member] | None = None,
    ) -> None:
        """Change the bot's reactions on `message`, superseding any change still under way.

        The bot adds reactions one request at a time, and an operator can tap the first while the rest are still
        coming. The handler's change must be the last word: it waits for a request already in flight, which would
        otherwise land after it, and the superseded change sends nothing more.
        """
        writes = self._reaction_writes.setdefault(message.id, ReactionWrites())
        writes.latest += 1
        mine = writes.latest
        # (request, reaction to name if it fails); only a failed add is worth a warning.
        steps: list[tuple[Callable[[], Awaitable[None]], str | None]] = []
        if remove_user_reaction is not None:
            emoji, user = remove_user_reaction
            steps.append((lambda: message.remove_reaction(emoji, user), None))
        if clear:
            steps.append((message.clear_reactions, None))
        steps.extend((functools.partial(message.add_reaction, reaction), reaction) for reaction in reactions)
        try:
            for request, added in steps:
                async with writes.lock:
                    if writes.latest != mine:
                        return
                    try:
                        await request()
                    except discord.DiscordException:
                        if added is not None:
                            logger.warning("Unable to add Agent session reaction %s to %s", added, message.id)
        finally:
            if writes.latest == mine and self._reaction_writes.get(message.id) is writes:
                del self._reaction_writes[message.id]

    @staticmethod
    async def remove_message_reaction(
        thread: discord.Thread,
        message_id: int,
        reaction: str,
        user: discord.User | discord.Member,
    ) -> None:
        try:
            message = await thread.fetch_message(message_id)
            await message.remove_reaction(reaction, user)
        except discord.DiscordException:
            logger.warning("Unable to remove Agent session reaction %s from %s", reaction, message_id)

    async def replace_message_reactions(
        self,
        thread: discord.Thread,
        message_id: int,
        reactions: list[str],
        *,
        remove_user_reaction: tuple[str, discord.User | discord.Member] | None = None,
    ) -> bool:
        session = self.sessions.get_by_thread(thread.id)
        if session is not None and session.control_message_id == message_id:
            return await self.refresh_control_card(
                session,
                thread,
                message_id,
                reactions,
                remove_user_reaction=remove_user_reaction,
            )
        try:
            message = await thread.fetch_message(message_id)
        except discord.DiscordException:
            logger.warning("Unable to replace Agent session reactions on %s", message_id)
            return False
        await self.write_message_reactions(message, reactions, clear=True, remove_user_reaction=remove_user_reaction)
        return True
