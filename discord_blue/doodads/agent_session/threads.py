from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import discord

from discord_blue.doodads.agent_session.messages import send_agent_session_message
from discord_blue.doodads.agent_session.protocol import SessionHello
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)
DISCORD_THREAD_NAME_LIMIT = 100
# The thread-name icon for each agent a client reports in hello's `harness`; others get none.
HARNESS_ICONS = {"claude": "\u2734\ufe0f", "codex": "\U0001f537"}
DEFAULT_BRANCH_NAMES = {"main", "master", "develop", "development", "dev", "trunk"}
SESSION_DETAILS_COMPONENT_ID = 216
# A new thread's opening line carries its identity only in component ids, which Discord never shows: one id for the
# session start text and one for that text without the PID, so discovery keeps its exact and PID-relaxed matches.
IDENTITY_ID_BASE = 2**30
IDENTITY_MARKER = "session-identity:"
IDENTITY_WITHOUT_PID_MARKER = "session-identity-without-pid:"
SESSION_START_PREFIX = "Agent session connected"
SESSION_NOTIFICATION_PREFIX = "Agent session connected for "
SESSION_NOTIFICATION_PREFIXES = (
    SESSION_NOTIFICATION_PREFIX,
    "Automated agent session connected for ",
)
SESSION_NOTIFICATION_THREAD_RE = re.compile(r"<#(?P<thread_id>\d+)>")


@dataclass(slots=True)
class SessionThread:
    thread: discord.Thread
    notification_message_id: int | None


def session_thread_name(hello: SessionHello) -> str:
    """`<icon> <repo> · <label>`: the client's title, else a non-default branch, else just the repo."""
    icon = HARNESS_ICONS.get(hello.harness or "")
    repo = session_display_name(hello)
    prefix = f"{icon} {repo}" if icon else repo
    if hello.origin and hello.origin.kind in {"launchplane", "agent_session"}:
        return _truncate_thread_name(prefix)
    if hello.title:
        # Bound a client-supplied title before any per-character work; only the start can fit anyway.
        title = " ".join(hello.title[: DISCORD_THREAD_NAME_LIMIT * 4].split())
        return _truncate_thread_name(f"{prefix} · {title}")
    branch = f" · {hello.branch}" if session_branch_is_title_worthy(hello.branch) else ""
    return _truncate_thread_name(f"{prefix}{branch}")


def distinct_thread_name(hello: SessionHello, taken: set[str]) -> str:
    """The session's thread name, told apart from other live sessions' names by branch, then a short ID."""
    name = session_thread_name(hello)
    if name not in taken:
        return name
    if hello.branch and hello.branch not in name:
        with_branch = _truncate_thread_name(f"{name} · {hello.branch}")
        if with_branch not in taken:
            return with_branch
    # The end of the session ID: Codex IDs share a time-ordered start.
    suffix = f" #{hello.session_id[-4:]}"
    return _truncate_thread_name(name, DISCORD_THREAD_NAME_LIMIT - _discord_length(suffix)) + suffix


def session_branch_is_title_worthy(branch: str | None) -> bool:
    return bool(branch and branch.lower() not in DEFAULT_BRANCH_NAMES)


def session_display_name(hello: SessionHello) -> str:
    repo = Path(hello.cwd).name or "session"
    if hello.origin and hello.origin.kind in {"launchplane", "agent_session"} and hello.origin.repository:
        repo = hello.origin.repository.rsplit("/", 1)[-1]
        issue = f"#{hello.origin.issue_number}" if hello.origin.issue_number is not None else ""
        return f"Auto {repo}{issue}"
    return repo


def _discord_length(text: str) -> int:
    # Count UTF-16 units, as a JavaScript client would, so an emoji icon never pushes a name over the limit.
    return len(text.encode("utf-16-le")) // 2


def _truncate_thread_name(name: str, limit: int = DISCORD_THREAD_NAME_LIMIT) -> str:
    # Every character is at least one UTF-16 unit, so nothing past this prefix can fit.
    name = name[: limit + 1]
    if _discord_length(name) <= limit:
        return name
    kept, units = [], 0
    for char in name:
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit - 1:
            break
        kept.append(char)
    return "".join(kept).rstrip() + "…"


