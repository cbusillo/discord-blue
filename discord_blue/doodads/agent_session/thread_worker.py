"""One worker per session thread makes every change the bridge makes to that thread.

Discord applies a request whether or not the caller is still waiting, and cancelling a discord.py 2.7.1 request
can leave its rate-limit state broken (a cancel during a global 429 clears `_global_over`, so later requests wait
forever). So no request here is ever cancelled. Callers say what they want the thread to be, open or closed, plus
its name, and the thread's worker gets it there one request at a time, each run to completion. After every request
it reads what is wanted again: an archive that finishes after a reconnect asked for the thread open is followed by a
reopen before the reconnect is told the thread is ready.

Opening comes first, then closing, then renaming. A rename is sent only while nothing more important is pending,
at most RENAMES_PER_WINDOW times per window, and is retried after a rate limit instead of sleeping inside discord.py.
Callers that need a bound wait on the worker (or use `bounded`) and stop waiting; the request itself carries on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, Literal, Protocol, TypeVar

import discord

from discord_blue.doodads.agent_session.sessions import CleanupStep

logger = logging.getLogger(__name__)
T = TypeVar("T")

THREAD_CLOSE_STEPS: frozenset[CleanupStep] = frozenset({"disconnect_notice", "members", "archive", "leave"})
# Requests in flight across all threads: a restart wave queues here rather than inside discord.py's buckets.
CONCURRENT_THREAD_REQUESTS = 4
RENAME_WINDOW_SECONDS = 600.0
RENAMES_PER_WINDOW = 2
CLOSE_STEP_ACTIONS = {
    "unarchive": "reopen (to close)",
    "disconnect_notice": "post the close notice in",
    "archive": "archive",
    "leave": "leave",
}


class RenameTarget(Protocol):
    @property
    def name(self) -> str | None: ...

    async def edit(self, *, name: str) -> object: ...


class ThreadHooks(Protocol):
    """What a worker needs from the bridge; each is read at the moment a request is about to be sent."""

    def owned(self, thread_id: int) -> bool:
        """A session holds this thread, so no close may touch it."""
        ...

    def rename_target(self, thread_id: int, epoch: str) -> RenameTarget | None:
        """The thread to rename, only while the session epoch that asked still owns it."""
        ...

    def bot_user_id(self) -> int | None:
        """The bot's own member ID, never removed from a thread."""
        ...

    async def post_close_notice(self, thread: discord.Thread) -> None: ...

    async def add_configured_members(self, thread: discord.Thread) -> None: ...


