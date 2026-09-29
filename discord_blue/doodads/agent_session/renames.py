"""Session thread renames within Discord's rate limit.

Discord allows about two renames per thread in ten minutes. A client may learn
a better title often (Claude Code's latest substantial prompt, for example), so
renames are coalesced per thread: only the latest wanted name is kept, a rename
that would exceed the limit waits for the window to reopen, and a name the
thread already has is never sent. Renames run in background tasks, so the
session's connection never waits on Discord.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Protocol

import discord

logger = logging.getLogger(__name__)

RENAME_WINDOW_SECONDS = 600.0
RENAMES_PER_WINDOW = 2
RENAME_TIMEOUT_SECONDS = 5.0


class RenameTarget(Protocol):
    @property
    def name(self) -> str: ...

    async def edit(self, *, name: str) -> object: ...


class ThreadRenamer:
    def __init__(
        self,
        resolve: Callable[[int], RenameTarget | None],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout: float = RENAME_TIMEOUT_SECONDS,
    ) -> None:
        # resolve returns the thread only while a live session owns it, so an archived thread is left alone.
        self.resolve, self.clock, self.sleep, self.timeout = resolve, clock, sleep, timeout
        self.wanted: dict[int, str] = {}
        self.recent: dict[int, deque[float]] = {}
        self.tasks: dict[int, asyncio.Task[None]] = {}

    def request(self, thread_id: int, name: str) -> None:
        """Ask for a rename; a later request for the same thread replaces one that has not run yet."""
        self.wanted[thread_id] = name
        if thread_id not in self.tasks:
            self.tasks[thread_id] = asyncio.create_task(self.drain(thread_id), name=f"agent-session-rename-{thread_id}")

    def wait_seconds(self, thread_id: int) -> float:
        recent = self.recent.setdefault(thread_id, deque(maxlen=RENAMES_PER_WINDOW))
        now = self.clock()
        while recent and now - recent[0] >= RENAME_WINDOW_SECONDS:
            recent.popleft()
        return RENAME_WINDOW_SECONDS - (now - recent[0]) if len(recent) >= RENAMES_PER_WINDOW else 0.0

    async def drain(self, thread_id: int) -> None:
        try:
            while (name := self.wanted.get(thread_id)) is not None:
                target = self.resolve(thread_id)
                if target is None or target.name == name:
                    self.wanted.pop(thread_id, None)
                    continue
                if (wait := self.wait_seconds(thread_id)) > 0:
                    await self.sleep(wait)
                    continue
                self.wanted.pop(thread_id, None)
                try:
                    await asyncio.wait_for(target.edit(name=name), timeout=self.timeout)
                except TimeoutError:
                    # discord.py sleeps through a rate limit; treat the window as full and try the name again later.
                    self.recent[thread_id].extend([self.clock()] * RENAMES_PER_WINDOW)
                    self.wanted.setdefault(thread_id, name)
                except discord.DiscordException:
                    logger.warning("Could not rename Agent session thread %s", thread_id)
                else:
                    self.recent[thread_id].append(self.clock())
        finally:
            self.tasks.pop(thread_id, None)

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
