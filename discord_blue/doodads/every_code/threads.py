from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import discord

from discord_blue.doodads.every_code.protocol import SessionHello
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SessionThread:
    thread: discord.Thread
    notification_message_id: int


def session_thread_name(hello: SessionHello) -> str:
    repo = Path(hello.cwd).name or "session"
    branch = f" · {hello.branch}" if hello.branch else ""
    return f"{repo}{branch}"


def session_start_message(hello: SessionHello) -> str:
    branch = hello.branch or "unknown"
    return "\n".join(
        [
            "Every Code session connected",
            "",
            f"host: {hello.host_label}",
            f"cwd: `{hello.cwd}`",
            f"branch: `{branch}`",
            f"pid: `{hello.pid}`",
        ]
    )


def session_notification_message(hello: SessionHello, thread: discord.Thread) -> str:
    repo = Path(hello.cwd).name or "session"
    branch = f" on `{hello.branch}`" if hello.branch else ""
    return f"Every Code session connected for `{repo}`{branch}: <#{thread.id}>"


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
    notification = await channel.send(
        session_notification_message(hello, thread),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await thread.send(session_start_message(hello))
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