class ThreadWorker:
    def __init__(self, pool: ThreadWorkers, thread_id: int) -> None:
        self.pool, self.thread_id = pool, thread_id
        self.thread: discord.Thread | None = None
        self.wanted: Literal["open", "closed"] | None = None
        # Open: the requests still to send ("reopen", "join", "members"), and who waits for them.
        self.open_steps: list[str] = []
        self.open_waiters: list[asyncio.Future[discord.Thread]] = []
        # Close: the caller's own step set, updated in place as each step lands, so a caller that stopped waiting
        # still holds exactly what remains. Steps already tried in this pass are not tried again until the next.
        self.close_steps: set[CleanupStep] = set()
        self.close_tried: set[str] = set()
        self.close_waiters: list[asyncio.Future[None]] = []
        # Members still to remove in this pass (None until fetched), one request each, so a reopen asked for midway
        # stops the rest; and whether every fetch and removal so far succeeded.
        self.member_ids: list[int] | None = None
        self.members_complete = True
        # Our own archive may have landed (or be about to) without discord.py's cached thread saying so yet.
        self.maybe_archived = False
        self.name: tuple[str, str] | None = None
        self.applied_name: str | None = None
        self.renames: deque[float] = deque(maxlen=RENAMES_PER_WINDOW)
        self.rename_not_before = 0.0
        self.in_flight = False
        self.task: asyncio.Task[None] | None = None
        self.wake = asyncio.Event()

    # Requests from callers. None of them waits on Discord.

    def open(self, thread: discord.Thread) -> asyncio.Future[discord.Thread]:
        self.thread = thread
        self.wanted = "open"
        # A close in progress stops before its next step; what it already did is undone by the reopen.
        self.finish_close()
        self.open_steps = ["reopen", "join", "members"]
        waiter: asyncio.Future[discord.Thread] = asyncio.get_running_loop().create_future()
        self.open_waiters.append(waiter)
        self.start()
        return waiter

    def close(self, thread: discord.Thread, steps: set[CleanupStep]) -> asyncio.Future[None]:
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if self.wanted == "open" and self.open_waiters:
            # An attach is reopening the thread: the newer intent wins, and the close's steps stay pending for a
            # retry, which finds the thread owned and drops them.
            waiter.set_result(None)
            return waiter
        if steps is not self.close_steps:
            # A newer close replaces an older one's steps; the older caller keeps its own record of what remains.
            self.finish_close()
            self.close_steps = steps
        self.thread = thread
        self.wanted = "closed"
        self.start_close_pass()
        self.close_waiters.append(waiter)
        self.start()
        return waiter

    def rename(self, name: str, epoch: str) -> None:
        self.name = (name, epoch)
        self.start()

    @property
    def busy(self) -> bool:
        return self.in_flight or bool(self.open_waiters or self.close_waiters)

    @property
    def closing(self) -> bool:
        return self.wanted == "closed" and bool(self.close_waiters)

    # The worker.

    def start(self) -> None:
        self.wake.set()
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name=f"agent-session-thread-{self.thread_id}")

    async def run(self) -> None:
        try:
            while not self.pool.stopped:
                request = self.next_request()
                if request is not None:
                    async with self.pool.slots:
                        self.in_flight = True
                        try:
                            await request
                        finally:
                            self.in_flight = False
                    continue
                delay = self.rename_delay()
                if delay is None:
                    return
                self.wake.clear()
                # Only a timer: a newer request wakes the worker early.
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout=delay)
        finally:
            self.task = None

    def next_request(self) -> Awaitable[None] | None:
        """The next request to send, or None; decides from current state, with no await in between."""
        thread = self.thread
        if self.wanted == "open" and self.open_waiters and thread is not None:
            return self.open_request(thread)
        if self.wanted == "closed" and self.close_waiters and thread is not None:
            if self.pool.hooks.owned(self.thread_id):
                # A session took the thread back: the close is moot, and nothing of it remains to retry.
                self.close_steps.difference_update(THREAD_CLOSE_STEPS)
                self.finish_close()
                return self.next_request()
            request = self.close_request(thread)
            if request is not None:
                return request
            self.finish_close()
        if self.name is not None:
            return self.rename_request()
        return None

    def open_request(self, thread: discord.Thread) -> Awaitable[None] | None:
        while self.open_steps:
            step = self.open_steps.pop(0)
            if step == "reopen" and (thread.archived or thread.locked or self.maybe_archived):
                return self.send_open_step(self.reopen(thread))
            if step == "join" and thread.is_private():
                return self.send_open_step(thread.join())
            if step == "members":
                return self.send_open_step(self.pool.hooks.add_configured_members(thread))
        waiters, self.open_waiters = self.open_waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(thread)
        return self.next_request()

    async def reopen(self, thread: discord.Thread) -> None:
        await thread.edit(archived=False, locked=False, reason="Reattaching live Agent session after bridge restart")
        self.maybe_archived = False

    async def send_open_step(self, request: Awaitable[object]) -> None:
        try:
            await request
        except Exception as exc:
            # The attach that asked fails, as it would have without the worker; the client retries its hello.
            logger.warning("Unable to reopen Agent session thread %s: %s", self.thread_id, type(exc).__name__)
            self.wanted = None
            self.open_steps = []
            self.fail_open(exc)

    def close_request(self, thread: discord.Thread) -> Awaitable[None] | None:
        steps, untried = self.close_steps, self.close_steps - self.close_tried
        archived = thread.archived or self.maybe_archived
        if archived and untried & {"disconnect_notice", "members"} and "unarchive" not in self.close_tried:
            # A notice or member change needs the thread open; it is archived again at the end.
            steps.update({"archive", "leave"})
            return self.send_close_step("unarchive", self.unarchive_for_close(thread))
        if "disconnect_notice" in untried:
            return self.send_close_step("disconnect_notice", self.pool.hooks.post_close_notice(thread))
        if "members" in untried:
            if self.member_ids is None:
                return self.fetch_members(thread)
            if self.member_ids:
                return self.remove_member(thread, self.member_ids.pop(0))
            self.close_tried.add("members")
            if self.members_complete:
                steps.discard("members")
            return self.close_request(thread)
        if "archive" in untried:
            self.maybe_archived = True
            return self.send_close_step("archive", thread.edit(archived=True, locked=True, reason="Agent session ended"))
        if "leave" in untried and "archive" not in steps:
            return self.send_close_step("leave", thread.leave())
        return None

    async def fetch_members(self, thread: discord.Thread) -> None:
        try:
            members: list[Any] = list(await thread.fetch_members())
        except Exception:
            logger.warning("Unable to list members of Agent session thread %s", self.thread_id, exc_info=True)
            self.members_complete = False
            members = list(thread.members)  # The cached members are still worth removing.
        bot_user_id = self.pool.hooks.bot_user_id()
        self.member_ids = [member.id for member in members if member.id != bot_user_id]

    async def remove_member(self, thread: discord.Thread, member_id: int) -> None:
        try:
            await thread.remove_user(discord.Object(id=member_id))
        except Exception:
            logger.warning("Unable to remove user %s from Agent session thread %s", member_id, self.thread_id)
            self.members_complete = False

    async def unarchive_for_close(self, thread: discord.Thread) -> None:
        await thread.edit(archived=False, locked=False, reason="Preparing to close Agent session thread")
        self.maybe_archived = False

    async def send_close_step(self, step: str, request: Awaitable[object]) -> None:
        """Send one close request; the step stays pending if it raises."""
        self.close_tried.add(step)
        try:
            await request
        except Exception:
            logger.warning("Unable to %s Agent session thread %s", CLOSE_STEP_ACTIONS[step], self.thread_id, exc_info=True)
        else:
            self.close_steps.discard(step)  # type: ignore[arg-type]  # "unarchive" is never in it.

    def rename_delay(self) -> float | None:
        """Seconds until a wanted rename may be sent; None when there is none."""
        if self.name is None:
            return None
        now = self.pool.clock()
        self.forget_old_renames()
        window = RENAME_WINDOW_SECONDS - (now - self.renames[0]) if len(self.renames) >= RENAMES_PER_WINDOW else 0.0
        return max(window, self.rename_not_before - now, 0.0)

    def forget_old_renames(self) -> None:
        now = self.pool.clock()
        while self.renames and now - self.renames[0] >= RENAME_WINDOW_SECONDS:
            self.renames.popleft()

    def rename_request(self) -> Awaitable[None] | None:
        """The wanted rename, if it is still wanted, still needed and allowed now; a pointless one is dropped at once.

        Reached only when no open or close is pending, so a rename never delays a reopen.
        """
        assert self.name is not None
        name, epoch = self.name
        target = self.pool.hooks.rename_target(self.thread_id, epoch)
        # The last name we gave decides, not the cache: discord.py's cached name lags behind a successful edit.
        if target is None or (self.applied_name if self.applied_name is not None else target.name) == name:
            self.name = None
            return None
        if self.rename_delay():
            return None  # run() waits for the window, or for a newer request.
        self.name = None
        return self.send_rename(target, name, epoch)

    async def send_rename(self, target: RenameTarget, name: str, epoch: str) -> None:
        try:
            await target.edit(name=name)
        except discord.RateLimited as exc:
            # Too long a wait for discord.py to sleep through: try this name again once Discord allows it,
            # unless a newer one was asked for meanwhile.
            # The thread still has the name it was last given, so applied_name stays as it is.
            self.rename_not_before = self.pool.clock() + exc.retry_after
            if self.name is None:
                self.name = (name, epoch)
        except Exception:
            # Any failure, not just Discord's: an exception escaping here would end the worker with work pending.
            logger.warning("Could not rename Agent session thread %s", self.thread_id, exc_info=True)
        else:
            self.renames.append(self.pool.clock())
            self.applied_name = name

    # Settling waiters.

    def start_close_pass(self) -> None:
        self.close_tried = set()
        self.member_ids, self.members_complete = None, True

    def finish_close(self) -> None:
        waiters, self.close_waiters = self.close_waiters, []
        self.start_close_pass()
        if self.wanted == "closed":
            self.wanted = None
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def fail_open(self, exc: Exception) -> None:
        waiters, self.open_waiters = self.open_waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_exception(exc)
                waiter.exception()  # Retrieved here; a caller that stopped waiting must not log it as lost.

    @property
    def idle(self) -> bool:
        """Nothing to do and no rename window to remember: the worker can be dropped and made afresh later."""
        self.forget_old_renames()
        return self.task is None and not self.busy and self.name is None and not self.renames


