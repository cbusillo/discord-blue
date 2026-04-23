from __future__ import annotations

import asyncio
import json
import logging
import shlex
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal

import discord
from aiohttp import WSMsgType, web

from discord_blue.doodads.every_code.protocol import (
    RemoteApprovalDecision,
    RemoteApprovalRequest,
    RemoteCommand,
    SessionHello,
    SessionStatus,
    UserMessage,
)
from discord_blue.doodads.every_code.sessions import (
    EveryCodeSession,
    EveryCodeSessionRegistry,
    PendingRemoteApproval,
    PendingRemoteCommand,
)
from discord_blue.doodads.every_code.threads import create_session_thread
from discord_blue.doodads.every_code.threads import get_every_code_channel
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
DISCORD_ASSISTANT_CHUNK_LIMIT = 1800
SESSION_START_PREFIX = "Every Code session connected"
SESSION_NOTIFICATION_PREFIX = "Every Code session connected for "
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


class ApprovalView(discord.ui.View):
    def __init__(self, bridge: EveryCodeBridge, session_id: str, approval_id: str) -> None:
        super().__init__(timeout=3600)
        self.bridge = bridge
        self.session_id = session_id
        self.approval_id = approval_id

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(
        self,
        interaction: discord.Interaction[BlueBot],
        _button: discord.ui.Button[ApprovalView],
    ) -> None:
        await self.bridge.handle_approval_interaction(
            interaction,
            self.session_id,
            self.approval_id,
            "approved",
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(
        self,
        interaction: discord.Interaction[BlueBot],
        _button: discord.ui.Button[ApprovalView],
    ) -> None:
        await self.bridge.handle_approval_interaction(
            interaction,
            self.session_id,
            self.approval_id,
            "denied",
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
        self._runner = web.AppRunner(app)
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
        await self._runner.cleanup()
        self._runner = None
        self._site = None

    async def close_active_sessions(self) -> None:
        for session_id in list(self.sessions.by_session):
            session = self.sessions.remove(session_id)
            if session is not None:
                await self.close_session_thread(session)

    async def cleanup_stale_sessions(self) -> None:
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
            async for message in channel.history(limit=100):
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

        candidates = list(channel.threads)
        try:
            async for thread in channel.archived_threads(private=True, joined=True, limit=50):
                candidates.append(thread)
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to scan archived Every Code threads")

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
                session_thread = await create_session_thread(self.bot, hello)
                self.sessions.bind_thread(
                    hello.session_id,
                    session_thread.thread.id,
                    session_thread.notification_message_id,
                )
                await websocket.send_json(
                    {"type": "hello_ack", "thread_id": session_thread.thread.id}
                )
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
            removed = self.sessions.remove(session.session_id)
            if removed is not None:
                await self.close_session_thread(removed)

        return websocket

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
        )
        await self.set_message_reaction(message.channel.id, message.id, REACTION_QUEUED)
        await session.websocket.send_json(command.to_message())
        return True

    async def handle_command_ack(self, payload: dict[str, object]) -> None:
        command_id = str(payload.get("command_id") or "")
        session_id = str(payload.get("session_id") or "")
        if not command_id or not session_id:
            return
        session = self.sessions.get(session_id)
        if session is None:
            return
        command = session.pending_commands.get(command_id)
        if command is not None:
            session.active_command_id = command_id
            await self.set_message_reaction(command.thread_id, command.message_id, REACTION_DELIVERED)

    async def handle_command_reject(self, payload: dict[str, object]) -> None:
        command_id = str(payload.get("command_id") or "")
        session_id = str(payload.get("session_id") or "")
        if not command_id or not session_id:
            return
        session = self.sessions.get(session_id)
        if session is None:
            return
        command = session.pending_commands.pop(command_id, None)
        if session.active_command_id == command_id:
            session.active_command_id = None
        if command is not None:
            await self.set_message_reaction(command.thread_id, command.message_id, REACTION_REJECTED)

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

        view = ApprovalView(self, approval.session_id, approval.approval_id)
        message = await channel.send(
            self.format_approval_request(approval),
            allowed_mentions=discord.AllowedMentions.none(),
            view=view,
        )
        session.pending_approvals[approval.approval_id] = PendingRemoteApproval(
            thread_id=session.thread_id,
            message_id=message.id,
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
            view=self.disabled_approval_view(session_id, approval_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )

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

        if message_type == "status_changed":
            status_message = (status.message or "").lower()
            if status_message == "turn aborted":
                reaction = REACTION_REJECTED
            elif "compact" in status_message:
                reaction = REACTION_COMPACTING
            else:
                reaction = REACTION_IN_PROGRESS
            await self.update_active_command_reaction(session, reaction)
            return

        if message_type == "error":
            await self.update_active_command_reaction(session, REACTION_REJECTED)
            return

        if message_type == "turn_complete" and status.assistant_message:
            await self.post_assistant_message(session.thread_id, status.assistant_message)
        if message_type == "turn_complete":
            await self.update_active_command_reaction(session, REACTION_FINISHED, clear=True)

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
        await self.post_thread_notice(session.thread_id, f"**You**\n{user_message.message}")

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
        await self.set_message_reaction(command.thread_id, command.message_id, reaction)
        if clear:
            session.pending_commands.pop(command_id, None)
            session.active_command_id = None

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
                for existing in STATUS_REACTIONS - {reaction}:
                    with suppress(discord.DiscordException):
                        await message.remove_reaction(existing, bot_user)
        except discord.DiscordException:
            logger.warning("Unable to update Every Code reply reaction %s", message_id)

    async def post_assistant_message(self, thread_id: int, text: str) -> None:
        for chunk in self._split_discord_message(text, DISCORD_ASSISTANT_CHUNK_LIMIT):
            await self.post_thread_notice(thread_id, f"**Assistant**\n{chunk}")

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
        role_name = (
            self.bot.config.every_code.operator_role_name
            or self.bot.config.discord.employee_role_name
        )
        if not role_name:
            return True
        if not isinstance(user, discord.Member):
            return False
        return any(role.name == role_name for role in user.roles)

    def disabled_approval_view(self, session_id: str, approval_id: str) -> ApprovalView:
        view = ApprovalView(self, session_id, approval_id)
        for child in view.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        return view

    @staticmethod
    def format_approval_request(approval: RemoteApprovalRequest) -> str:
        command = shlex.join(approval.command) if approval.command else ""
        parts = [
            "**Approval requested**",
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

    def _authorized(self, request: web.Request) -> bool:
        token = self.bot.config.every_code.token
        if not token:
            return False
        expected = f"Bearer {token}"
        return request.headers.get("Authorization") == expected

    @staticmethod
    def _split_discord_message(text: str, limit: int) -> list[str]:
        normalized = text.strip()
        if not normalized:
            return []

        chunks: list[str] = []
        remaining = normalized
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
