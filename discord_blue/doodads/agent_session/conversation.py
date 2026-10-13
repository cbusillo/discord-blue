"""External channel input on the existing transport, separate from session controls."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord
from aiohttp import ClientError, WSMsgType, web

from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT, split_discord_message

if TYPE_CHECKING:
    from discord_blue.plugs.discord_plug import BlueBot

FEATURE = "external_conversation"
INBOX_LIMIT = 32
MEMORY_LIMIT = 1024
REPLY_LIMIT = 16000


@dataclass
class ConversationConnection:
    session_id: str
    epoch: str
    binding: tuple[int, int, tuple[int, ...]]
    websocket: web.WebSocketResponse
    pending: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    replies: OrderedDict[str, tuple[str, int]] = field(default_factory=OrderedDict)
    reply_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConversationRouter:
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        self.connection: ConversationConnection | None = None
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.observed: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self.connection_lock = asyncio.Lock()

    def binding(self) -> tuple[int, int, tuple[int, ...]] | None:
        config = self.bot.config.conversation
        config.validate()
        if (
            not config.enabled
            or not self.bot.config.agent_session.enabled
            or not self.bot.config.discord.guild_id
            or config.guild_id == self.bot.config.discord.guild_id
        ):
            return None
        return config.guild_id, config.channel_id, tuple(config.allowed_user_ids)

    def channel(self) -> discord.TextChannel | None:
        binding = self.binding()
        if binding is None:
            return None
        channel = self.bot.get_channel(binding[1])
        if isinstance(channel, discord.TextChannel) and channel.guild.id == binding[0] and channel.name == "odoo-with-chris":
            return channel
        return None

    async def receive_message(self, message: discord.Message, *, amendment: bool = False) -> bool:
        channel = self.channel()
        binding = self.binding()
        if (
            channel is None
            or binding is None
            or message.guild is None
            or message.guild.id != binding[0]
            or message.channel.id != channel.id
            or message.author.id not in binding[2]
            or message.author.bot
            or message.webhook_id is not None
        ):
            return False
        if amendment and message.edited_at is None:
            return False
        revision = message.edited_at.isoformat() if amendment and message.edited_at else "new"
        command_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{message.jump_url}:{revision}").hex
        if command_id in self.seen:
            return True
        envelope = {
            "source": "discord",
            "trust": "untrusted_external_input",
            "authority": [],
            "scope": "cell_mechanic",
            "allowed": True,
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "message_id": message.id,
            "message_url": message.jump_url,
            "author_id": message.author.id,
            "author_name": message.author.display_name,
            "created_at": message.created_at.isoformat(),
            "edited_at": message.edited_at.isoformat() if message.edited_at else None,
            "event": "amendment" if amendment else "message",
            "text": message.content,
        }
        self.observed[message.id] = envelope
        while len(self.observed) > MEMORY_LIMIT:
            self.observed.popitem(last=False)
        return await self.queue_envelope(command_id, envelope)

    async def receive_delete(self, payload: discord.RawMessageDeleteEvent) -> bool:
        envelope = self.observed.get(payload.message_id)
        binding = self.binding()
        if (
            envelope is None
            or binding is None
            or payload.guild_id != binding[0]
            or payload.channel_id != binding[1]
            or envelope["author_id"] not in binding[2]
        ):
            return False
        return await self.queue_envelope(f"delete-{payload.message_id}", {**envelope, "event": "deletion", "text": ""})

    async def queue_envelope(self, command_id: str, envelope: dict[str, Any]) -> bool:
        channel, binding, connection = self.channel(), self.binding(), self.connection
        if channel is None:
            return False
        if command_id in self.seen:
            return True
        if (
            connection is None
            or connection.websocket.closed
            or connection.binding != binding
            or connection.session_id != self.bot.config.conversation.session_id
        ):
            await channel.send(
                "The CM conversation is offline. Please resend when it is connected.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        if len(connection.pending) >= INBOX_LIMIT:
            await channel.send(
                "The CM conversation inbox is full. Please try again shortly.", allowed_mentions=discord.AllowedMentions.none()
            )
            return False
        payload = {
            "type": "command",
            "kind": "conversation_input",
            "command_id": command_id,
            "session_id": connection.session_id,
            "session_epoch": connection.epoch,
            "envelope": envelope,
        }
        connection.pending[command_id] = payload
        self.seen[command_id] = None
        while len(self.seen) > MEMORY_LIMIT:
            self.seen.popitem(last=False)
        try:
            await connection.websocket.send_json(payload)
        except ConnectionError:
            # Retained only for the same epoch; never offered to a fresh conversation.
            pass
        return True

    async def serve(self, websocket: web.WebSocketResponse, hello: dict[str, Any]) -> None:
        binding = self.binding()
        config = self.bot.config.conversation
        epoch = hello.get("session_epoch")
        if (
            binding is None
            or self.channel() is None
            or hello.get("session_id") != config.session_id
            or hello.get("conversation") != "cell_mechanic"
            or hello.get("mirror") is not False
            or hello.get("capabilities") != ["conversation_input"]
            or not isinstance(epoch, str)
            or not epoch
            or len(epoch) > 128
        ):
            await websocket.close(message=b"invalid conversation binding", drain=False)
            return
        async with self.connection_lock:
            previous = self.connection
            # No input may precede the new socket's hello_ack, including during an awaited old-socket close.
            self.connection = None
            if previous and previous.session_id == config.session_id and previous.epoch == epoch and previous.binding == binding:
                connection = previous
                old_socket = previous.websocket
                connection.websocket = websocket
                if not old_socket.closed:
                    await old_socket.close(message=b"replaced connection", drain=False)
            else:
                if previous and not previous.websocket.closed:
                    await previous.websocket.close(message=b"conversation changed", drain=False)
                connection = ConversationConnection(config.session_id, epoch, binding, websocket)
            await websocket.send_json({"type": "hello_ack", "features": [FEATURE], "guild_id": binding[0], "channel_id": binding[1]})
            self.connection = connection
            for payload in list(connection.pending.values()):
                await websocket.send_json(payload)
        while not websocket.closed:
            try:
                async with asyncio.timeout(self.bot.config.agent_session.heartbeat_timeout_seconds):
                    frame = await websocket.receive()
            except TimeoutError:
                await websocket.close(message=b"conversation heartbeat expired", drain=False)
                break
            if frame.type != WSMsgType.TEXT:
                break
            try:
                payload = json.loads(frame.data)
            except ValueError:
                continue
            if (
                not isinstance(payload, dict)
                or self.connection is not connection
                or connection.websocket is not websocket
                or self.binding() != connection.binding
                or config.session_id != connection.session_id
                or payload.get("session_id") != connection.session_id
                or payload.get("session_epoch") != connection.epoch
            ):
                continue
            kind = payload.get("type")
            command_id = payload.get("command_id")
            if kind == "command_ack":
                if isinstance(command_id, str):
                    connection.pending.pop(command_id, None)
            elif kind == "command_reject":
                if (
                    isinstance(command_id, str)
                    and connection.pending.pop(command_id, None) is not None
                    and (channel := self.channel())
                ):
                    await channel.send(
                        "The CM conversation could not take that message. Please resend when it is idle.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            elif kind == "conversation_reply":
                await self.reply(connection, payload)
            elif kind == "session_end":
                connection.pending.clear()
                await websocket.close(drain=False)
            # Hooks, status, final answers, title, controls and approval events are never mirrored.

    async def reply(self, connection: ConversationConnection, payload: dict[str, Any]) -> None:
        reply_id, text = payload.get("reply_id"), payload.get("text")
        channel = self.channel()
        if not isinstance(reply_id, str) or not reply_id or len(reply_id) > 128:
            return
        response: dict[str, Any] = {
            "type": "conversation_reply_ack",
            "reply_id": reply_id,
            "session_id": connection.session_id,
            "session_epoch": connection.epoch,
        }
        if channel is None or not isinstance(text, str) or not text.strip() or len(text) > REPLY_LIMIT:
            response["error"] = "Reply needs a connected CM channel and bounded, nonempty text."
        else:
            async with connection.reply_lock:
                previous_text, sent = connection.replies.get(reply_id, (text, 0))
                if previous_text != text:
                    response["error"] = "Reply ID already belongs to different text."
                else:
                    chunks = split_discord_message(text, DISCORD_MESSAGE_LIMIT)
                    try:
                        for chunk in chunks[sent:]:
                            if (
                                self.connection is not connection
                                or self.binding() != connection.binding
                                or connection.session_id != self.bot.config.conversation.session_id
                            ):
                                response["error"] = "Conversation binding changed; unsent reply chunks were discarded."
                                break
                            nonce = uuid.uuid5(
                                uuid.NAMESPACE_URL, f"{channel.id}:{connection.session_id}:{connection.epoch}:{reply_id}:{sent}"
                            ).hex[:24]
                            await channel.send(
                                chunk, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True, nonce=nonce
                            )
                            sent += 1
                            connection.replies[reply_id] = (text, sent)
                        while len(connection.replies) > MEMORY_LIMIT:
                            connection.replies.popitem(last=False)
                    except (discord.DiscordException, ClientError, OSError):
                        response["error"] = "Discord could not confirm the complete reply. Retry the same reply ID."
        await connection.websocket.send_json(response)

    async def stop(self) -> None:
        if self.connection and not self.connection.websocket.closed:
            await self.connection.websocket.close(message=b"bridge shutdown", drain=False)