def session_origin_lines(hello: SessionHello) -> list[str]:
    if not hello.origin or hello.origin.kind not in {"launchplane", "agent_session"}:
        return []
    lines = ["origin: `Launchplane automation`"]
    if hello.origin.repository:
        issue = f"#{hello.origin.issue_number}" if hello.origin.issue_number is not None else ""
        lines.append(f"source: `{hello.origin.repository}{issue}`")
    if hello.origin.issue_url:
        lines.append(f"issue: {hello.origin.issue_url}")
    if hello.origin.request_id:
        lines.append(f"request: `{hello.origin.request_id}`")
    return lines


def session_start_message(hello: SessionHello) -> str:
    branch = hello.branch or "unknown"
    return "\n".join(
        [
            "Agent session connected",
            "",
            f"session: `{hello.session_id}`",
            *session_origin_lines(hello),
            f"host: {hello.host_label}",
            f"cwd: `{hello.cwd}`",
            f"branch: `{branch}`",
            f"pid: `{hello.pid}`",
        ]
    )


def expected_session_start_messages(hello: SessionHello) -> set[str]:
    return {session_start_message(hello)}


def session_start_messages_without_pid(starts: set[str]) -> set[str]:
    return {start_without_pid for start in starts if (start_without_pid := session_start_without_pid(start))}


def session_start_without_pid(content: str) -> str | None:
    lines = content.splitlines()
    if not lines or lines[0] != SESSION_START_PREFIX:
        return None
    if not any(line.startswith("session: `") for line in lines):
        return None
    if not lines[-1].startswith("pid: `"):
        return None
    return "\n".join(lines[:-1])


def identity_id(text: str) -> int:
    return IDENTITY_ID_BASE + int(hashlib.sha256(text.encode()).hexdigest(), 16) % IDENTITY_ID_BASE


def session_identity_view(hello: SessionHello) -> discord.ui.LayoutView:
    start = session_start_message(hello)
    without_pid = session_start_without_pid(start) or start
    harness = hello.host_label.partition(" on ")[0]
    folder = Path(hello.cwd).name or hello.cwd
    line = " · ".join(part for part in (harness, folder, hello.branch) if part)
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.TextDisplay(f"-# {discord.utils.escape_markdown(line)}", id=identity_id(without_pid)))
    view.add_item(discord.ui.Separator(visible=False, id=identity_id(start)))
    return view


def session_message_markers(message: discord.Message) -> list[str]:
    markers = [message.content]
    for component in message.components:
        if isinstance(component, discord.Container) and component.id == SESSION_DETAILS_COMPONENT_ID:
            markers.extend(child.content for child in component.children if isinstance(child, discord.TextDisplay))
        elif isinstance(component, discord.TextDisplay) and (component.id or 0) >= IDENTITY_ID_BASE:
            markers.append(f"{IDENTITY_WITHOUT_PID_MARKER}{component.id}")
        elif isinstance(component, discord.SeparatorComponent) and (component.id or 0) >= IDENTITY_ID_BASE:
            markers.append(f"{IDENTITY_MARKER}{component.id}")
    return markers


def is_session_marker(marker: str) -> bool:
    return marker.startswith((SESSION_START_PREFIX, IDENTITY_MARKER, IDENTITY_WITHOUT_PID_MARKER))


def marker_matches(marker: str, starts: set[str]) -> bool:
    """An opening marker naming one of these exact session starts, PID included."""
    return marker in starts or marker in {f"{IDENTITY_MARKER}{identity_id(start)}" for start in starts}


def marker_matches_without_pid(marker: str, starts_without_pid: set[str]) -> bool:
    """An opening marker naming one of these session starts, whatever its PID."""
    return session_start_without_pid(marker) in starts_without_pid or marker in {
        f"{IDENTITY_WITHOUT_PID_MARKER}{identity_id(start)}" for start in starts_without_pid
    }


def notification_thread_id(content: str) -> int | None:
    match = SESSION_NOTIFICATION_THREAD_RE.search(content)
    if match is None:
        return None
    return int(match.group("thread_id"))


