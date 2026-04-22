from __future__ import annotations

import json
import logging
import uuid

import discord
from aiohttp import WSMsgType, web

from discord_blue.every_code.protocol import RemoteCommand, SessionHello, SessionStatus
from discord_blue.every_code.sessions import (
    EveryCodeSession,
    EveryCodeSessionRegistry,
    PendingRemoteCommand,
)
from discord_blue.every_code.threads import create_session_thread
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
DISCORD_ASSISTANT_CHUNK_LIMIT = 1800
REACTION_QUEUED = "⏳"
REACTION_DELIVERED = "📬"
REACTION_IN_PROGRESS = "🔄"
REACTION_FINISHED = "✅"
REACTION_REJECTED = "❌"
STATUS_REACTIONS = {
    REACTION_QUEUED,
    REACTION_DELIVERED,
    REACTION_IN_PROGRESS,
    REACTION_FINISHED,
    REACTION_REJECTED,
}


class EveryCodeBridge:
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        self.sessions = EveryCodeSessionRegistry()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

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

    async def stop(self) -> None:
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None
        self._site = None

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
                thread = await create_session_thread(self.bot, hello)
                self.sessions.bind_thread(hello.session_id, thread.id)
                await websocket.send_json({"type": "hello_ack", "thread_id": thread.id})
            elif message_type == "heartbeat" and session is not None:
                session.touch()
            elif message_type in {"status_changed", "turn_complete", "error"}:
                status = SessionStatus.from_payload(payload)
                await self.handle_session_status(message_type, status)
            elif message_type == "command_ack":
                logger.info("Every Code command ack: %s", payload.get("command_id"))
                await self.handle_command_ack(payload)
            elif message_type == "command_reject":
                logger.warning("Every Code command reject: %s", payload)
                await self.handle_command_reject(payload)

        if session is not None:
            removed = self.sessions.remove(session.session_id)
            if removed and removed.thread_id is not None:
                await self.post_thread_notice(removed.thread_id, "Every Code session disconnected")

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

    async def handle_session_status(self, message_type: str, status: SessionStatus) -> None:
        session = self.sessions.get(status.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Every Code status for unknown session: %s", status.session_id)
            return
        if status.session_epoch != session.session_epoch:
            logger.warning("Every Code status for stale session epoch: %s", status.session_id)
            return

        if message_type == "status_changed":
            reaction = (
                REACTION_REJECTED
                if (status.message or "").lower() == "turn aborted"
                else REACTION_IN_PROGRESS
            )
            await self.update_active_command_reaction(session, reaction)
            return

        if message_type == "error":
            await self.update_active_command_reaction(session, REACTION_REJECTED)
            return

        if message_type == "turn_complete" and status.assistant_message:
            await self.post_assistant_message(session.thread_id, status.assistant_message)
        if message_type == "turn_complete":
            await self.update_active_command_reaction(session, REACTION_FINISHED, clear=True)

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
            if bot_user is not None:
                for existing in STATUS_REACTIONS - {reaction}:
                    try:
                        await message.remove_reaction(existing, bot_user)
                    except discord.DiscordException:
                        pass
            await message.add_reaction(reaction)
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
