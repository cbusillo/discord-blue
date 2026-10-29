"""Closing what ended sessions leave behind (their notifications and threads), and sweeping up orphans.

Cleanup that does not finish is kept as a residual record and retried by maintenance until done or exhausted.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections import Counter
from typing import TYPE_CHECKING, Protocol

import discord

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.director_questions import QuestionCards

from discord_blue.doodads.agent_session import threads as threads_module
from discord_blue.doodads.agent_session.sessions import AgentSession, AgentSessionRegistry, CleanupStep, PendingSessionCleanup
from discord_blue.doodads.agent_session.store import SessionState, SessionStore, StoredSession
from discord_blue.doodads.agent_session.thread_worker import THREAD_CLOSE_STEPS, ThreadWorkers
from discord_blue.doodads.agent_session.threads import SESSION_NOTIFICATION_PREFIXES, SESSION_START_PREFIX
from discord_blue.plugs.discord_plug import BlueBot

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.session_threads import SessionThreads

logger = logging.getLogger(__name__)
SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS = 2
# How long teardown waits for its thread's close steps. The steps themselves are never cancelled: one still running
# when the wait ends lands later, and the residual record it shares is updated as it does.
SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS = 6
SESSION_FINALIZATION_TIMEOUT_SECONDS = 9
THREAD_LOOKUP_TIMEOUT_SECONDS = 1
THREAD_CLOSE_WAIT_SECONDS = SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS - THREAD_LOOKUP_TIMEOUT_SECONDS
MAINTENANCE_DISCOVERY_TIMEOUT_SECONDS = 30
PENDING_CLEANUP_LIMIT = 256
PENDING_CLEANUP_MAX_ATTEMPTS = 5
# Notifications deleted recently enough that a history page read before the delete may still list them.
DELETED_NOTIFICATIONS_REMEMBERED = 1024


class CleanupHooks(Protocol):
    """What cleanup needs from the bridge."""

    bot: BlueBot
    session_threads: SessionThreads
    sessions: AgentSessionRegistry
    store: SessionStore
    questions: QuestionCards
    threads: ThreadWorkers

    def attaching(self) -> bool: ...

    def bot_user_id(self) -> int | None: ...

    def record_maintenance_progress(self) -> None: ...

    def save_session(self, session: AgentSession, status: SessionState, *, grace_until: float = 0) -> None: ...

    def session_lifecycle_lock(self, session_id: str) -> asyncio.Lock: ...

    def stored_thread_protected(self, thread_id: int) -> bool: ...

    def thread_channel(self, thread_id: int) -> object | None: ...


class SessionCleanups:
    def __init__(self, bridge: CleanupHooks) -> None:
        self.bridge = bridge
        self.pending: dict[tuple[str, str, int | None], PendingSessionCleanup] = {}
        self.finalizing: set[tuple[str, str, int | None]] = set()
        # Notifications with a DELETE sent (a count, as sweeps and cleanups can overlap) or already done: an attach or
        # sweep never adopts one, even from a history page read before the delete landed.
        self._notification_deletes: Counter[int] = Counter()
        self._deleted_notifications: dict[int, None] = {}

    async def cleanup_stale_session_notifications(self) -> None:
        for channel_id in threads_module.agent_session_channel_ids(self.bridge.bot):
            await self.cleanup_channel_notifications(channel_id)

    async def cleanup_channel_notifications(self, channel_id: int) -> None:
        # An in-flight attachment may have published a notice before binding it.
        # Defer this sweep rather than pinning maintenance behind its network I/O.
        if self.bridge.attaching():
            return
        try:
            channel = await asyncio.wait_for(threads_module.get_agent_session_channel(self.bridge.bot, channel_id), timeout=3)
        except Exception:
            logger.warning("Unable to clean Agent session notifications: channel is unavailable", exc_info=True)
            return
        bot_user = self.bridge.bot.user
        if bot_user is None:
            return

        deferred: dict[int, list[discord.Message]] = {}
        observed: dict[int, tuple[AgentSession, int | None]] = {}
        messages = channel.history(limit=None).__aiter__()
        while True:
            self.bridge.record_maintenance_progress()
            try:
                message = await self.bridge.threads.bounded(anext(messages), MAINTENANCE_DISCOVERY_TIMEOUT_SECONDS)
            except StopAsyncIteration:
                break
            except Exception:
                logger.warning("Unable to scan Agent session channel for stale notifications", exc_info=True)
                return
            if message.author.id != bot_user.id or not message.content.startswith(SESSION_NOTIFICATION_PREFIXES):
                continue
            thread_id = threads_module.notification_thread_id(message.content)
            if thread_id is None:
                await self.delete_discovered_notification(message, None)
                continue
            if self.bridge.threads.busy(thread_id) or self.bridge.stored_thread_protected(thread_id):
                continue
            session = self.bridge.sessions.get_by_thread(thread_id)
            if session is None:
                await self.delete_discovered_notification(message, thread_id)
            else:
                deferred.setdefault(thread_id, []).append(message)
                observed.setdefault(thread_id, (session, session.notification_message_id))

        for thread_id, notices in deferred.items():
            self.bridge.record_maintenance_progress()
            if self.bridge.threads.busy(thread_id) or self.bridge.attaching():
                continue
            current = self.bridge.sessions.get_by_thread(thread_id)
            old_session, old_notice = observed[thread_id]
            if current is old_session and current.notification_message_id == old_notice:
                ids = {notice.id for notice in notices}
                notices = [notice for notice in notices if not self.notification_going(notice.id)]
                if notices and current.notification_message_id not in ids:
                    # Only adopt if neither the connection nor its notice
                    # changed during discovery. Reconnect always wins.
                    current.notification_message_id = notices[0].id
                    if record := self.bridge.store.records.get(current.session_id):
                        self.bridge.save_session(current, record.status, grace_until=record.grace_until)
            for notice in notices:
                self.bridge.record_maintenance_progress()
                if self.bridge.threads.busy(thread_id):
                    break
                current = self.bridge.sessions.get_by_thread(thread_id)
                if current is not None and current.notification_message_id == notice.id:
                    continue
                await self.delete_discovered_notification(notice, thread_id)

    async def delete_discovered_notification(self, message: discord.Message, thread_id: int | None) -> None:
        if self.bridge.attaching() or (thread_id is not None and self.bridge.stored_thread_protected(thread_id)):
            # A newly created thread can have a notice before bind_thread runs.
            return
        try:
            await self.bridge.threads.bounded(
                self.delete_notification_message(message), SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS
            )
        except discord.NotFound:
            return
        except Exception:
            logger.warning("Unable to delete stale Agent session notification %s", message.id, exc_info=True)
            self.remember_pending_cleanup(
                PendingSessionCleanup(
                    session_id=f"orphan-notification-{message.id}",
                    session_epoch="orphan",
                    thread_id=thread_id,
                    notification_message_id=message.id,
                    pending_steps={"notification"},
                )
            )

    async def cleanup_stale_session_threads(self) -> None:
        for channel_id in threads_module.agent_session_channel_ids(self.bridge.bot):
            await self.cleanup_channel_threads(channel_id)

    async def cleanup_channel_threads(self, channel_id: int) -> None:
        self.bridge.record_maintenance_progress()
        try:
            channel = await asyncio.wait_for(threads_module.get_agent_session_channel(self.bridge.bot, channel_id), timeout=3)
        except Exception:
            logger.warning("Unable to clean Agent session threads: channel is unavailable", exc_info=True)
            return
        self.bridge.record_maintenance_progress()
        try:
            candidates = await self.bridge.threads.bounded(
                self.session_thread_candidates(channel), MAINTENANCE_DISCOVERY_TIMEOUT_SECONDS
            )
        except Exception:
            logger.warning("Unable to discover archived Agent session threads; checking active threads", exc_info=True)
            candidates = list(channel.threads)

        seen: set[int] = set()
        for thread in candidates:
            self.bridge.record_maintenance_progress()
            if thread.id in seen:
                continue
            seen.add(thread.id)
            if self.has_pending_cleanup_for_thread(thread.id):
                continue
            # Discovery must not reopen completed threads or repost a disconnect
            # notice each sweep. Explicit residual work is retried separately.
            if thread.archived and thread.locked:
                continue
            if thread.id in self.bridge.sessions.by_thread or self.bridge.stored_thread_protected(thread.id):
                continue
            try:
                matches = await self.bridge.threads.bounded(self.is_agent_session_session_thread(thread), 3)
            except Exception:
                logger.warning("Unable to inspect stale Agent session thread %s", thread.id, exc_info=True)
                continue
            if not matches:
                continue
            if (
                self.bridge.threads.busy(thread.id)
                or self.bridge.stored_thread_protected(thread.id)
                or thread.id in self.bridge.sessions.by_thread
                or self.bridge.attaching()
                or self.has_pending_cleanup_for_thread(thread.id)
            ):
                # Creation may have published a marker before binding its
                # thread. Defer rather than racing an in-flight attachment.
                continue
            cleanup = PendingSessionCleanup(
                session_id=f"orphan-thread-{thread.id}",
                session_epoch="orphan",
                thread_id=thread.id,
                notification_message_id=None,
                pending_steps={"disconnect_notice", "members", "archive", "leave"},
            )
            # Remembered first and shared with the worker, which shrinks it as each step lands.
            self.remember_pending_cleanup(cleanup)
            await self.close_thread(thread, cleanup.pending_steps, timeout=SESSION_THREAD_CLEANUP_TIMEOUT_SECONDS)
            self.bridge.record_maintenance_progress()

    @staticmethod
    async def session_thread_candidates(channel: discord.TextChannel) -> list[discord.Thread]:
        candidates = list(channel.threads)
        try:
            async for thread in channel.archived_threads(
                limit=50,
            ):
                candidates.append(thread)
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to scan public archived Agent session threads")
        try:
            async for thread in channel.archived_threads(
                private=True,
                joined=True,
                limit=50,
            ):
                candidates.append(thread)
        except (discord.DiscordException, ValueError):
            logger.warning("Unable to scan joined private archived Agent session threads")
        return candidates

    async def is_agent_session_session_thread(self, thread: discord.Thread) -> bool:
        bot_user = self.bridge.bot.user
        if bot_user is None:
            return False
        try:
            async for message in thread.history(limit=10, oldest_first=True):
                if message.author.id != bot_user.id:
                    continue
                if any(marker.startswith(SESSION_START_PREFIX) for marker in threads_module.session_message_markers(message)):
                    return True
        except discord.DiscordException:
            logger.warning("Unable to inspect Agent session thread %s", thread.id)
        return False

    async def recover_stored_cleanups(self) -> None:
        """Expire startup grace using the same ownership checks and workers as a disconnected socket."""
        for session_id, record in list(self.bridge.store.records.items()):
            if record.status != "closing" and not (record.status == "grace" and record.grace_until <= time.time()):
                continue
            if any(cleanup.session_id == session_id for cleanup in self.pending.values()):
                continue
            try:
                await self.recover_stored_cleanup(session_id, record)
            except Exception:
                logger.warning("Unable to recover Agent session cleanup %s; continuing maintenance", session_id, exc_info=True)
                if self.bridge.store.records.get(session_id) is record and self.bridge.sessions.get(session_id) is None:
                    attempts = record.recovery_attempts + 1
                    self.bridge.store.put(
                        session_id,
                        dataclasses.replace(
                            record,
                            recovery_attempts=attempts,
                            status="closed" if attempts >= PENDING_CLEANUP_MAX_ATTEMPTS else record.status,
                            updated_at=time.time(),
                        ),
                    )
            finally:
                current = self.bridge.store.records.get(session_id)
                if current is not None and current.status in {"closing", "closed"} and self.bridge.sessions.get(session_id) is None:
                    await self.bridge.questions.close(session_id)

    async def recover_stored_cleanup(self, session_id: str, record: StoredSession) -> None:
        lock = self.bridge.session_lifecycle_lock(session_id)
        if lock.locked() or self.bridge.attaching() or self.bridge.sessions.get(session_id) is not None:
            return
        async with lock:
            if self.bridge.store.records.get(session_id) is not record or self.bridge.sessions.get(session_id) is not None:
                return
            thread = await self.bridge.threads.bounded(
                self.bridge.session_threads.validated_stored_thread(session_id, record), THREAD_LOOKUP_TIMEOUT_SECONDS
            )
            if thread is None:
                return
            notification_id = record.notification_id
            if notification_id is not None:
                try:
                    channel = await threads_module.get_agent_session_channel(self.bridge.bot, thread.parent_id)
                    notification = await self.bridge.threads.bounded(
                        channel.fetch_message(notification_id), SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS
                    )
                    if (
                        notification.author.id != self.bridge.bot_user_id()
                        or threads_module.notification_thread_id(notification.content) != thread.id
                    ):
                        notification_id = None
                except (discord.DiscordException, TimeoutError, ValueError):
                    notification_id = None  # Notification cleanup remains best-effort; the thread can still close.
            # A hello can register while the Discord reads run, then wait on this lock. Its newer intent wins.
            if self.bridge.sessions.get(session_id) is not None or self.bridge.attaching():
                return
            self.bridge.store.put(session_id, dataclasses.replace(record, status="closing", updated_at=time.time()))
            cleanup = PendingSessionCleanup(
                session_id,
                "restart",
                record.thread_id,
                notification_id,
                set(record.pending_steps) if record.pending_steps is not None else {"notification", *THREAD_CLOSE_STEPS},
            )
            residual = await self.cleanup_session_artifacts(cleanup)
            if residual is not None:
                self.save_cleanup(residual)
                self.remember_pending_cleanup(residual)
            else:
                self.bridge.store.put(session_id, dataclasses.replace(record, status="closed", updated_at=time.time()))

    @staticmethod
    def pending_cleanup_for_session(session: AgentSession) -> PendingSessionCleanup:
        pending_steps: set[CleanupStep] = set()
        if session.notification_message_id is not None or session.thread_id is not None:
            pending_steps.add("notification")
        if session.thread_id is not None:
            pending_steps.update(THREAD_CLOSE_STEPS)
        return PendingSessionCleanup(
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            thread_id=session.thread_id,
            notification_message_id=session.notification_message_id,
            pending_steps=pending_steps,
        )

    def save_cleanup(self, cleanup: PendingSessionCleanup, *, exhausted: bool = False) -> None:
        record = self.bridge.store.records.get(cleanup.session_id)
        if record is not None and record.status == "closing" and record.thread_id == cleanup.thread_id:
            self.bridge.store.put(
                cleanup.session_id,
                dataclasses.replace(
                    record,
                    status="closed" if exhausted or not cleanup.pending_steps else "closing",
                    pending_steps=tuple(sorted(cleanup.pending_steps)),
                    updated_at=time.time(),
                ),
            )

    def remember_pending_cleanup(self, cleanup: PendingSessionCleanup) -> None:
        if not cleanup.pending_steps:
            return
        existing = self.pending.get(cleanup.key)
        if existing is not None:
            if existing is cleanup:
                return
            # Reconnect preserves session/epoch/thread identity, but a later
            # teardown has new work and possibly a new notification. Callers
            # share their live residual record even on timeout/cancellation;
            # a distinct record is the newer authoritative observation.
            self.pending[cleanup.key] = cleanup
            return
        if len(self.pending) >= PENDING_CLEANUP_LIMIT:
            self.save_cleanup(cleanup, exhausted=True)
            logger.warning(
                "Dropping Agent session cleanup retry for %s/%s because the %s-record limit was reached; "
                "periodic orphan reconciliation remains enabled",
                cleanup.session_id,
                cleanup.session_epoch,
                PENDING_CLEANUP_LIMIT,
            )
            return
        self.pending[cleanup.key] = cleanup

    def has_pending_cleanup_for_thread(self, thread_id: int) -> bool:
        if any(key[2] == thread_id for key in self.finalizing):
            return True
        thread_steps = {"disconnect_notice", "members", "archive", "leave"}
        return any(
            cleanup.thread_id == thread_id and bool(cleanup.pending_steps & thread_steps) for cleanup in self.pending.values()
        )

    async def retry_pending_cleanups(self) -> None:
        for key, cleanup in list(self.pending.items()):
            self.bridge.record_maintenance_progress()
            if self.pending.get(key) is not cleanup or self.bridge.attaching():
                continue
            lifecycle_lock = self.bridge.session_lifecycle_lock(cleanup.session_id)
            if lifecycle_lock.locked():
                continue
            async with lifecycle_lock:
                if self.bridge.attaching():
                    continue
                current = self.bridge.sessions.get(cleanup.session_id)
                if current is not None and current.thread_id == cleanup.thread_id:
                    self.pending.pop(key, None)
                    continue
                if cleanup.thread_id is not None and self.bridge.threads.busy(cleanup.thread_id):
                    continue  # Its worker is still sending an earlier request for this thread.
                cleanup.attempts += 1
                try:
                    async with asyncio.timeout(SESSION_FINALIZATION_TIMEOUT_SECONDS):
                        residual = await self.cleanup_session_artifacts(cleanup)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    logger.warning(
                        "Agent session cleanup retry timed out for %s/%s",
                        cleanup.session_id,
                        cleanup.session_epoch,
                    )
                    residual = cleanup
                except Exception:
                    logger.exception(
                        "Agent session cleanup retry failed for %s/%s",
                        cleanup.session_id,
                        cleanup.session_epoch,
                    )
                    residual = cleanup
                self.save_cleanup(cleanup, exhausted=cleanup.attempts >= PENDING_CLEANUP_MAX_ATTEMPTS)
                if residual is None and self.pending.get(key) is cleanup:
                    self.pending.pop(key, None)
                    record = self.bridge.store.records.get(cleanup.session_id)
                    if record is not None and record.status == "closing" and record.thread_id == cleanup.thread_id:
                        self.bridge.store.put(
                            cleanup.session_id, dataclasses.replace(record, status="closed", updated_at=time.time())
                        )
                elif cleanup.attempts >= PENDING_CLEANUP_MAX_ATTEMPTS:
                    if self.pending.get(key) is cleanup:
                        self.pending.pop(key, None)
                    logger.warning(
                        "Dropping exhausted Agent session cleanup retry for %s/%s steps=%s; "
                        "periodic orphan reconciliation remains enabled",
                        cleanup.session_id,
                        cleanup.session_epoch,
                        sorted(cleanup.pending_steps),
                    )

    async def close_session_thread(
        self,
        session: AgentSession,
        cleanup: PendingSessionCleanup | None = None,
    ) -> PendingSessionCleanup | None:
        return await self.cleanup_session_artifacts(cleanup or self.pending_cleanup_for_session(session))

    async def cleanup_session_artifacts(self, cleanup: PendingSessionCleanup) -> PendingSessionCleanup | None:
        if not cleanup.pending_steps:
            return None
        owner = self.bridge.sessions.get_by_thread(cleanup.thread_id) if cleanup.thread_id is not None else None
        if owner is not None:
            duplicate_notice = (
                "notification" in cleanup.pending_steps
                and cleanup.notification_message_id is not None
                and cleanup.notification_message_id != owner.notification_message_id
            )
            if not duplicate_notice:
                return None
            cleanup.pending_steps.intersection_update({"notification"})
        if "notification" in cleanup.pending_steps:
            try:
                if cleanup.notification_message_id is not None:
                    deleted = await self.bridge.threads.bounded(
                        self.delete_session_notification(cleanup.notification_message_id),
                        SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS,
                    )
                elif cleanup.thread_id is not None:
                    deleted = await self.bridge.threads.bounded(
                        self.delete_session_notification_for_thread(cleanup.thread_id),
                        SESSION_NOTIFICATION_CLEANUP_TIMEOUT_SECONDS,
                    )
                else:
                    deleted = True
            except TimeoutError:
                deleted = False
                logger.warning("Agent session notification cleanup timed out for %s", cleanup.session_id)
            except Exception:
                deleted = False
                logger.warning("Agent session notification cleanup failed for %s", cleanup.session_id, exc_info=True)
            if deleted:
                cleanup.pending_steps.discard("notification")

        thread_steps = cleanup.pending_steps & {"disconnect_notice", "members", "archive", "leave"}
        if cleanup.thread_id is not None and thread_steps:
            try:
                thread, resolved = await self.bridge.threads.bounded(
                    self.get_thread_for_cleanup(cleanup.thread_id), THREAD_LOOKUP_TIMEOUT_SECONDS
                )
            except TimeoutError:
                thread, resolved = None, False
                logger.warning("Agent session thread lookup timed out for %s", cleanup.thread_id)
            if resolved and thread is None:
                cleanup.pending_steps.difference_update(thread_steps)
            elif thread is not None:
                await self.bridge.questions.close(cleanup.session_id, thread=thread)
                await self.close_thread(thread, cleanup.pending_steps, timeout=THREAD_CLOSE_WAIT_SECONDS)

        return cleanup if cleanup.pending_steps else None

    async def close_thread(
        self,
        thread: discord.Thread,
        pending_steps: set[CleanupStep] | None = None,
        *,
        timeout: float | None = None,
    ) -> set[CleanupStep]:
        """Ask the thread's worker for one pass over the close steps and wait up to `timeout` for it.

        The worker removes each step from the supplied set as it lands, including after the wait ends, so a caller
        keeping that set as its residual record never repeats a step. The return value is that set.
        """
        steps: set[CleanupStep] = (
            pending_steps if pending_steps is not None else {"disconnect_notice", "members", "archive", "leave"}
        )
        return await self.bridge.threads.close(thread, steps, timeout=timeout)

    async def delete_session_notification(self, message_id: int) -> bool:
        complete = True
        for channel_id in threads_module.agent_session_channel_ids(self.bridge.bot):
            try:
                channel = await threads_module.get_agent_session_channel(self.bridge.bot, channel_id)
                message = await channel.fetch_message(message_id)
                if any(session.notification_message_id == message_id for session in self.bridge.sessions.by_session.values()):
                    return True
                await self.delete_notification_message(message)
                return True
            except discord.NotFound:
                continue
            except (discord.DiscordException, ValueError):
                logger.warning("Unable to delete Agent session notification message %s", message_id)
                complete = False
        return complete

    async def delete_notification_message(self, message: discord.Message) -> None:
        """Delete a notification; from the request on (a rate limit can hold it a while), no attach adopts it."""
        self._notification_deletes[message.id] += 1
        try:
            await message.delete()
        except discord.NotFound:
            self.remember_deleted_notification(message.id)
            raise
        else:
            self.remember_deleted_notification(message.id)
        finally:
            self._notification_deletes[message.id] -= 1
            if self._notification_deletes[message.id] <= 0:
                del self._notification_deletes[message.id]

    def remember_deleted_notification(self, message_id: int) -> None:
        self._deleted_notifications[message_id] = None
        while len(self._deleted_notifications) > DELETED_NOTIFICATIONS_REMEMBERED:
            del self._deleted_notifications[next(iter(self._deleted_notifications))]

    def notification_going(self, message_id: int) -> bool:
        return message_id in self._notification_deletes or message_id in self._deleted_notifications

    async def delete_session_notification_for_thread(self, thread_id: int) -> bool:
        complete = True
        for channel_id in threads_module.agent_session_channel_ids(self.bridge.bot):
            complete = await self.delete_channel_notification_for_thread(thread_id, channel_id) and complete
        return complete

    async def delete_channel_notification_for_thread(self, thread_id: int, channel_id: int) -> bool:
        try:
            channel = await threads_module.get_agent_session_channel(self.bridge.bot, channel_id)
        except ValueError:
            logger.warning("Unable to delete Agent session notification for thread %s: channel is unavailable", thread_id)
            return False

        bot_user = self.bridge.bot.user
        if bot_user is None:
            return False

        mention = f"<#{thread_id}>"
        try:
            async for message in channel.history(limit=None):
                if message.author.id != bot_user.id:
                    continue
                if not message.content.startswith(SESSION_NOTIFICATION_PREFIXES):
                    continue
                if mention not in message.content:
                    continue
                if self.bridge.sessions.get_by_thread(thread_id) is not None:
                    return True  # A reconnect took the thread, and its notification, during the scan.
                await self.delete_notification_message(message)
                return True
        except discord.DiscordException:
            logger.warning("Unable to delete Agent session notification for thread %s", thread_id)
            return False
        return True

    async def get_thread_for_cleanup(self, thread_id: int) -> tuple[discord.Thread | None, bool]:
        channel = self.bridge.thread_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel, True
        try:
            fetched = await self.bridge.bot.fetch_channel(thread_id)
        except discord.NotFound:
            return None, True
        except discord.DiscordException:
            logger.warning("Unable to fetch Agent session thread %s for cleanup", thread_id)
            return None, False
        return (
            (fetched, True)
            if isinstance(fetched, discord.Thread) and fetched.guild.id == self.bridge.bot.config.discord.guild_id
            else (None, True)
        )