def agent_session_channel_ids(bot: BlueBot) -> tuple[int, ...]:
    main = bot.config.agent_session.channel_id or bot.config.discord.bot_channel_id
    spawned = bot.config.agent_session.spawned_channel_id
    return (main, spawned) if spawned and spawned != main else (main,)


async def get_agent_session_channel(bot: BlueBot, channel_id: int | None = None) -> discord.TextChannel:
    channel_id = channel_id if channel_id is not None else agent_session_channel_ids(bot)[0]
    if channel_id not in agent_session_channel_ids(bot):
        raise ValueError("Agent session channel is not configured")
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel) and channel.guild.id == bot.config.discord.guild_id:
        return channel
    raise ValueError(f"Agent session channel {channel_id} is not available")


CREATION_TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
CREATION_TOKEN_LENGTH = 6
CREATION_TOKEN_RE = re.compile(rf" \[[{CREATION_TOKEN_ALPHABET}]{{{CREATION_TOKEN_LENGTH}}}]$")


def has_creation_token(name: str) -> bool:
    return CREATION_TOKEN_RE.search(name) is not None


def new_creation_token() -> str:
    return "".join(secrets.choice(CREATION_TOKEN_ALPHABET) for _ in range(CREATION_TOKEN_LENGTH))


def creation_token_suffix(token: str) -> str:
    return f" [{token}]"


def tokened_thread_name(hello: SessionHello, token: str) -> str:
    """The session's thread name plus its creation token, which the first rename removes."""
    suffix = creation_token_suffix(token)
    return _truncate_thread_name(session_thread_name(hello), DISCORD_THREAD_NAME_LIMIT - _discord_length(suffix)) + suffix


class ThreadCreationStopped(Exception):
    """Shutdown interrupted a creation waiting for Discord's allowance."""


async def create_session_thread(
    bot: BlueBot,
    hello: SessionHello,
    *,
    token: str | None = None,
    settle: Callable[[discord.Thread], Awaitable[None]] | None = None,
    stopping: asyncio.Event | None = None,
) -> SessionThread:
    channel_id = bot.config.agent_session.spawned_channel_id if hello.spawned else agent_session_channel_ids(bot)[0]
    if hello.spawned and (not channel_id or channel_id == agent_session_channel_ids(bot)[0]):
        raise ValueError("Spawned sessions need a separate agent_session.spawned_channel_id")
    channel = await get_agent_session_channel(bot, channel_id)
    while True:
        if stopping is not None and stopping.is_set():
            raise ThreadCreationStopped
        try:
            thread = await channel.create_thread(
                name=tokened_thread_name(hello, token) if token else session_thread_name(hello),
                auto_archive_duration=1440,
            )
            break
        except discord.RateLimited as exc:
            # The bridge keeps its queue slot through the cooldown. Retry only creation, with the same token:
            # retrying the announcements could create a second thread after a later request failed.
            logger.info("Agent session %s waiting for a thread; retry after %.2f s", hello.session_id, exc.retry_after)
            if stopping is None:
                await asyncio.sleep(exc.retry_after)
            else:
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=exc.retry_after)
                except TimeoutError:
                    pass
                else:
                    raise ThreadCreationStopped from exc
    if settle is not None:
        await settle(thread)
    await send_agent_session_message(thread, view=session_identity_view(hello), silent=True)
    await auto_join_configured_users(bot, thread)
    return SessionThread(thread=thread, notification_message_id=None)


async def auto_join_configured_users(bot: BlueBot, thread: discord.Thread) -> None:
    if thread.parent_id in agent_session_channel_ids(bot)[1:]:
        return
    user_ids = bot.config.agent_session.auto_join_user_ids
    if not user_ids:
        return
    try:
        joined = {member.id for member in await thread.fetch_members()}
    except discord.DiscordException:
        logger.warning("Unable to inspect Agent session members; ensuring configured access", exc_info=True)
        joined = set()
    for user_id in user_ids:
        if user_id in joined:
            continue
        try:
            await thread.add_user(discord.Object(id=user_id))
        except discord.DiscordException:
            logger.warning(
                "Unable to auto-join user %s to Agent session thread %s",
                user_id,
                thread.id,
            )
