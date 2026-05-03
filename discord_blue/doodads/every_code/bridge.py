from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import subprocess
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import discord
from aiohttp import WSMsgType, web

from discord_blue.doodads.every_code.protocol import (
    RequestUserInputQuestion,
    RemoteApprovalDecision,
    RemoteApprovalRequest,
    RemoteCommand,
    RemoteRequestUserInput,
    SessionHello,
    SessionStatus,
    UserMessage,
)
from discord_blue.doodads.every_code.sessions import (
    EveryCodeSession,
    EveryCodeSessionRegistry,
    PendingRemoteApproval,
    PendingRemoteCommand,
    PendingRemoteUserInput,
    RejectedCommandMessage,
)
from discord_blue.doodads.every_code.threads import SessionThread
from discord_blue.doodads.every_code.threads import auto_join_configured_users
from discord_blue.doodads.every_code.threads import create_session_thread
from discord_blue.doodads.every_code.threads import get_every_code_channel
from discord_blue.doodads.every_code.threads import session_start_message
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
DISCORD_ASSISTANT_CHUNK_LIMIT = 1800
DISCORD_CODE_FENCE_WRAP_RESERVE = 80
STARTUP_RECONNECT_GRACE_SECONDS = 20
SHUTDOWN_WEBSOCKET_CLOSE_TIMEOUT_SECONDS = 2
SHUTDOWN_RUNNER_CLEANUP_TIMEOUT_SECONDS = 5
SESSION_START_PREFIX = "Every Code session connected"
SESSION_NOTIFICATION_PREFIX = "Every Code session connected for "
MARKDOWN_CODE_FENCE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^`~\n]*)$")
RESUME_SESSION_RE = re.compile(
    r"\bresume\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
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
        self.parent_view.set_answer(self.question.id, value)
        await interaction.response.edit_message(
            content=self.parent_view.format_prompt(),
            view=self.parent_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class RequestUserInputAnswerModal(discord.ui.Modal):
    def __init__(
        self,
        parent_view: RequestUserInputView,
        question: RequestUserInputQuestion,
    ) -> None:
        title = (question.header or question.question or "Every Code input")[:45]
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
        self.parent_view.set_answer(self.question.id, self.answer.value.strip())
        await interaction.response.edit_message(
            content=self.parent_view.format_prompt(),
            view=self.parent_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


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
        bridge: EveryCodeBridge,
        session_id: str,
        request: RemoteRequestUserInput,
    ) -> None:
        super().__init__(timeout=3600)
        self.bridge = bridge
        self.session_id = session_id
        self.request = request
        self.answers: dict[str, str] = {}

        for question in request.questions[:4]:
            if question.options:
                self.add_item(RequestUserInputSelect(self, question))
            else:
                self.add_item(RequestUserInputAnswerButton(self, question))
        self.add_item(RequestUserInputSubmitButton(self))
        self.add_item(RequestUserInputCancelButton(self))

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
        )

    async def cancel(self, interaction: discord.Interaction[BlueBot]) -> None:
        await self.bridge.handle_request_user_input_interaction(
            interaction,
            self.session_id,
            self.request.call_id,
            self.request.turn_id,
            {"answers": {}},
            cancelled=True,
        )


class EveryCodeBridge:
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        self.sessions = EveryCodeSessionRegistry()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._runner is not None:
            return

        app = web.Application()
        app.router.add_get("/every-code/connect", self.handle_connect)
        self._runner = web.AppRunner(app, shutdown_timeout=SHUTDOWN_RUNNER_CLEANUP_TIMEOUT_SECONDS)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            self.bot.config.every_code.listen_host,
            self.bot.config.every_code.listen_port,
        )
        await self._site.start()
        logger.info(
            "Every Code bridge listening on %s:%s",
            self.bot.config.every_code.listen_host,
            self.bot.config.every_code.listen_port,
        )
        self._cleanup_task = asyncio.create_task(self.cleanup_stale_sessions())
        self._heartbeat_task = asyncio.create_task(self.monitor_heartbeats())

    async def stop(self) -> None:
        if self._runner is None:
            return
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        await self.disconnect_active_sessions()
        try:
            await asyncio.wait_for(self._runner.cleanup(), timeout=SHUTDOWN_RUNNER_CLEANUP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("Every Code bridge runner cleanup timed out during shutdown")
        finally:
            self._runner = None
            self._site = None

    async def disconnect_active_sessions(self) -> None:
        close_tasks: list[asyncio.Task[None]] = []
        for session_id in list(self.sessions.by_session):
            session = self.sessions.remove(session_id)
            if session is None or session.websocket.closed:
                continue
            close_tasks.append(asyncio.create_task(self.close_session_websocket(session_id, session)))

        if close_tasks:
            await asyncio.gather(*close_tasks)

    async def close_session_websocket(self, session_id: str, session: EveryCodeSession) -> None:
        try:
            await asyncio.wait_for(
                session.websocket.close(message=b"bridge shutdown", drain=False),
                timeout=SHUTDOWN_WEBSOCKET_CLOSE_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("Unable to close Every Code websocket %s during shutdown", session_id, exc_info=True)

    async def close_active_sessions(self) -> None:
        for session_id in list(self.sessions.by_session):
            session = self.sessions.remove(session_id)
            if session is not None:
                await self.close_session_thread(session)

    async def cleanup_stale_sessions(self) -> None:
        await asyncio.sleep(STARTUP_RECONNECT_GRACE_SECONDS)
        await self.cleanup_stale_session_notifications()
        await self.cleanup_stale_session_threads()

    async def monitor_heartbeats(self) -> None:
        while True:
            await asyncio.sleep(self.bot.config.every_code.heartbeat_check_interval_seconds)
            await self.close_timed_out_sessions()

    async def close_timed_out_sessions(self) -> None:
        timeout = timedelta(seconds=self.bot.config.every_code.heartbeat_timeout_seconds)
        now = datetime.now(UTC)
        for session_id, session in list(self.sessions.by_session.items()):
            if now - session.last_seen <= timeout:
                continue

            removed = self.sessions.remove(session_id)
            if removed is None:
                continue

            logger.warning(
                "Every Code session %s timed out after %s seconds without heartbeat",
                session_id,
                self.bot.config.every_code.heartbeat_timeout_seconds,
            )
            await removed.websocket.close(message=b"heartbeat timeout")
            await self.close_session_thread(removed)

    async def cleanup_stale_session_notifications(self) -> None:
        try:
            channel = await get_every_code_channel(self.bot)
        except ValueError:
            logger.warning("Unable to clean Every Code notifications: channel is unavailable")
            return

        bot_user = self.bot.user
        if bot_user is None:
            return

        deleted = 0
        try:
            async for message in channel.history():
                if message.author.id != bot_user.id:
                    continue
                if not message.content.startswith(SESSION_NOTIFICATION_PREFIX):
                    continue
                try:
                    await message.delete()
                    deleted += 1
                except discord.DiscordException:
                    logger.warning(
                        "Unable to delete stale Every Code notification %s",
                        message.id,
                    )
        except discord.DiscordException:
            logger.warning("Unable to scan Every Code channel for stale notifications")
            return

        if deleted:
            logger.info("Deleted %s stale Every Code notification(s)", deleted)

    async def cleanup_stale_session_threads(self) -> None:
        try:
            channel = await get_every_code_channel(self.bot)
        except ValueError:
            logger.warning("Unable to clean Every Code threads: channel is unavailable")
            return

        candidates = await self.session_thread_candidates(channel)

        closed = 0
        seen: set[int] = set()
        for thread in candidates:
            if thread.id in seen:
                continue
            seen.add(thread.id)
            if thread.id in self.sessions.by_thread:
                continue
            if not await self.is_every_code_session_thread(thread):
                continue
            if thread.id in self.sessions.by_thread:
                continue
            await self.close_thread(thread)
            closed += 1

        if closed:
            logger.info("Closed %s stale Every Code thread(s)", closed)

    async def is_every_code_session_thread(self, thread: discord.Thread) -> bool:
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
            logger.warning("Unable to inspect Every Code thread %s", thread.id)
        return False

    async def handle_connect(self, request: web.Request) -> web.WebSocketResponse:
        if not self._authorized(request):
            raise web.HTTPUnauthorized()

        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        session: EveryCodeSession | None = None

        async for message in websocket:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except json.JSONDecodeError:
                logger.warning("Invalid Every Code bridge JSON: %s", message.data)
                continue

            message_type = payload.get("type")
            if message_type == "hello":
                hello = SessionHello.from_payload(payload)
                session = EveryCodeSession(hello=hello, websocket=websocket)
                self.sessions.register(session)
                session_thread = await self.find_or_create_session_thread(hello)
                self.sessions.bind_thread(
                    hello.session_id,
                    session_thread.thread.id,
                    session_thread.notification_message_id,
                )
                await self.backfill_latest_assistant_message(
                    session_thread.thread,
                    hello,
                )
                await websocket.send_json({"type": "hello_ack", "thread_id": session_thread.thread.id})
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
                logger.info("Every Code approval decision ack: %s", payload.get("approval_id"))
                await self.handle_approval_decision_ack(payload)
            elif message_type == "approval_decision_reject":
                logger.warning("Every Code approval decision reject: %s", payload)
                await self.handle_approval_decision_reject(payload)
            elif message_type == "command_ack":
                logger.info("Every Code command ack: %s", payload.get("command_id"))
                await self.handle_command_ack(payload)
            elif message_type == "command_reject":
                logger.warning("Every Code command reject: %s", payload)
                await self.handle_command_reject(payload)

        if session is not None:
            removed = self.sessions.remove_if_current(session)
            if removed is not None:
                await self.close_session_thread(removed)

        return websocket

    async def find_or_create_session_thread(self, hello: SessionHello) -> SessionThread:
        thread = await self.find_existing_session_thread(hello)
        if thread is None:
            return await create_session_thread(self.bot, hello)

        if thread.archived or thread.locked:
            try:
                await thread.edit(
                    archived=False,
                    locked=False,
                    reason="Reattaching live Every Code session after bridge restart",
                )
            except discord.DiscordException:
                logger.warning("Unable to reopen Every Code thread %s", thread.id)
        await auto_join_configured_users(self.bot, thread)
        return SessionThread(thread=thread, notification_message_id=None)

    async def find_existing_session_thread(self, hello: SessionHello) -> discord.Thread | None:
        try:
            channel = await get_every_code_channel(self.bot)
        except ValueError:
            logger.warning("Unable to find reusable Every Code thread: channel is unavailable")
            return None

        expected_start = session_start_message(hello)
        best_thread: discord.Thread | None = None
        best_score: tuple[int, int, int] | None = None
        seen: set[int] = set()
        for thread in await self.session_thread_candidates(channel):
            if thread.id in seen:
                continue
            seen.add(thread.id)
            mapped_session_id = self.sessions.by_thread.get(thread.id)
            if mapped_session_id is not None and mapped_session_id != hello.session_id:
                continue
            if not await self.session_thread_matches(thread, expected_start):
                continue
            score = await self.score_session_thread(thread)
            if best_score is None or score > best_score:
                best_thread = thread
                best_score = score
        return best_thread

    @staticmethod
    async def session_thread_candidates(
        channel: discord.TextChannel,
    ) -> list[discord.Thread]:
        candidates = list(channel.threads)
        for private in (False, True):
            try:
                async for thread in channel.archived_threads(
                    private=private,
                    joined=True,
                    limit=50,
                ):
                    candidates.append(thread)
            except (discord.DiscordException, ValueError):
                logger.warning("Unable to scan archived Every Code threads")
        return candidates

    async def session_thread_matches(
        self,
        thread: discord.Thread,
        expected_start: str,
    ) -> bool:
        bot_user = self.bot.user
        if bot_user is None:
            return False
        try:
            async for message in thread.history(limit=10, oldest_first=True):
                if message.author.id != bot_user.id:
                    continue
                if message.content == expected_start:
                    return True
        except discord.DiscordException:
            logger.warning("Unable to inspect Every Code thread %s", thread.id)
        return False

    @staticmethod
    async def score_session_thread(thread: discord.Thread) -> tuple[int, int, int]:
        assistant_messages = 0
        messages = 0
        try:
            async for message in thread.history(limit=50):
                messages += 1
                if message.content.startswith("**Assistant**"):
                    assistant_messages += 1
        except discord.DiscordException:
            logger.warning("Unable to score Every Code thread %s", thread.id)
        return assistant_messages, messages, thread.id

    async def backfill_latest_assistant_message(
        self,
        thread: discord.Thread,
        hello: SessionHello,
    ) -> None:
        if await self.thread_has_assistant_message(thread):
            return
        assistant_message = await asyncio.to_thread(
            self.recover_latest_assistant_message,
            hello,
        )
        if assistant_message is None:
            return
        for message in self.format_assistant_messages(assistant_message):
            await thread.send(
                message[:DISCORD_MESSAGE_LIMIT],
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @staticmethod
    async def thread_has_assistant_message(thread: discord.Thread) -> bool:
        try:
            async for message in thread.history(limit=50):
                if message.content.startswith("**Assistant**"):
                    return True
        except discord.DiscordException:
            logger.warning("Unable to inspect Every Code assistant history %s", thread.id)
        return False

    def recover_latest_assistant_message(self, hello: SessionHello) -> str | None:
        session_id = self.resume_session_id_for_pid(hello.pid)
        if session_id is None:
            return None
        rollout_path = self.rollout_path_for_session(session_id)
        if rollout_path is None:
            return None
        return self.latest_assistant_message_from_rollout(rollout_path)

    @staticmethod
    def resume_session_id_for_pid(pid: int) -> str | None:
        if pid <= 0:
            return None
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        match = RESUME_SESSION_RE.search(result.stdout)
        return match.group(1) if match else None

    @staticmethod
    def rollout_path_for_session(session_id: str) -> Path | None:
        code_home = Path.home() / ".code"
        catalog_path = code_home / "sessions" / "index" / "catalog.jsonl"
        try:
            lines = catalog_path.read_text(errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("session_id") != session_id:
                continue
            rollout_path = entry.get("rollout_path")
            if not isinstance(rollout_path, str) or not rollout_path:
                return None
            return code_home / rollout_path
        return None

    @staticmethod
    def latest_assistant_message_from_rollout(rollout_path: Path) -> str | None:
        try:
            lines = rollout_path.read_text(errors="replace").splitlines()
        except OSError:
            return None
        latest: str | None = None
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            message = payload.get("msg")
            if not isinstance(message, dict):
                continue
            if message.get("type") != "agent_message":
                continue
            text = message.get("message")
            if isinstance(text, str) and text.strip():
                latest = text.strip()
        return latest

    async def send_thread_reply(self, message: discord.Message) -> bool:
        if not isinstance(message.channel, discord.Thread):
            return False

        session = self.sessions.get_by_thread(message.channel.id)
        if session is None:
            return False
        if session.websocket.closed:
            await message.reply("Every Code session is offline; reply was not delivered.", mention_author=False)
            return True
        text = message.content.strip()
        if not text or text.startswith("!"):
            return False

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="reply",
            text=text,
            issued_by=str(message.author.id),
        )
        session.pending_commands[command.command_id] = PendingRemoteCommand(
            thread_id=message.channel.id,
            message_id=message.id,
            kind="reply",
        )
        await self.set_message_reaction(message.channel.id, message.id, REACTION_QUEUED)
        await self.show_active_session_controls(session, message.channel, REACTION_QUEUED)
        await session.websocket.send_json(command.to_message())
        return True

    async def send_continue_autonomously(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code go-ahead` inside an Every Code session thread."
        if not self.is_operator(user):
            return "Only Every Code operators can ask a session to continue."

        session = self.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live Every Code session."
        if session.websocket.closed:
            return "Every Code session is offline; go-ahead was not delivered."

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="continue_autonomously",
            issued_by=str(user.id),
        )
        session.pending_commands[command.command_id] = PendingRemoteCommand(
            thread_id=channel.id,
            message_id=session.control_message_id,
            kind="continue_autonomously",
            reject_notice="Every Code could not go ahead",
        )
        await session.websocket.send_json(command.to_message())
        return "Asked Every Code to go ahead until it needs you."

    async def send_pause_current_turn(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code pause` inside an Every Code session thread."
        if not self.is_operator(user):
            return "Only Every Code operators can pause a turn."

        session = self.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live Every Code session."
        if session.websocket.closed:
            return "Every Code session is offline; pause was not delivered."

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="pause_current_turn",
            issued_by=str(user.id),
        )
        session.pending_commands[command.command_id] = PendingRemoteCommand(
            thread_id=channel.id,
            message_id=session.control_message_id,
            kind="pause_current_turn",
            reject_notice="Every Code could not pause the current turn",
        )
        await session.websocket.send_json(command.to_message())
        return "Asked Every Code to pause what it is doing now."

    async def send_new_session(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code new` inside an Every Code session thread."
        if not self.is_operator(user):
            return "Only Every Code operators can start a new session."

        session = self.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live Every Code session."
        if session.websocket.closed:
            return "Every Code session is offline; new session was not started."

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="new_session",
            issued_by=str(user.id),
        )
        session.pending_commands[command.command_id] = PendingRemoteCommand(
            thread_id=channel.id,
            message_id=session.control_message_id,
            kind="new_session",
            reject_notice="Every Code could not start a new session",
        )
        await session.websocket.send_json(command.to_message())
        return "Asked Every Code to start a new session in this folder."

    async def send_end_session(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code end-session` inside an Every Code session thread."
        if not self.is_operator(user):
            return "Only Every Code operators can end a session."

        session = self.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live Every Code session."
        if session.websocket.closed:
            return "Every Code session is already offline."

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="end_session",
            issued_by=str(user.id),
        )
        session.pending_commands[command.command_id] = PendingRemoteCommand(
            thread_id=channel.id,
            message_id=session.control_message_id,
            kind="end_session",
            reject_notice="Every Code could not end the session",
        )
        await session.websocket.send_json(command.to_message())
        return "Asked Every Code to end this session."

    async def handle_go_ahead_interaction(
        self,
        interaction: discord.Interaction[BlueBot],
    ) -> None:
        response = await self.send_continue_autonomously(
            interaction.channel,
            interaction.user,
        )
        await interaction.response.send_message(response, ephemeral=True)
        if response != "Asked Every Code to go ahead until it needs you.":
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
        sessions = list(self.sessions.by_session.values())
        if not sessions:
            return "No live Every Code sessions."

        lines = ["Live Every Code sessions:"]
        for session in sessions:
            repo = Path(session.hello.cwd).name or "session"
            branch = f" on `{session.hello.branch}`" if session.hello.branch else ""
            thread = f" <#{session.thread_id}>" if session.thread_id is not None else ""
            state = "offline" if session.websocket.closed else "online"
            lines.append(f"- `{repo}`{branch} ({state}, {session.hello.host_label}){thread}")
        return "\n".join(lines)

    def session_status_summary(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code status` inside an Every Code session thread."
        if not self.is_operator(user):
            return "Only Every Code operators can inspect session status."

        session = self.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live Every Code session."

        repo = Path(session.hello.cwd).name or "session"
        branch = f" on `{session.hello.branch}`" if session.hello.branch else ""
        state = "offline" if session.websocket.closed else "online"
        status = session.last_status_message or "No status update received yet."
        return "\n".join(
            [
                f"Every Code `{repo}`{branch}",
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
            await self.update_command_message_reaction(session, command, REACTION_REJECTED)
            if command.message_id is not None:
                session.rejected_command_messages.append(
                    RejectedCommandMessage(
                        thread_id=command.thread_id,
                        message_id=command.message_id,
                    )
                )
            if command.reject_notice is not None:
                reason = str(payload.get("reason") or "command was rejected")
                await self.post_thread_notice(command.thread_id, f"{command.reject_notice}: {reason}")

    def command_context(self, payload: dict[str, object]) -> tuple[str, EveryCodeSession] | None:
        command_id = str(payload.get("command_id") or "")
        session_id = str(payload.get("session_id") or "")
        if not command_id or not session_id:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            return None
        return command_id, session

    async def update_command_message_reaction(
        self,
        session: EveryCodeSession,
        command: PendingRemoteCommand,
        reaction: str,
    ) -> None:
        if command.message_id is None:
            return
        if command.message_id == session.control_message_id:
            session.control_status_reaction = None if reaction == REACTION_REJECTED else reaction
            channel = self.bot.get_channel(command.thread_id)
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
            logger.warning("Every Code approval for unknown session: %s", approval.session_id)
            return
        if approval.session_epoch != session.session_epoch:
            logger.warning("Every Code approval for stale session epoch: %s", approval.session_id)
            return

        channel = self.bot.get_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return

        message = await channel.send(
            self.format_approval_request(approval),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.add_message_reactions(
            message,
            [REACTION_APPROVAL_APPROVE, REACTION_APPROVAL_DENY],
        )
        session.pending_approvals[approval.approval_id] = PendingRemoteApproval(
            thread_id=session.thread_id,
            message_id=message.id,
        )

    async def handle_request_user_input(self, request: RemoteRequestUserInput) -> None:
        session = self.sessions.get(request.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Every Code request_user_input for unknown session: %s", request.session_id)
            return
        if request.session_epoch != session.session_epoch:
            logger.warning(
                "Every Code request_user_input for stale session epoch: %s",
                request.session_id,
            )
            return

        await self.clear_pending_user_inputs(
            session,
            "Every Code is waiting on a newer prompt.",
        )

        channel = self.bot.get_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return

        message = await channel.send(
            self.format_request_user_input(request, {}),
            allowed_mentions=discord.AllowedMentions.none(),
            view=self.request_user_input_view(session.session_id, request),
        )
        session.pending_user_inputs[request.turn_id] = PendingRemoteUserInput(
            thread_id=session.thread_id,
            message_id=message.id,
            turn_id=request.turn_id,
        )

    async def handle_request_user_input_interaction(
        self,
        interaction: discord.Interaction[BlueBot],
        session_id: str,
        call_id: str,
        turn_id: str,
        response: dict[str, object],
        *,
        cancelled: bool = False,
    ) -> None:
        if not self.is_operator(interaction.user):
            await interaction.response.send_message(
                "Only Every Code operators can answer prompts.",
                ephemeral=True,
            )
            return

        session = self.sessions.get(session_id)
        if session is None or session.websocket.closed:
            await interaction.response.send_message(
                "Every Code session is offline; answer was not delivered.",
                ephemeral=True,
            )
            return

        pending = session.pending_user_inputs.get(turn_id)
        if pending is None:
            await interaction.response.send_message(
                "This prompt is no longer active.",
                ephemeral=True,
            )
            return

        command_id = str(uuid.uuid4())
        session.pending_commands[command_id] = PendingRemoteCommand(
            thread_id=pending.thread_id,
            message_id=pending.message_id,
            kind="request_user_input_response",
        )
        await session.websocket.send_json(
            RemoteCommand(
                command_id=command_id,
                session_id=session.session_id,
                session_epoch=session.session_epoch,
                kind="request_user_input_response",
                call_id=call_id,
                turn_id=turn_id,
                response=response,
                issued_by=str(interaction.user.id),
            ).to_message()
        )
        await interaction.response.edit_message(
            content=self.format_request_user_input_pending(interaction.user, cancelled=cancelled),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_approval_interaction(
        self,
        interaction: discord.Interaction[BlueBot],
        session_id: str,
        approval_id: str,
        decision: Literal["approved", "denied"],
    ) -> None:
        if not self.is_operator(interaction.user):
            await interaction.response.send_message(
                "Only Every Code operators can respond to approvals.",
                ephemeral=True,
            )
            return

        session = self.sessions.get(session_id)
        if session is None or session.websocket.closed:
            await interaction.response.send_message(
                "Every Code session is offline; approval was not delivered.",
                ephemeral=True,
            )
            return

        pending = session.pending_approvals.get(approval_id)
        if pending is None:
            await interaction.response.send_message(
                "This approval is no longer active.",
                ephemeral=True,
            )
            return

        pending.decision = decision
        pending.decided_by = interaction.user.id
        await session.websocket.send_json(
            RemoteApprovalDecision(
                approval_id=approval_id,
                session_id=session.session_id,
                session_epoch=session.session_epoch,
                decision=decision,
            ).to_message()
        )
        await interaction.response.edit_message(
            content=self.format_approval_pending(decision, interaction.user),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
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
        session: EveryCodeSession,
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
            if response == "Asked Every Code to go ahead until it needs you.":
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
            if response == "Asked Every Code to pause what it is doing now.":
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
        session: EveryCodeSession,
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
        if response == "Asked Every Code to end this session.":
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
        session: EveryCodeSession,
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
            await self.post_thread_notice(thread.id, "Every Code session is offline; approval was not delivered.")
            return True

        decision: Literal["approved", "denied"]
        if emoji == REACTION_APPROVAL_APPROVE:
            decision = "approved"
        else:
            decision = "denied"

        pending.decision = decision
        pending.decided_by = user.id
        await session.websocket.send_json(
            RemoteApprovalDecision(
                approval_id=approval_id,
                session_id=session.session_id,
                session_epoch=session.session_epoch,
                decision=decision,
            ).to_message()
        )
        await self.edit_approval_message(
            pending,
            self.format_approval_pending(decision, user),
        )
        return True

    async def handle_approval_decision_ack(self, payload: dict[str, object]) -> None:
        session_id = str(payload.get("session_id") or "")
        approval_id = str(payload.get("approval_id") or "")
        session = self.sessions.get(session_id)
        if session is None or not approval_id:
            return
        pending = session.pending_approvals.pop(approval_id, None)
        if pending is None:
            return
        await self.edit_approval_message(
            pending,
            self.format_approval_finished(pending.decision, pending.decided_by),
        )

    async def handle_approval_decision_reject(self, payload: dict[str, object]) -> None:
        session_id = str(payload.get("session_id") or "")
        approval_id = str(payload.get("approval_id") or "")
        reason = str(payload.get("reason") or "approval was rejected")
        session = self.sessions.get(session_id)
        if session is None or not approval_id:
            return
        pending = session.pending_approvals.pop(approval_id, None)
        if pending is None:
            return
        await self.edit_approval_message(pending, f"**Approval expired**\n{reason}")

    async def edit_approval_message(self, pending: PendingRemoteApproval, content: str) -> None:
        channel = self.bot.get_channel(pending.thread_id)
        if not isinstance(channel, discord.Thread):
            return
        try:
            message = await channel.fetch_message(pending.message_id)
            await message.edit(
                content=content[:DISCORD_MESSAGE_LIMIT],
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.clear_message_reactions(message)
        except discord.DiscordException:
            logger.warning("Unable to edit Every Code approval message %s", pending.message_id)

    async def handle_session_status(self, message_type: str, status: SessionStatus) -> None:
        session = self.sessions.get(status.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Every Code status for unknown session: %s", status.session_id)
            return
        if status.session_epoch != session.session_epoch:
            logger.warning("Every Code status for stale session epoch: %s", status.session_id)
            return
        session.last_status_message = status.message

        if message_type == "status_changed":
            status_message = (status.message or "").lower()
            if status_message == "turn aborted":
                reaction = REACTION_REJECTED
            elif "compact" in status_message:
                reaction = REACTION_COMPACTING
            else:
                reaction = REACTION_IN_PROGRESS
            await self.clear_pending_user_inputs(
                session,
                "Every Code is no longer waiting on this prompt.",
            )
            await self.update_session_status_reaction(session, reaction)
            return

        if message_type == "error":
            await self.clear_pending_user_inputs(
                session,
                "Every Code stopped waiting on this prompt.",
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
                "Every Code is no longer waiting on this prompt.",
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
            logger.warning("Every Code user message for unknown session: %s", user_message.session_id)
            return
        if user_message.session_epoch != session.session_epoch:
            logger.warning("Every Code user message for stale session epoch: %s", user_message.session_id)
            return
        if not user_message.message.strip():
            return
        channel = self.bot.get_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return

        await channel.send(
            self.format_user_message_notice(user_message.message)[:DISCORD_MESSAGE_LIMIT],
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.spawn_session_controls(
            session,
            reaction=REACTION_IN_PROGRESS,
            interruptions_enabled=True,
        )

    async def update_active_command_reaction(
        self,
        session: EveryCodeSession,
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

    async def clear_rejected_command_reactions(self, session: EveryCodeSession) -> None:
        rejected_messages = session.rejected_command_messages
        session.rejected_command_messages = []
        for rejected_message in rejected_messages:
            await self.clear_message_transient_reactions(rejected_message.thread_id, rejected_message.message_id)

    async def update_session_status_reaction(
        self,
        session: EveryCodeSession,
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
        session: EveryCodeSession,
        thread_id: int,
        reaction: str,
    ) -> None:
        channel = self.bot.get_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        session.control_status_reaction = reaction
        await self.show_or_refresh_session_controls(session, channel)

    async def show_active_session_controls(
        self,
        session: EveryCodeSession,
        thread: discord.Thread,
        reaction: str,
    ) -> None:
        session.control_status_reaction = reaction
        session.control_interruptions_enabled = True
        await self.show_or_refresh_session_controls(session, thread)

    async def show_or_refresh_session_controls(
        self,
        session: EveryCodeSession,
        thread: discord.Thread,
    ) -> None:
        if session.control_message_id is not None:
            await self.refresh_session_controls(session, thread)
            if session.control_message_id is not None:
                return
        message = await thread.send(
            self.format_waiting_for_direction(session),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.add_message_reactions(message, self.session_control_reactions(session))
        session.control_message_id = message.id

    async def set_message_reaction(self, thread_id: int, message_id: int, reaction: str) -> None:
        channel = self.bot.get_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.DiscordException:
            logger.warning("Unable to fetch Every Code reply message %s", message_id)
            return

        bot_user = self.bot.user
        try:
            await message.add_reaction(reaction)
            if bot_user is not None:
                for existing in TRANSIENT_REACTIONS - {reaction}:
                    with suppress(discord.DiscordException):
                        await message.remove_reaction(existing, bot_user)
        except discord.DiscordException:
            logger.warning("Unable to update Every Code reply reaction %s", message_id)

    async def clear_message_transient_reactions(self, thread_id: int, message_id: int) -> None:
        channel = self.bot.get_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        bot_user = self.bot.user
        if bot_user is None:
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.DiscordException:
            logger.warning("Unable to fetch Every Code reply message %s", message_id)
            return

        for existing in TRANSIENT_REACTIONS:
            with suppress(discord.DiscordException):
                await message.remove_reaction(existing, bot_user)

    async def post_assistant_message(self, thread_id: int, text: str) -> None:
        for message in self.format_assistant_messages(text):
            await self.post_thread_notice(thread_id, message)

    @classmethod
    def format_assistant_messages(cls, text: str) -> list[str]:
        return [f"**Assistant**\n{chunk}" for chunk in cls._split_discord_message(text, DISCORD_ASSISTANT_CHUNK_LIMIT)]

    async def post_session_controls(self, session: EveryCodeSession) -> None:
        if session.thread_id is None:
            return
        channel = self.bot.get_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return
        session.pending_control_confirmation = None
        session.control_status_reaction = None
        session.control_interruptions_enabled = False
        if session.control_message_id is not None:
            replaced = await self.replace_message_reactions(
                channel,
                session.control_message_id,
                self.session_control_reactions(session),
            )
            if replaced:
                return
            session.control_message_id = None

        message = await channel.send(
            self.format_waiting_for_direction(session),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.add_message_reactions(
            message,
            self.session_control_reactions(session),
        )
        session.control_message_id = message.id

    async def clear_pending_user_inputs(
        self,
        session: EveryCodeSession,
        content: str,
    ) -> None:
        if not session.pending_user_inputs:
            return

        pending_items = list(session.pending_user_inputs.values())
        session.pending_user_inputs.clear()
        for pending in pending_items:
            channel = self.bot.get_channel(pending.thread_id)
            if not isinstance(channel, discord.Thread):
                continue
            try:
                message = await channel.fetch_message(pending.message_id)
                await message.edit(
                    content=content[:DISCORD_MESSAGE_LIMIT],
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.DiscordException:
                logger.warning(
                    "Unable to clear Every Code request_user_input message %s",
                    pending.message_id,
                )

    async def clear_session_controls(self, session: EveryCodeSession) -> None:
        if session.thread_id is None or session.control_message_id is None:
            return
        channel = self.bot.get_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return
        try:
            message = await channel.fetch_message(session.control_message_id)
            await message.delete()
        except discord.NotFound:
            session.control_message_id = None
        except discord.DiscordException:
            logger.warning(
                "Unable to clear Every Code control message %s",
                session.control_message_id,
            )
        else:
            session.control_message_id = None
        session.pending_control_confirmation = None
        session.control_status_reaction = None
        session.control_interruptions_enabled = False

    async def delete_session_message(self, thread_id: int, message_id: int) -> None:
        channel = self.bot.get_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        try:
            message = await channel.fetch_message(message_id)
            await self.clear_message_reactions(message)
            await message.delete()
        except discord.NotFound:
            return
        except discord.DiscordException:
            logger.warning("Unable to clear Every Code control message %s", message_id)

    async def spawn_session_controls(
        self,
        session: EveryCodeSession,
        *,
        reaction: str | None,
        interruptions_enabled: bool,
    ) -> bool:
        if session.thread_id is None:
            return False
        channel = self.bot.get_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return False

        old_control_message_id = session.control_message_id
        old_pending_control_confirmation = session.pending_control_confirmation
        old_control_status_reaction = session.control_status_reaction
        old_control_interruptions_enabled = session.control_interruptions_enabled

        session.pending_control_confirmation = None
        session.control_status_reaction = reaction
        session.control_interruptions_enabled = interruptions_enabled

        try:
            message = await channel.send(
                self.format_waiting_for_direction(session),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.add_message_reactions(message, self.session_control_reactions(session))
        except discord.DiscordException:
            session.pending_control_confirmation = old_pending_control_confirmation
            session.control_status_reaction = old_control_status_reaction
            session.control_interruptions_enabled = old_control_interruptions_enabled
            return False

        session.control_message_id = message.id
        if old_control_message_id is not None and old_control_message_id != message.id:
            self.rebind_session_control_commands(session, old_control_message_id, message.id)
            await self.delete_session_message(session.thread_id, old_control_message_id)
        return True

    @staticmethod
    def rebind_session_control_commands(
        session: EveryCodeSession,
        old_message_id: int,
        new_message_id: int,
    ) -> None:
        for command in session.pending_commands.values():
            if command.message_id == old_message_id:
                command.message_id = new_message_id

    async def post_thread_notice(self, thread_id: int, text: str) -> None:
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            await channel.send(
                text[:DISCORD_MESSAGE_LIMIT],
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def close_session_thread(self, session: EveryCodeSession) -> None:
        if session.notification_message_id is not None:
            await self.delete_session_notification(session.notification_message_id)

        if session.thread_id is None:
            return

        thread = await self.get_thread(session.thread_id)
        if thread is None:
            return

        await self.close_thread(thread)

    async def close_thread(self, thread: discord.Thread) -> None:
        if thread.archived:
            try:
                await thread.edit(
                    archived=False,
                    locked=False,
                    reason="Preparing to close Every Code session thread",
                )
            except discord.DiscordException:
                logger.warning("Unable to unarchive Every Code thread %s", thread.id)

        try:
            await thread.send(
                "Every Code session disconnected",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            logger.warning("Unable to post close notice in Every Code thread %s", thread.id)
        await self.remove_thread_members(thread)

        try:
            await thread.edit(
                archived=True,
                locked=True,
                reason="Every Code session disconnected",
            )
        except discord.DiscordException:
            logger.warning("Unable to archive Every Code thread %s", thread.id)

        try:
            await thread.leave()
        except discord.DiscordException:
            logger.warning("Unable to leave Every Code thread %s", thread.id)

    async def delete_session_notification(self, message_id: int) -> None:
        try:
            channel = await get_every_code_channel(self.bot)
            message = await channel.fetch_message(message_id)
            await message.delete()
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to delete Every Code notification message %s", message_id)

    async def get_thread(self, thread_id: int) -> discord.Thread | None:
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except discord.DiscordException:
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def remove_thread_members(self, thread: discord.Thread) -> None:
        bot_user = self.bot.user
        bot_user_id = bot_user.id if bot_user is not None else None
        try:
            members = await thread.fetch_members()
        except discord.DiscordException:
            members = thread.members

        for member in members:
            if member.id == bot_user_id:
                continue
            try:
                await thread.remove_user(discord.Object(id=member.id))
            except discord.DiscordException:
                logger.warning(
                    "Unable to remove user %s from Every Code thread %s",
                    member.id,
                    thread.id,
                )

    def is_operator(self, user: discord.User | discord.Member) -> bool:
        role_name = self.bot.config.every_code.operator_role_name or self.bot.config.discord.employee_role_name
        if not role_name:
            return True
        if not isinstance(user, discord.Member):
            return False
        return any(role.name == role_name for role in user.roles)

    def request_user_input_view(
        self,
        session_id: str,
        request: RemoteRequestUserInput,
    ) -> discord.ui.View:
        return RequestUserInputView(self, session_id, request)

    @staticmethod
    def can_render_request_user_input_as_select(request: RemoteRequestUserInput) -> bool:
        return len(request.questions) == 1 and bool(request.questions[0].options) and len(request.questions[0].options) <= 25

    @staticmethod
    def format_approval_request(approval: RemoteApprovalRequest) -> str:
        command = shlex.join(approval.command) if approval.command else ""
        parts = [
            "**Approval requested**",
            "Quick review: `✅` approve · `✖️` deny",
            "",
            f"```sh\n{command[:1600]}\n```",
        ]
        if approval.cwd:
            parts.append(f"cwd: `{approval.cwd}`")
        if approval.reason:
            parts.extend(["", approval.reason[:500]])
        return "\n".join(parts)[:DISCORD_MESSAGE_LIMIT]

    @staticmethod
    def format_approval_pending(
        decision: Literal["approved", "denied"],
        user: discord.User | discord.Member,
    ) -> str:
        label = "Approval sent" if decision == "approved" else "Denial sent"
        return f"**{label}**\nWaiting for local Every Code to accept the decision.\nby: `{user}`"

    @staticmethod
    def format_approval_finished(decision: str | None, decided_by: int | None) -> str:
        label = "Approved" if decision == "approved" else "Denied"
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
        return f"**{label}**\nWaiting for local Every Code to accept the response.\nby: `{user}`"

    @staticmethod
    def format_user_message_notice(message: str) -> str:
        return f"**You**\n>>> {message.strip()}"

    @staticmethod
    def format_waiting_for_direction(_session: EveryCodeSession) -> str:
        return "\u200b"

    @staticmethod
    def session_control_reactions(session: EveryCodeSession) -> list[str]:
        if session.pending_control_confirmation is not None:
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
                return [
                    session.control_status_reaction,
                    REACTION_CONTROL_PAUSE,
                    REACTION_CONTROL_END,
                ]
            return [session.control_status_reaction]
        return [
            REACTION_CONTROL_CONTINUE,
            REACTION_CONTROL_STATUS,
            REACTION_CONTROL_END,
        ]

    async def refresh_session_controls(
        self,
        session: EveryCodeSession,
        thread: discord.Thread,
        *,
        remove_user_reaction: tuple[str, discord.User | discord.Member] | None = None,
    ) -> None:
        if session.control_message_id is None:
            return
        replaced = await self.replace_message_reactions(
            thread,
            session.control_message_id,
            self.session_control_reactions(session),
            remove_user_reaction=remove_user_reaction,
        )
        if replaced:
            return
        session.control_message_id = None
        message = await thread.send(
            self.format_waiting_for_direction(session),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.add_message_reactions(message, self.session_control_reactions(session))
        session.control_message_id = message.id

    def _authorized(self, request: web.Request) -> bool:
        token = self.bot.config.every_code.token
        if not token:
            return False
        expected = f"Bearer {token}"
        return request.headers.get("Authorization") == expected

    @staticmethod
    async def add_message_reactions(
        message: discord.Message,
        reactions: list[str],
    ) -> None:
        for reaction in reactions:
            try:
                await message.add_reaction(reaction)
            except discord.DiscordException:
                logger.warning("Unable to add Every Code reaction %s to %s", reaction, message.id)

    @staticmethod
    async def clear_message_reactions(message: discord.Message) -> None:
        with suppress(discord.DiscordException):
            await message.clear_reactions()

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
            logger.warning("Unable to remove Every Code reaction %s from %s", reaction, message_id)

    async def replace_message_reactions(
        self,
        thread: discord.Thread,
        message_id: int,
        reactions: list[str],
        *,
        remove_user_reaction: tuple[str, discord.User | discord.Member] | None = None,
    ) -> bool:
        try:
            message = await thread.fetch_message(message_id)
            if remove_user_reaction is not None:
                reaction, user = remove_user_reaction
                with suppress(discord.DiscordException):
                    await message.remove_reaction(reaction, user)
            await self.clear_message_reactions(message)
            await self.add_message_reactions(message, reactions)
            return True
        except discord.DiscordException:
            logger.warning("Unable to replace Every Code reactions on %s", message_id)
            return False

    @staticmethod
    def _split_discord_message(text: str, limit: int) -> list[str]:
        normalized = text.strip()
        if not normalized:
            return []

        plain_limit = max(1, limit - DISCORD_CODE_FENCE_WRAP_RESERVE)
        chunks = EveryCodeBridge._split_discord_message_plain(normalized, plain_limit)
        return EveryCodeBridge._wrap_split_code_fences(chunks)

    @staticmethod
    def _split_discord_message_plain(text: str, limit: int) -> list[str]:
        chunks: list[str] = []
        remaining = text
        while len(remaining) > limit:
            split_at = remaining.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = remaining.rfind(" ", 0, limit)
            if split_at < limit // 2:
                split_at = limit
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        if remaining:
            chunks.append(remaining)
        return chunks

    @staticmethod
    def _wrap_split_code_fences(chunks: list[str]) -> list[str]:
        wrapped: list[str] = []
        fence_state: tuple[str, int, str] | None = None
        for chunk in chunks:
            prefix = EveryCodeBridge._opening_code_fence(fence_state)
            next_fence_state = EveryCodeBridge._scan_code_fence_state(chunk, fence_state)
            suffix = EveryCodeBridge._closing_code_fence(next_fence_state)
            wrapped.append(f"{prefix}{chunk}{suffix}")
            fence_state = next_fence_state
        return wrapped

    @staticmethod
    def _scan_code_fence_state(text: str, state: tuple[str, int, str] | None) -> tuple[str, int, str] | None:
        for line in text.splitlines():
            match = MARKDOWN_CODE_FENCE_RE.match(line.rstrip())
            if match is None:
                continue
            fence = match.group("fence")
            fence_char = fence[0]
            fence_length = len(fence)
            if state is None:
                info = match.group("info").strip()
                state = (fence_char, fence_length, info)
            elif fence_char == state[0] and fence_length >= state[1]:
                state = None
        return state

    @staticmethod
    def _opening_code_fence(state: tuple[str, int, str] | None) -> str:
        if state is None:
            return ""
        fence_char, fence_length, info = state
        return f"{fence_char * fence_length}{info}\n"

    @staticmethod
    def _closing_code_fence(state: tuple[str, int, str] | None) -> str:
        if state is None:
            return ""
        fence_char, fence_length, _info = state
        return f"\n{fence_char * fence_length}"
