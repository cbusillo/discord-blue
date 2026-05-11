from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import TYPE_CHECKING

import discord

from discord_blue.doodads.every_code.protocol import SessionHello

if TYPE_CHECKING:
    from discord_blue.config import Config


class FakeWebSocket:
    def __init__(self, *, closed: bool = False) -> None:
        self.closed = closed
        self.sent_json: list[dict[str, object]] = []
        self.close_messages: list[bytes] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent_json.append(payload)

    async def close(self, *, message: bytes = b"", drain: bool = True) -> bool:
        self.close_messages.append(message)
        self.closed = True
        return True


class FakeReplyMessage:
    def __init__(
        self,
        message_id: int,
        channel: FakeThread | FakeTextChannel,
        content: str = "",
        *,
        author_id: int = 123,
    ) -> None:
        self.id = message_id
        self.channel = channel
        self.content = content
        self.author = SimpleNamespace(id=author_id)
        self.reactions: list[str] = []
        self.replies: list[str] = []
        self.reply_mentions: list[bool] = []
        self.edits: list[tuple[str, bool]] = []
        self.edit_kwargs: list[dict[str, object]] = []
        self.deleted = False
        self.delete_raises = False

    async def add_reaction(self, reaction: str) -> None:
        self.reactions.append(reaction)

    async def remove_reaction(self, reaction: str, _user: object) -> None:
        if reaction in self.reactions:
            self.reactions.remove(reaction)

    async def clear_reactions(self) -> None:
        self.reactions.clear()

    async def reply(self, content: str, *, mention_author: bool) -> None:
        self.replies.append(content)
        self.reply_mentions.append(mention_author)

    async def edit(self, content: str, **kwargs: object) -> None:
        self.content = content
        self.edits.append((content, kwargs.get("view") is None))
        self.edit_kwargs.append(kwargs)

    async def delete(self) -> None:
        if self.delete_raises:
            raise discord.Forbidden(
                response=SimpleNamespace(status=403, reason="Forbidden"),
                message=f"Cannot delete {self.id}",
            )
        self.deleted = True
        self.channel.delete_message(self.id)


class FakeThread:
    def __init__(
        self,
        thread_id: int,
        *,
        archived: bool = False,
        locked: bool = False,
        manage_messages: bool = True,
    ) -> None:
        self.id = thread_id
        self.archived = archived
        self.locked = locked
        self.guild = SimpleNamespace(me=SimpleNamespace(id=999))
        self._manage_messages = manage_messages
        self._messages: dict[int, FakeReplyMessage] = {}
        self._history: list[FakeReplyMessage] = []
        self.sent_messages: list[str] = []
        self.sent_views: list[object] = []
        self.sent_kwargs: list[dict[str, object]] = []
        self.send_raises = False
        self.send_failures_remaining = 0
        self.edits: list[dict[str, object]] = []

    def add_message(self, message: FakeReplyMessage) -> None:
        self._messages[message.id] = message
        self._history.append(message)

    def permissions_for(self, _member: object) -> object:
        return SimpleNamespace(manage_messages=self._manage_messages)

    def delete_message(self, message_id: int) -> None:
        message = self._messages.pop(message_id, None)
        if message is None:
            return
        message.deleted = True
        self._history = [stored for stored in self._history if stored.id != message_id]

    async def fetch_message(self, message_id: int) -> FakeReplyMessage:
        message = self._messages.get(message_id)
        if message is None:
            raise discord.NotFound(
                response=SimpleNamespace(status=404, reason="Not Found"),
                message=f"Message {message_id} not found",
            )
        return message

    async def history(
        self,
        limit: int,
        oldest_first: bool = False,
    ) -> AsyncIterator[FakeReplyMessage]:
        messages = self._history[:limit] if oldest_first else list(reversed(self._history))[:limit]
        for message in messages:
            yield message

    async def send(self, content: str | None = None, **kwargs: object) -> FakeReplyMessage:
        if self.send_raises or self.send_failures_remaining > 0:
            if self.send_failures_remaining > 0:
                self.send_failures_remaining -= 1
            raise discord.Forbidden(
                response=SimpleNamespace(status=403, reason="Forbidden"),
                message=f"Cannot send to {self.id}",
            )
        stored_content = content or ""
        self.sent_messages.append(stored_content)
        self.sent_views.append(kwargs.get("view"))
        self.sent_kwargs.append(kwargs)
        message = FakeReplyMessage(
            900 + len(self.sent_messages),
            self,
            stored_content,
            author_id=999,
        )
        self.add_message(message)
        return message

    async def edit(self, **kwargs: object) -> None:
        if "archived" in kwargs:
            self.archived = bool(kwargs["archived"])
        if "locked" in kwargs:
            self.locked = bool(kwargs["locked"])
        self.edits.append(kwargs)


