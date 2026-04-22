from __future__ import annotations

from pathlib import Path

import discord

from discord_blue.every_code.protocol import SessionHello
from discord_blue.plugs.discord_plug import BlueBot


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


async def get_every_code_channel(bot: BlueBot) -> discord.TextChannel:
    channel_id = bot.config.every_code.channel_id or bot.config.discord.bot_channel_id
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    raise ValueError(f"Every Code channel {channel_id} is not available")


async def create_session_thread(bot: BlueBot, hello: SessionHello) -> discord.Thread:
    channel = await get_every_code_channel(bot)
    thread = await channel.create_thread(
        name=session_thread_name(hello),
        auto_archive_duration=1440,
    )
    await thread.send(session_start_message(hello))
    return thread
