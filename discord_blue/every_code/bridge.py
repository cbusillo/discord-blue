from __future__ import annotations

import json
import logging
import uuid

import discord
from aiohttp import WSMsgType, web

from discord_blue.every_code.protocol import RemoteCommand, SessionHello, SessionStatus
from discord_blue.every_code.sessions import EveryCodeSession, EveryCodeSessionRegistry
from discord_blue.every_code.threads import create_session_thread
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)


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
            elif message_type == "command_reject":
                logger.warning("Every Code command reject: %s", payload)

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
        await session.websocket.send_json(command.to_message())
        return True

    async def handle_session_status(self, message_type: str, status: SessionStatus) -> None:
        session = self.sessions.get(status.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Every Code status for unknown session: %s", status.session_id)
            return
        if status.session_epoch != session.session_epoch:
            logger.warning("Every Code status for stale session epoch: %s", status.session_id)
            return

        text = status.message or self._default_status_message(message_type)
        await self.post_thread_notice(session.thread_id, text)

    async def post_thread_notice(self, thread_id: int, text: str) -> None:
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            await channel.send(text)

    def _authorized(self, request: web.Request) -> bool:
        token = self.bot.config.every_code.token
        if not token:
            return False
        expected = f"Bearer {token}"
        return request.headers.get("Authorization") == expected

    @staticmethod
    def _default_status_message(message_type: str) -> str:
        if message_type == "turn_complete":
            return "Turn complete. Replies here will start the next turn."
        if message_type == "error":
            return "Every Code reported an error."
        return "Every Code status changed."
