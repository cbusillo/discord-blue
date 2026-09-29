"""One shared index of the parent channel's session threads, for every attach.

Finding a session's thread means listing the channel's threads (active, plus archived public and private, every page)
and reading each candidate's opening messages for its session marker. Doing that per hello cost about 30 s each and
serialized a restart wave. The index lists once and reads each thread's opening messages once, then serves every
session from memory; a refresh relists (cheap) and reads only threads it has not read yet.

A failed or rate-limited listing page or read is never taken for "not this session": it leaves the index incomplete,
and an attach may create a new thread only when a complete index has no match (#148, M6).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import discord

logger = logging.getLogger(__name__)

# A refresh this recent is reused, so a restart wave's attaches share one scan.
INDEX_REUSE_SECONDS = 5.0
# Opening messages read per candidate; the session marker is the bot's first message in its thread.
OPENING_MESSAGES = 10
CONCURRENT_READS = 4


@dataclass(slots=True)
class IndexedThread:
    thread: discord.Thread
    # The bot's own messages among the thread's first OPENING_MESSAGES, oldest first; None until read.
    opening: list[str] | None = None


@dataclass(slots=True)
class DiscoveryIndex:
    bot_user_id: Callable[[], int | None]
    clock: Callable[[], float] = time.monotonic
    threads: dict[int, IndexedThread] = field(default_factory=dict)
    # True when the last refresh listed every page and read every candidate's opening messages.
    complete: bool = False
    refreshed_at: float | None = None
    _refreshing: asyncio.Task[None] | None = None

    async def fresh(self, channel: discord.TextChannel, *, force: bool = False) -> None:
        """Refresh unless a refresh finished moments ago; concurrent callers share one refresh."""
        recent = self.refreshed_at is not None and self.clock() - self.refreshed_at < INDEX_REUSE_SECONDS
        if recent and self.complete and not force:
            return
        if self._refreshing is None:
            self._refreshing = asyncio.create_task(self.refresh(channel), name="agent-session-discovery-index")
            self._refreshing.add_done_callback(self._refresh_done)
        # Shielded: a waiter that is cancelled leaves the shared refresh (and its Discord reads) running.
        await asyncio.shield(self._refreshing)

    def _refresh_done(self, task: asyncio.Task[None]) -> None:
        if self._refreshing is task:
            self._refreshing = None
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.warning("Agent session discovery index refresh failed: %r", exc)

    async def refresh(self, channel: discord.TextChannel) -> None:
        listed, complete = await self.list_threads(channel)
        # A listing only adds and updates. A thread missing from it is not gone: discord.py caches a thread it just
        # created or reopened only once the gateway says so, and archive listings omit open threads. Only Discord
        # saying a thread does not exist removes one (forget), or empties what it matches (a NotFound read).
        for thread in listed:
            if (known := self.threads.get(thread.id)) is not None:
                known.thread = thread
            else:
                self.threads[thread.id] = IndexedThread(thread)
        unread = [entry for entry in self.threads.values() if entry.opening is None]
        slots = asyncio.Semaphore(CONCURRENT_READS)

        async def read(entry: IndexedThread) -> bool:
            async with slots:
                return await self.read_opening(entry)

        results = await asyncio.gather(*(read(entry) for entry in unread))
        self.complete = complete and all(results)
        self.refreshed_at = self.clock()
        if not self.complete:
            logger.warning("Agent session discovery is incomplete: %s of %s candidate(s) unread", results.count(False), len(unread))

    async def list_threads(self, channel: discord.TextChannel) -> tuple[list[discord.Thread], bool]:
        listed: dict[int, discord.Thread] = {thread.id: thread for thread in channel.threads}
        complete = True

        async def collect(threads: AsyncIterator[discord.Thread]) -> None:
            async for thread in threads:
                listed.setdefault(thread.id, thread)

        try:
            await collect(channel.archived_threads(private=False, joined=False, limit=None))
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to list public archived Agent session threads", exc_info=True)
            complete = False
        try:
            await collect(channel.archived_threads(private=True, joined=False, limit=None))
        except discord.Forbidden:
            # Without Manage Threads only the joined private archive can be listed; that is all the bot can reach.
            logger.warning("Unable to list all private archived Agent session threads; listing joined ones instead")
            try:
                await collect(channel.archived_threads(private=True, joined=True, limit=None))
            except (discord.DiscordException, ValueError):
                logger.warning("Unable to list joined private archived Agent session threads", exc_info=True)
                complete = False
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to list private archived Agent session threads", exc_info=True)
            complete = False
        return list(listed.values()), complete

    async def read_opening(self, entry: IndexedThread) -> bool:
        bot_user_id = self.bot_user_id()
        if bot_user_id is None:
            return False
        try:
            entry.opening = [
                message.content
                async for message in entry.thread.history(limit=OPENING_MESSAGES, oldest_first=True)
                if message.author.id == bot_user_id
            ]
        except (discord.NotFound, discord.Forbidden):
            # Deleted since the listing, or not readable by the bot (so not a thread it could reattach): nothing to
            # match. Only a failure that may pass (a 5xx, a rate limit) leaves the index incomplete.
            entry.opening = []
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to read Agent session thread %s; it stays unread", entry.thread.id)
            return False
        return True

    def add(self, thread: discord.Thread, opening: list[str]) -> None:
        """A thread this bot just created, so a reconnect finds it before the next listing does."""
        self.threads[thread.id] = IndexedThread(thread, list(opening))

    def forget(self, thread_id: int) -> None:
        """Discord says this thread no longer exists; drop what was read, so any listing that still names it re-reads."""
        self.threads.pop(thread_id, None)

    def entries(self) -> list[IndexedThread]:
        return [entry for entry in self.threads.values() if entry.opening is not None]