class FakeTextChannel:
    def __init__(self, channel_id: int, threads: list[FakeThread], *, manage_messages: bool = True) -> None:
        self.id = channel_id
        self.guild = SimpleNamespace(me=SimpleNamespace(id=999))
        self._manage_messages = manage_messages
        self.threads = [thread for thread in threads if not thread.archived]
        self._archived_threads = [thread for thread in threads if thread.archived]
        self._messages: dict[int, FakeReplyMessage] = {}
        self._history: list[FakeReplyMessage] = []
        self.sent_messages: list[str] = []
        self.sent_kwargs: list[dict[str, object]] = []

    def add_message(self, message: FakeReplyMessage) -> None:
        self._messages[message.id] = message
        self._history.append(message)

    def delete_message(self, message_id: int) -> None:
        message = self._messages.pop(message_id, None)
        if message is None:
            return
        message.deleted = True
        self._history = [stored for stored in self._history if stored.id != message_id]

    async def history(
        self,
        limit: int | None = None,
        oldest_first: bool = False,
    ) -> AsyncIterator[FakeReplyMessage]:
        messages = list(self._history)
        if not oldest_first:
            messages.reverse()
        if limit is not None:
            messages = messages[:limit]
        for message in messages:
            yield message

    async def archived_threads(self, **_: object) -> AsyncIterator[FakeThread]:
        for thread in self._archived_threads:
            yield thread

    def permissions_for(self, _member: object) -> object:
        return SimpleNamespace(manage_messages=self._manage_messages)

    async def create_thread(self, **kwargs: object) -> FakeThread:
        thread = FakeThread(9000 + len(self.threads), manage_messages=self._manage_messages)
        self.threads.append(thread)
        return thread

    async def send(self, content: str | None = None, **kwargs: object) -> FakeReplyMessage:
        stored_content = content or ""
        self.sent_messages.append(stored_content)
        self.sent_kwargs.append(kwargs)
        message = FakeReplyMessage(800 + len(self.sent_messages), self, stored_content, author_id=999)
        self.add_message(message)
        return message


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.edits: list[tuple[str, bool]] = []
        self.edit_kwargs: list[dict[str, object]] = []
        self.modals: list[object] = []

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.messages.append((content, ephemeral))

    async def edit_message(self, content: str, **kwargs: object) -> None:
        self.edits.append((content, kwargs.get("view") is None))
        self.edit_kwargs.append(kwargs)

    async def send_modal(self, modal: object) -> None:
        self.modals.append(modal)


class FakeInteraction:
    def __init__(
        self,
        channel: FakeThread,
        user_id: int = 123,
        message: FakeReplyMessage | None = None,
    ) -> None:
        self.channel = channel
        self.user = SimpleNamespace(id=user_id)
        self.message = message
        self.response = FakeInteractionResponse()


class FakeBot:
    def __init__(
        self,
        config: Config,
        thread: FakeThread | None = None,
        channel: FakeTextChannel | None = None,
    ) -> None:
        self.config = config
        self.user = SimpleNamespace(id=999)
        self._thread = thread
        self._channel = channel

    def get_channel(self, channel_id: int) -> FakeThread | FakeTextChannel | None:
        if self._thread is not None and self._thread.id == channel_id:
            return self._thread
        if self._channel is not None and self._channel.id == channel_id:
            return self._channel
        return None


def make_hello() -> SessionHello:
    return SessionHello(
        session_id="session-1",
        session_epoch="epoch-1",
        host_label="Mac Studio",
        cwd="/tmp/project",
        branch="main",
        pid=42,
    )


def add_bot_message(channel: FakeThread | FakeTextChannel, message_id: int, content: str) -> FakeReplyMessage:
    message = FakeReplyMessage(message_id, channel, content, author_id=999)
    channel.add_message(message)
    return message
