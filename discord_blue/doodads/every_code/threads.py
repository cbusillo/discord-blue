from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import discord

from discord_blue.doodads.every_code.messages import send_every_code_message
from discord_blue.doodads.every_code.protocol import SessionHello
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)
DISCORD_THREAD_NAME_LIMIT = 100
DEFAULT_BRANCH_NAMES = {"main", "master", "develop", "development", "dev", "trunk"}


@dataclass(slots=True)
class SessionThread:
    thread: discord.Thread
    notification_message_id: int | None


def session_thread_name(hello: SessionHello) -> str:
    repo = session_display_name(hello)
    if hello.origin and hello.origin.kind == "every_code":
        return _truncate_thread_name(repo)
    branch = f" · {hello.branch}" if session_branch_is_title_worthy(hello.branch) else ""
    return _truncate_thread_name(f"{repo}{branch}")


def session_branch_is_title_worthy(branch: str | None) -> bool:
    return bool(branch and branch.lower() not in DEFAULT_BRANCH_NAMES)


def session_display_name(hello: SessionHello) -> str:
    repo = Path(hello.cwd).name or "session"
    if hello.origin and hello.origin.kind == "every_code" and hello.origin.repository:
        repo = hello.origin.repository.rsplit("/", 1)[-1]
        issue = f"#{hello.origin.issue_number}" if hello.origin.issue_number is not None else ""
        return f"EC {repo}{issue}"
    return repo


def _truncate_thread_name(name: str) -> str:
    if len(name) <= DISCORD_THREAD_NAME_LIMIT:
        return name
    return name[: DISCORD_THREAD_NAME_LIMIT - 1].rstrip() + "…"


def session_origin_lines(hello: SessionHello) -> list[str]:
    if not hello.origin or hello.origin.kind != "every_code":
        return []
    lines = ["origin: `Every Code automation`"]
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
            "Every Code session connected",
            "",
            *session_origin_lines(hello),
            f"host: {hello.host_label}",
            f"cwd: `{hello.cwd}`",
            f"branch: `{branch}`",
            f"pid: `{hello.pid}`",
        ]
    )


def session_notification_message(hello: SessionHello, thread: discord.Thread) -> str:
    repo = Path(hello.cwd).name or "session"
    prefix = "Every Code session connected"
    if hello.origin and hello.origin.kind == "every_code" and hello.origin.repository:
        issue = f"#{hello.origin.issue_number}" if hello.origin.issue_number is not None else ""
        repo = f"{hello.origin.repository}{issue}"
        prefix = "Every Code automated session connected"
    branch = f" on `{hello.branch}`" if session_branch_is_title_worthy(hello.branch) else ""
    return f"{prefix} for `{repo}`{branch}: <#{thread.id}>"


async def get_every_code_channel(bot: BlueBot) -> discord.TextChannel:
    channel_id = bot.config.every_code.channel_id or bot.config.discord.bot_channel_id
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    raise ValueError(f"Every Code channel {channel_id} is not available")


async def create_session_thread(bot: BlueBot, hello: SessionHello) -> SessionThread:
    channel = await get_every_code_channel(bot)
    thread = await channel.create_thread(
        name=session_thread_name(hello),
        auto_archive_duration=1440,
    )
    notification = await send_every_code_message(channel, session_notification_message(hello, thread))
    await send_every_code_message(thread, session_start_message(hello))
    await auto_join_configured_users(bot, thread)
    return SessionThread(thread=thread, notification_message_id=notification.id)


async def auto_join_configured_users(bot: BlueBot, thread: discord.Thread) -> None:
    for user_id in bot.config.every_code.auto_join_user_ids:
        try:
            await thread.add_user(discord.Object(id=user_id))
        except discord.DiscordException:
            logger.warning(
                "Unable to auto-join user %s to Every Code thread %s",
                user_id,
                thread.id,
            )
