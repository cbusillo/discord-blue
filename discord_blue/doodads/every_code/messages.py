from __future__ import annotations

import asyncio
import logging

import discord

logger = logging.getLogger(__name__)
MISSING_MANAGE_MESSAGES_DESTINATIONS: set[int] = set()
MISSING_MANAGE_MESSAGES_NOTICE_LOCK = asyncio.Lock()


def every_code_allowed_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions.none()


def can_suppress_embeds(destination: discord.abc.Messageable) -> bool:
    guild = getattr(destination, "guild", None)
    bot_member = getattr(guild, "me", None)
    permissions_for = getattr(destination, "permissions_for", None)
    if bot_member is None or not callable(permissions_for):
        return False
    permissions = permissions_for(bot_member)
    return bool(getattr(permissions, "manage_messages", False))


async def send_every_code_message(
    destination: discord.abc.Messageable,
    content: str | None = None,
    *,
    view: discord.ui.View | None = None,
) -> discord.Message:
    if can_suppress_embeds(destination):
        try:
            if view is None:
                return await destination.send(
                    content,
                    allowed_mentions=every_code_allowed_mentions(),
                    suppress_embeds=True,
                )

            return await destination.send(
                content,
                allowed_mentions=every_code_allowed_mentions(),
                suppress_embeds=True,
                view=view,
            )
        except discord.Forbidden:
            logger.warning("Unable to suppress Every Code embeds despite apparent Manage Messages permission")

    if view is None:
        message = await destination.send(
            content,
            allowed_mentions=every_code_allowed_mentions(),
        )
    else:
        message = await destination.send(
            content,
            allowed_mentions=every_code_allowed_mentions(),
            view=view,
        )

    await notify_missing_manage_messages(destination)
    return message


async def notify_missing_manage_messages(destination: discord.abc.Messageable) -> None:
    destination_id = getattr(destination, "id", id(destination))
    async with MISSING_MANAGE_MESSAGES_NOTICE_LOCK:
        if destination_id in MISSING_MANAGE_MESSAGES_DESTINATIONS:
            return
        try:
            await destination.send(
                "Every Code could not suppress link previews because I am missing the `Manage Messages` permission here.",
                allowed_mentions=every_code_allowed_mentions(),
            )
        except discord.DiscordException:
            logger.warning("Unable to post Every Code missing Manage Messages notice in %s", destination_id)
            return
        MISSING_MANAGE_MESSAGES_DESTINATIONS.add(destination_id)


async def edit_every_code_message(message: discord.Message, *, content: str) -> discord.Message:
    return await message.edit(content=content, allowed_mentions=every_code_allowed_mentions(), view=None)
