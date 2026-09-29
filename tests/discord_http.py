"""The agent-session bridge's Discord objects, backed by the real discord.py HTTP client.

`HttpBot`, `HttpChannel` and `HttpThread` subclass the in-memory fakes, so the
bridge's type checks still pass, but every Discord operation is a real
`discord.http.HTTPClient` request against `FakeDiscord`. That puts discord.py's
own rate-limit buckets, 429 sleeps and 5xx retries between the bridge and the
fake server. `get_channel` mirrors discord.py's cache: a thread is cached while a
gateway event says it is open, and removed when an event says it is archived.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import discord
from aiohttp.test_utils import TestServer
from discord.http import HTTPClient, Route, handle_message_parameters

from discord_blue.plugs.discord_plug import MAX_RATELIMIT_SLEEP_SECONDS
from tests.fake_discord import BOT_ID, GUILD_ID, PARENT_ID, FakeDiscord, FakeThreadState
from tests.fakes_agent_session import FakeBot, FakeReplyMessage, FakeTextChannel, FakeThread, UserLike

Json = dict[str, Any]
PAGE = 100


def as_json(value: object) -> Json:
    return cast(Json, value)


class HttpMessage(FakeReplyMessage):
    def __init__(self, channel: HttpThread | HttpChannel, data: Json) -> None:
        super().__init__(int(data["id"]), channel, str(data.get("content") or ""), author_id=int(data["author"]["id"]))
        self.http = channel.http

    async def delete(self) -> None:
        await self.http.delete_message(self.channel.id, self.id)
        self.deleted = True


class HttpThread(FakeThread):
    def __init__(self, bot: HttpBot, data: Json) -> None:
        metadata = data.get("thread_metadata") or {}
        super().__init__(
            int(data["id"]),
            archived=bool(metadata.get("archived")),
            locked=bool(metadata.get("locked")),
            private=data.get("type") == 12,
        )
        self.bot, self.http = bot, bot.http
        self.name = str(data.get("name"))
        self.parent = bot.parent
        self.parent_id = int(data.get("parent_id") or PARENT_ID)
        self.owner_id = int(data["owner_id"]) if data.get("owner_id") else None
        self.message_count = int(data.get("message_count") or 0)
        self.guild = bot.guild  # type: ignore[assignment]

    def refresh(self, data: Json) -> None:
        metadata = data.get("thread_metadata") or {}
        self.archived, self.locked = bool(metadata.get("archived")), bool(metadata.get("locked"))
        self.name = str(data.get("name"))

    async def edit(self, **kwargs: object) -> None:
        options = {key: kwargs[key] for key in ("archived", "locked", "name") if key in kwargs}
        reason = kwargs.get("reason")
        self.edits.append(kwargs)
        self.refresh(as_json(await self.http.edit_channel(self.id, reason=reason if isinstance(reason, str) else None, **options)))

    async def delete(self, *, reason: str | None = None) -> None:
        await self.http.delete_channel(self.id, reason=reason)

    async def join(self) -> None:
        await self.http.join_thread(self.id)
        self.joined, self.left = True, False

    async def leave(self) -> None:
        await self.http.leave_thread(self.id)
        self.left, self.joined = True, False

    async def add_user(self, user: UserLike) -> None:
        await self.http.add_user_to_thread(self.id, user.id)

    async def remove_user(self, user: UserLike) -> None:
        await self.http.remove_user_from_thread(self.id, user.id)
        self.removed_user_ids.append(user.id)

    async def fetch_members(self) -> list[object]:
        return [SimpleNamespace(id=int(m["user_id"])) for m in await self.http.get_thread_members(self.id)]

    async def send(self, content: str | None = None, **kwargs: object) -> FakeReplyMessage:
        return await send(self, content)

    async def fetch_message(self, message_id: int) -> FakeReplyMessage:
        return HttpMessage(self, as_json(await self.http.get_message(self.id, message_id)))

    async def history(self, limit: int | None = None, oldest_first: bool = False) -> AsyncIterator[FakeReplyMessage]:
        async for message in history(self, limit, oldest_first):
            yield message


class HttpChannel(FakeTextChannel):
    def __init__(self, bot: HttpBot) -> None:
        super().__init__(PARENT_ID, [])
        self.bot, self.http = bot, bot.http

    @property
    def threads(self) -> list[FakeThread]:
        return list(self.bot.cache.values())

    async def archived_threads(self, **kwargs: object) -> AsyncIterator[FakeThread]:
        self.archived_thread_calls.append(kwargs)
        private, joined = bool(kwargs.get("private")), bool(kwargs.get("joined"))
        limit = kwargs.get("limit")
        fetch = self.http.get_public_archived_threads
        if private:
            fetch = self.http.get_joined_private_archived_threads if joined else self.http.get_private_archived_threads
        before, yielded = None, 0
        while True:
            page = as_json(await fetch(self.id, before=before, limit=50))
            for data in page["threads"]:
                yield HttpThread(self.bot, data)
                yielded += 1
                if isinstance(limit, int) and yielded >= limit:
                    return
            if not page.get("has_more") or not page["threads"]:
                return
            before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]

    async def create_thread(self, **kwargs: object) -> FakeThread:
        name = str(kwargs.get("name") or "thread")
        data = await self.http.start_thread_without_message(self.id, name=name, auto_archive_duration=1440, type=12)
        return HttpThread(self.bot, as_json(data))

    async def send(self, content: str | None = None, **kwargs: object) -> FakeReplyMessage:
        return await send(self, content)

    async def fetch_message(self, message_id: int) -> FakeReplyMessage:
        return HttpMessage(self, as_json(await self.http.get_message(self.id, message_id)))

    async def history(self, limit: int | None = None, oldest_first: bool = False) -> AsyncIterator[FakeReplyMessage]:
        async for message in history(self, limit, oldest_first):
            yield message


async def send(channel: HttpThread | HttpChannel, content: str | None) -> FakeReplyMessage:
    with handle_message_parameters(content=content) as params:
        message = HttpMessage(channel, as_json(await channel.http.send_message(channel.id, params=params)))
    channel.sent_messages.append(content or "")
    return message


async def history(channel: HttpThread | HttpChannel, limit: int | None, oldest_first: bool) -> AsyncIterator[FakeReplyMessage]:
    """Page through a channel's messages the way discord.py's history() does."""
    remaining, cursor = limit, None
    while remaining is None or remaining > 0:
        size = min(PAGE, remaining) if remaining is not None else PAGE
        if oldest_first:
            page = list(reversed(cast(list[Json], await channel.http.logs_from(channel.id, size, after=cursor or 0))))
        else:
            page = cast(list[Json], await channel.http.logs_from(channel.id, size, before=cursor))
        for data in page:
            yield HttpMessage(channel, data)
        if len(page) < size:
            return
        cursor = page[-1]["id"]
        remaining = None if remaining is None else remaining - len(page)


class HttpBot(FakeBot):
    def __init__(self, config: object, http: HTTPClient, fake: FakeDiscord) -> None:
        super().__init__(config)  # type: ignore[arg-type]
        self.http = http
        self.guild = SimpleNamespace(id=GUILD_ID, me=SimpleNamespace(id=BOT_ID), active_threads=self.active_threads)
        self.parent = HttpChannel(self)
        self.cache: dict[int, HttpThread] = {}
        for state in fake.threads.values():
            if not state.archived:
                self.cache[state.id] = HttpThread(self, state.payload())
        fake.listeners.append(self.on_gateway)

    def on_gateway(self, state: FakeThreadState, deleted: bool) -> None:
        """discord.py drops a thread from its cache when it is archived and re-adds it when it opens again."""
        if deleted or state.archived:
            self.cache.pop(state.id, None)
        elif (cached := self.cache.get(state.id)) is not None:
            cached.refresh(state.payload())
        else:
            self.cache[state.id] = HttpThread(self, state.payload())

    def get_channel(self, channel_id: int) -> HttpThread | HttpChannel | None:  # type: ignore[override]
        return self.parent if channel_id == PARENT_ID else self.cache.get(channel_id)

    async def active_threads(self) -> list[HttpThread]:
        """Guild.active_threads(): every open thread, straight from REST rather than the gateway cache."""
        data = as_json(await self.http.get_active_threads(GUILD_ID))
        return [HttpThread(self, thread) for thread in data["threads"]]

    async def fetch_thread(self, thread_id: int) -> HttpThread:
        return HttpThread(self, as_json(await self.http.get_channel(thread_id)))

    async def fetch_channel(self, channel_id: int) -> HttpThread | HttpChannel:  # type: ignore[override]
        self.fetch_channel_calls.append(channel_id)
        if channel_id == PARENT_ID:
            return self.parent
        return HttpThread(self, as_json(await self.http.get_channel(channel_id)))  # Not cached, as in discord.py.


@contextmanager
def scaled_discord_sleeps(factor: float) -> Iterator[None]:
    """Scale the sleeps inside discord.py's HTTP client (429 waits, 5xx backoff) so scenarios run quickly."""
    real_sleep = asyncio.sleep

    async def scaled(delay: float, result: object = None) -> object:
        return await real_sleep(delay * factor, result)

    shim = types.SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio) if not name.startswith("__")})
    shim.sleep = scaled
    with patch("discord.http.asyncio", shim):
        yield


@asynccontextmanager
async def discord_bot(fake: FakeDiscord, config: object) -> AsyncIterator[HttpBot]:
    """An HttpBot whose real discord.py HTTP client talks to `fake`."""
    async with TestServer(fake.app(), host="127.0.0.1") as server:
        with patch.object(Route, "BASE", f"http://127.0.0.1:{server.port}/api/v10"):
            http = HTTPClient(asyncio.get_running_loop(), max_ratelimit_timeout=MAX_RATELIMIT_SLEEP_SECONDS)
            await http.static_login("fake-token")
            try:
                yield HttpBot(config, http, fake)
            finally:
                await http.close()


__all__ = ["HttpBot", "HttpChannel", "HttpThread", "discord", "discord_bot", "scaled_discord_sleeps"]