class ThreadWorkers:
    def __init__(
        self,
        hooks: ThreadHooks,
        *,
        clock: Callable[[], float] = time.monotonic,
        concurrency: int = CONCURRENT_THREAD_REQUESTS,
    ) -> None:
        self.hooks, self.clock = hooks, clock
        self.slots = asyncio.Semaphore(concurrency)
        self.workers: dict[int, ThreadWorker] = {}
        self.background: set[asyncio.Task[Any]] = set()
        self.stopped = False

    def worker(self, thread_id: int) -> ThreadWorker:
        for idle in [key for key, worker in self.workers.items() if key != thread_id and worker.idle]:
            del self.workers[idle]
        worker = self.workers.get(thread_id)
        if worker is None:
            worker = self.workers[thread_id] = ThreadWorker(self, thread_id)
        return worker

    async def open(self, thread: discord.Thread) -> discord.Thread:
        """Reopen, unlock and join the thread and add its configured members; raises if Discord refuses."""
        return await asyncio.shield(self.worker(thread.id).open(thread))

    async def close(self, thread: discord.Thread, steps: set[CleanupStep], timeout: float | None = None) -> set[CleanupStep]:
        """One pass over the close steps; `steps` keeps what remains, and keeps shrinking after a timeout."""
        waiter = self.worker(thread.id).close(thread, steps)
        await asyncio.wait({waiter}, timeout=timeout)
        return steps

    def rename(self, thread_id: int, name: str, epoch: str) -> None:
        self.worker(thread_id).rename(name, epoch)

    def busy(self, thread_id: int) -> bool:
        worker = self.workers.get(thread_id)
        return worker is not None and worker.busy

    def closing(self, thread_id: int) -> bool:
        worker = self.workers.get(thread_id)
        return worker is not None and worker.closing

    async def bounded(self, request: Awaitable[T], timeout: float) -> T:
        """Wait up to `timeout` for a Discord request without cancelling it; TimeoutError if it is still running.

        A request that outlives the wait, or whose caller is cancelled, carries on and its outcome is only logged.
        """
        task: asyncio.Task[T] = asyncio.ensure_future(request)
        self.background.add(task)
        task.add_done_callback(self.request_done)
        await asyncio.wait({task}, timeout=timeout)
        if not task.done():
            raise TimeoutError
        return task.result()

    def request_done(self, task: asyncio.Task[Any]) -> None:
        self.background.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            # Also retrieved (and raised) by a caller still waiting; logged for one that stopped.
            logger.debug("Agent session Discord request failed: %r", exc)

    def stop(self) -> None:
        """At shutdown: drop everything still wanted and let each worker end after its current request.

        Nothing is cancelled. The bot, and its HTTP client, can outlive this bridge (a doodad reload), and a request
        cancelled during a global rate limit would leave every later request of that client waiting forever.
        """
        self.stopped = True
        for worker in self.workers.values():
            worker.name = None
            worker.open_steps = []
            worker.fail_open(RuntimeError("the Agent session bridge is stopping"))
            worker.finish_close()
            worker.wake.set()
