"""Finding, reopening or creating a session's thread for an attach, and recovering its notification.

A stored record is tried first, then the shared discovery index; a thread is created only when a complete index has
no match.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections import Counter
from functools import partial
from typing import TYPE_CHECKING, Protocol

import discord

from discord_blue.doodads.agent_session import threads as threads_module
from discord_blue.doodads.agent_session.cards import ANSWER_CARD_COMPONENT_ID, ANSWER_CONTINUATION_COMPONENT_ID
from discord_blue.doodads.agent_session.cleanup import PENDING_CLEANUP_MAX_ATTEMPTS, THREAD_CLOSE_WAIT_SECONDS, SessionCleanups
from discord_blue.doodads.agent_session.discovery import DiscoveryIndex
from discord_blue.doodads.agent_session.formatting import is_assistant_message
from discord_blue.doodads.agent_session.messages import send_assistant_message
from discord_blue.doodads.agent_session.protocol import SessionHello
from discord_blue.doodads.agent_session.sessions import AgentSessionRegistry
from discord_blue.doodads.agent_session.store import SessionStore, StoredSession
from discord_blue.doodads.agent_session.thread_worker import ThreadWorkers
from discord_blue.doodads.agent_session.threads import SESSION_NOTIFICATION_PREFIXES, SessionThread
from discord_blue.plugs.discord_plug import BlueBot

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.director_questions import QuestionCards

logger = logging.getLogger(__name__)
# Before giving up on an incomplete discovery (a failed read or listing page), refresh it again after these waits.
DISCOVERY_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0)
DISCORD_UNKNOWN_CHANNEL = 10003


class DiscoveryIncomplete(discord.DiscordException):
    """Discovery could not read every candidate, so it cannot say the session has no thread; nothing is created."""


class CandidateGone(Exception):
    """The thread discovery chose no longer exists on Discord."""

    def __init__(self, thread_id: int) -> None:
        super().__init__(thread_id)
        self.thread_id = thread_id


@dataclasses.dataclass(slots=True)
class CreationCheck:
    created_id: int
    parent_id: int | None
    guild: discord.Guild
    attempts: int = 0


class SessionThreadHooks(Protocol):
    """What thread matching needs from the bridge."""

    bot: BlueBot
    cleanups: SessionCleanups
    discovery: DiscoveryIndex
    sessions: AgentSessionRegistry
    store: SessionStore
    threads: ThreadWorkers

    questions: QuestionCards

    def bot_user_id(self) -> int | None: ...

    def record_maintenance_progress(self) -> None: ...


class SessionThreads:
    def __init__(self, bridge: SessionThreadHooks) -> None:
        self.bridge = bridge
        # Set at shutdown: a creation waiting out Discord's rate limit stops instead.
        self.creation_stopping = asyncio.Event()
        # Creations whose duplicate check could not finish, by token; maintenance retries them.
        self._creation_checks: dict[str, CreationCheck] = {}
        # Threads an attach is reopening and has not bound yet; no close may touch them meanwhile.
        self._attaching_threads: Counter[int] = Counter()

    def binding(self, thread_id: int) -> bool:
        """An attach is reopening this thread and has not bound it yet."""
        return thread_id in self._attaching_threads

    async def find_or_create_session_thread(self, hello: SessionHello) -> SessionThread:
        try:
            return await self.find_or_create_session_thread_once(hello)
        except CandidateGone as gone:
            # The thread discovery found was deleted meanwhile (a cached listing still named it): forget it and
            # resolve again, which creates a replacement if the session has no other thread.
            self.bridge.discovery.forget(gone.thread_id)
            return await self.find_or_create_session_thread_once(hello)

    def forget_stored_hint(self, session_id: str, record: StoredSession) -> None:
        if self.bridge.store.records.get(session_id) is record:
            self.bridge.store.forget(session_id)

    async def validated_stored_thread(self, session_id: str, record: StoredSession) -> discord.Thread | None:
        try:
            thread = await self.bridge.bot.fetch_channel(record.thread_id)
        except discord.Forbidden:
            # Definite lost access makes this hint unusable. Discovery already skips forbidden candidates.
            self.forget_stored_hint(session_id, record)
            self.bridge.discovery.forget(record.thread_id)
            return None
        except discord.NotFound:
            self.forget_stored_hint(session_id, record)
            self.bridge.discovery.forget(record.thread_id)
            return None
        # Other Discord failures remain failures, not evidence permitting a duplicate creation.
        parent_ids = threads_module.agent_session_channel_ids(self.bridge.bot)
        if (
            not isinstance(thread, discord.Thread)
            or thread.parent_id not in parent_ids
            or thread.guild.id != self.bridge.bot.config.discord.guild_id
            or thread.owner_id != self.bridge.bot_user_id()
        ):
            self.forget_stored_hint(session_id, record)
            return None
        opening = [
            marker
            async for message in thread.history(limit=10, oldest_first=True)
            if message.author.id == self.bridge.bot_user_id()
            for marker in threads_module.session_message_markers(message)
        ]
        expected = threads_module.session_start_without_pid(record.marker)
        if expected is None or not any(threads_module.marker_matches_without_pid(message, {expected}) for message in opening):
            self.forget_stored_hint(session_id, record)
            return None
        return thread

    async def resume_stored_thread(self, hello: SessionHello) -> SessionThread | None:
        record = self.bridge.store.records.get(hello.session_id)
        if record is None:
            return None
        expected = threads_module.session_start_without_pid(threads_module.session_start_message(hello))
        if threads_module.session_start_without_pid(record.marker) != expected:
            return None  # Retain the stored marker's session identity and metadata; PID changes are tolerated.
        mapped = self.bridge.sessions.by_thread.get(record.thread_id)
        if mapped is not None and mapped != hello.session_id:
            return None
        thread = await self.validated_stored_thread(hello.session_id, record)
        if thread is None:
            return None
        self._attaching_threads[thread.id] += 1
        try:
            if thread.archived or thread.locked or self.bridge.threads.closing(thread.id) or record.status in {"closing", "closed"}:
                thread = await self.bridge.threads.open(thread)
            notification_id = record.notification_id
            if notification_id is not None:
                try:
                    channel = await threads_module.get_agent_session_channel(self.bridge.bot, thread.parent_id)
                    notification = await channel.fetch_message(notification_id)
                    if (
                        notification.author.id != self.bridge.bot_user_id()
                        or not notification.content.startswith(SESSION_NOTIFICATION_PREFIXES)
                        or threads_module.notification_thread_id(notification.content) != thread.id
                        or self.bridge.cleanups.notification_going(notification_id)
                    ):
                        notification_id = None
                except (discord.DiscordException, ValueError):
                    notification_id = None
            self.bridge.sessions.bind_thread(hello.session_id, thread.id, notification_id)
            logger.info("Recovered Agent session %s from store in thread %s", hello.session_id, thread.id)
            return SessionThread(thread=thread, notification_message_id=notification_id)
        finally:
            self._attaching_threads[thread.id] -= 1
            if self._attaching_threads[thread.id] <= 0:
                del self._attaching_threads[thread.id]

    async def find_or_create_session_thread_once(self, hello: SessionHello) -> SessionThread:
        if stored := await self.resume_stored_thread(hello):
            return stored
        thread = await self.find_existing_session_thread(hello)
        if thread is None:
            token = threads_module.new_creation_token()
            session_thread = await threads_module.create_session_thread(
                self.bridge.bot,
                hello,
                token=token,
                settle=partial(self.delete_creation_duplicates, token),
                stopping=self.creation_stopping,
            )
            self.bridge.discovery.add(session_thread.thread, [threads_module.session_start_message(hello)])
            self.bridge.sessions.bind_thread(
                hello.session_id,
                session_thread.thread.id,
                session_thread.notification_message_id,
            )
            return session_thread

        mapped_session_id = self.bridge.sessions.by_thread.get(thread.id)
        if mapped_session_id is not None and mapped_session_id != hello.session_id:
            raise ValueError(f"Agent session thread {thread.id} is already attached")
        # The thread's worker reopens it after any close request of its last session has landed, never before. From
        # here until it is bound, the thread counts as owned, so a close that is already under way stops.
        self._attaching_threads[thread.id] += 1
        try:
            try:
                thread = await self.bridge.threads.open(thread)
            except discord.NotFound as exc:
                if exc.code == DISCORD_UNKNOWN_CHANNEL:
                    raise CandidateGone(thread.id) from exc
                raise
            notification_message_id = None
            self.bridge.sessions.bind_thread(hello.session_id, thread.id, notification_message_id)
        finally:
            self._attaching_threads[thread.id] -= 1
            if self._attaching_threads[thread.id] <= 0:
                del self._attaching_threads[thread.id]
        return SessionThread(thread=thread, notification_message_id=notification_message_id)

    async def delete_creation_duplicates(self, token: str, created: discord.Thread) -> None:
        """Close other threads of this creation: discord.py retries a create that failed with a 5xx, and Discord may
        have made the first one anyway. Runs before anything is posted, so a duplicate is still empty. If the check
        cannot finish now, maintenance retries it."""
        try:
            done = await self.check_creation_duplicates(token, created.id, created.parent_id, created.guild)
        except Exception:
            logger.warning("Unable to check for duplicate Agent session threads of %s", created.id, exc_info=True)
            done = False
        if not done:
            self._creation_checks[token] = CreationCheck(created.id, created.parent_id, created.guild)

    async def check_creation_duplicates(self, token: str, created_id: int, parent_id: int | None, guild: discord.Guild) -> bool:
        """True once every duplicate is archived and locked; False if listing or an archive failed, to retry later.

        A duplicate is archived rather than deleted: the checks below cannot rule out that someone posts in it
        before the request lands, and an archive can be undone.
        """
        suffix = threads_module.creation_token_suffix(token)
        bot_user_id = self.bridge.bot_user_id()
        try:
            active = await guild.active_threads()
        except Exception:
            logger.warning("Unable to check for duplicate Agent session threads of %s", created_id, exc_info=True)
            return False
        done = True
        for thread in active:
            # Only an empty thread the bot itself created under the same parent, carrying this creation's token: a
            # name alone can be copied, but not ownership, and a session thread has at least its marker.
            if (
                thread.id == created_id
                or thread.parent_id != parent_id
                or thread.owner_id != bot_user_id
                or thread.message_count
                or not (thread.name or "").endswith(suffix)
                or thread.id in self.bridge.sessions.by_thread
            ):
                continue
            remaining = await self.bridge.threads.close(thread, {"archive", "leave"}, timeout=THREAD_CLOSE_WAIT_SECONDS)
            if "archive" in remaining:
                logger.warning("Unable to archive duplicate Agent session thread %s yet", thread.id)
                done = False
            else:
                logger.info("Archived duplicate Agent session thread %s of %s", thread.id, created_id)
        return done

    async def retry_creation_checks(self) -> None:
        for token, check in list(self._creation_checks.items()):
            self.bridge.record_maintenance_progress()
            check.attempts += 1
            try:
                done = await self.check_creation_duplicates(token, check.created_id, check.parent_id, check.guild)
            except Exception:
                logger.warning("Unable to check for duplicate Agent session threads of %s", check.created_id, exc_info=True)
                done = False
            if done:
                self._creation_checks.pop(token, None)
            elif check.attempts >= PENDING_CLEANUP_MAX_ATTEMPTS:
                self._creation_checks.pop(token, None)
                logger.warning("Giving up on duplicate Agent session threads of %s", check.created_id)

    async def find_existing_session_thread(self, hello: SessionHello) -> discord.Thread | None:
        """The session's thread from the shared discovery index; None only when a complete index has no match.

        An incomplete index (a listing page or a candidate's read failed) is refreshed a few times; if it stays
        incomplete the attach fails with DiscoveryIncomplete rather than creating a second thread.
        """
        try:
            channels = tuple(
                [
                    await threads_module.get_agent_session_channel(self.bridge.bot, channel_id)
                    for channel_id in threads_module.agent_session_channel_ids(self.bridge.bot)
                ]
            )
        except ValueError:
            logger.warning("Unable to find reusable Agent session thread: channel is unavailable")
            raise DiscoveryIncomplete("Agent session channel is unavailable") from None
        for attempt, delay in enumerate((0.0, *DISCOVERY_RETRY_DELAYS_SECONDS)):
            if delay:
                await asyncio.sleep(delay)
            await self.bridge.discovery.fresh(channels[0], force=attempt > 0, additional_channels=channels[1:])
            if self.bridge.discovery.complete:
                return await self.match_session_thread(hello, complete=True)
        # Still incomplete: an exact match is still this session's thread (the best of those read), but neither a
        # pid-relaxed match nor "no match" can be trusted, since an unread thread might be the better or only one.
        thread = await self.match_session_thread(hello, complete=False)
        if thread is not None:
            return thread
        raise DiscoveryIncomplete(f"Agent session discovery stayed incomplete for session {hello.session_id}")

    async def match_session_thread(self, hello: SessionHello, *, complete: bool) -> discord.Thread | None:
        expected_starts = threads_module.expected_session_start_messages(hello)
        expected_starts_without_pid = threads_module.session_start_messages_without_pid(expected_starts)
        matches: list[discord.Thread] = []
        pid_relaxed_matches: list[discord.Thread] = []
        skipped_mapped = 0
        entries = self.bridge.discovery.entries()
        for entry in entries:
            mapped_session_id = self.bridge.sessions.by_thread.get(entry.thread.id)
            if mapped_session_id is not None and mapped_session_id != hello.session_id:
                skipped_mapped += 1
                continue
            opening = entry.opening or []
            if any(threads_module.marker_matches(message, expected_starts) for message in opening):
                matches.append(entry.thread)
            elif expected_starts_without_pid and any(
                threads_module.marker_matches_without_pid(message, expected_starts_without_pid) for message in opening
            ):
                pid_relaxed_matches.append(entry.thread)
        if not complete:
            pid_relaxed_matches = []
        if len(matches) == 1:
            return matches[0]
        if matches:
            # Rare (an earlier duplicate): the thread with the most conversation wins, as before.
            scored_threads = [(await self.score_session_thread(thread), thread) for thread in matches]
            _, thread = max(scored_threads, key=lambda pair: pair[0])
            return thread
        if len(pid_relaxed_matches) == 1:
            thread = pid_relaxed_matches[0]
            logger.info(
                "Reusing Agent session thread %s despite pid mismatch for cwd=%s branch=%s host=%s",
                thread.id,
                hello.cwd,
                hello.branch or "unknown",
                hello.host_label,
            )
            return thread
        if pid_relaxed_matches:
            logger.warning(
                "Not reusing Agent session thread for cwd=%s branch=%s host=%s because %s pid-relaxed candidates matched",
                hello.cwd,
                hello.branch or "unknown",
                hello.host_label,
                len(pid_relaxed_matches),
            )
        elif complete:
            logger.info(
                "No reusable Agent session thread found for cwd=%s branch=%s host=%s pid=%s "
                "after checking %s candidate(s), skipped_mapped=%s",
                hello.cwd,
                hello.branch or "unknown",
                hello.host_label,
                hello.pid,
                len(entries),
                skipped_mapped,
            )
        return None

    async def score_session_thread(self, thread: discord.Thread) -> tuple[int, int, int]:
        assistant_messages = 0
        messages = 0
        try:
            async for message in thread.history(limit=50):
                messages += 1
                if self.is_bot_assistant_message(message):
                    assistant_messages += 1
        except discord.DiscordException:
            logger.warning("Unable to score Agent session thread %s", thread.id)
        return assistant_messages, messages, thread.id

    async def backfill_latest_assistant_message(
        self,
        thread: discord.Thread,
        hello: SessionHello,
    ) -> None:
        if await self.thread_has_assistant_message(thread):
            return
        assistant_message = hello.assistant_message
        if assistant_message is None:
            return
        session = self.bridge.sessions.get(hello.session_id)
        if session is not None:
            await self.bridge.questions.send_answer(session, thread, assistant_message, replay=True)
        else:
            await send_assistant_message(thread, assistant_message)

    def is_bot_assistant_message(self, message: discord.Message) -> bool:
        bot_user = self.bridge.bot.user
        if bot_user is None or message.author.id != bot_user.id:
            return False
        if is_assistant_message(message.content):
            return True
        if any(
            isinstance(component, discord.Container) and component.id == ANSWER_CARD_COMPONENT_ID for component in message.components
        ):
            return True
        if any(
            isinstance(component, discord.Container) and component.id == ANSWER_CONTINUATION_COMPONENT_ID
            for component in message.components
        ):
            return False
        # Also recognize marked cards from before the explicit component ID.
        return any(
            isinstance(child, discord.TextDisplay) and is_assistant_message(child.content)
            for component in message.components
            if isinstance(component, discord.Container)
            for child in component.children
        )

    async def thread_has_assistant_message(self, thread: discord.Thread) -> bool:
        try:
            async for message in thread.history(limit=50):
                bot_user = self.bridge.bot.user
                if (
                    bot_user is not None
                    and message.author.id == bot_user.id
                    and any(
                        isinstance(component, discord.Container) and component.id == ANSWER_CONTINUATION_COMPONENT_ID
                        for component in message.components
                    )
                ):
                    return False
                if self.is_bot_assistant_message(message):
                    return True
        except discord.DiscordException:
            logger.warning("Unable to inspect Agent session assistant history %s", thread.id)
        return False
