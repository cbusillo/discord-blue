from __future__ import annotations

import asyncio
import logging
from io import BytesIO

import discord

from discord_blue.doodads.agent_session.cards import ANSWER_FILENAME, assistant_answer_cards, notice_card
from discord_blue.doodads.agent_session.formatting import ASSISTANT_MESSAGE_MARKER, strip_assistant_markers

logger = logging.getLogger(__name__)
MISSING_MANAGE_MESSAGES_DESTINATIONS: set[int] = set()
MISSING_MANAGE_MESSAGES_NOTICE_LOCK = asyncio.Lock()


def agent_session_allowed_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions.none()


def can_suppress_embeds(destination: discord.abc.Messageable) -> bool:
    guild = getattr(destination, "guild", None)
    bot_member = getattr(guild, "me", None)
    permissions_for = getattr(destination, "permissions_for", None)
    if bot_member is None or not callable(permissions_for):
        return False
    permissions = permissions_for(bot_member)
    return bool(getattr(permissions, "manage_messages", False))


async def send_agent_session_message(
    destination: discord.abc.Messageable,
    content: str | None = None,
    *,
    view: discord.ui.View | discord.ui.LayoutView | None = None,
    silent: bool | None = None,
) -> discord.Message:
    """Post a bot message that is not an assistant answer; any assistant marker in its text is removed."""
    return await _send(destination, None if content is None else strip_assistant_markers(content), view=view, silent=silent)


async def send_assistant_message(destination: discord.abc.Messageable, message: str) -> discord.Message | None:
    if not strip_assistant_markers(message.replace(ASSISTANT_MESSAGE_MARKER, "")).strip():
        return None
    views, attached = assistant_answer_cards(message)
    last_message = None
    for index, view in enumerate(views):
        if attached and index == len(views) - 1:
            file = discord.File(BytesIO(strip_assistant_markers(message).encode()), filename=ANSWER_FILENAME)
            try:
                last_message = await destination.send(
                    file=file, view=view, allowed_mentions=agent_session_allowed_mentions(), silent=index > 0
                )
                continue
            except discord.Forbidden:
                logger.warning("Unable to attach the complete Agent session answer; keeping questions inline")
                container = view.children[0]
                assert isinstance(container, discord.ui.Container)
                container.remove_item(container.children[-1])
                note = container.children[-1]
                assert isinstance(note, discord.ui.TextDisplay)
                note.content = "-# Attachment unavailable · Full answer remains in the terminal"
            finally:
                file.close()
        last_message = await destination.send(view=view, allowed_mentions=agent_session_allowed_mentions(), silent=index > 0)
    return last_message


async def _send(
    destination: discord.abc.Messageable,
    content: str | None,
    *,
    view: discord.ui.View | discord.ui.LayoutView | None,
    silent: bool | None = None,
) -> discord.Message:
    if isinstance(view, discord.ui.LayoutView):
        return await destination.send(
            view=view, allowed_mentions=agent_session_allowed_mentions(), silent=True if silent is None else silent
        )
    if can_suppress_embeds(destination):
        try:
            if view is None:
                return await destination.send(
                    content,
                    allowed_mentions=agent_session_allowed_mentions(),
                    suppress_embeds=True,
                    silent=False if silent is None else silent,
                )

            return await destination.send(
                content,
                allowed_mentions=agent_session_allowed_mentions(),
                suppress_embeds=True,
                view=view,
                silent=False if silent is None else silent,
            )
        except discord.Forbidden:
            logger.warning("Unable to suppress Agent session embeds despite apparent Manage Messages permission")

    if view is None:
        message = await destination.send(
            content,
            allowed_mentions=agent_session_allowed_mentions(),
            silent=False if silent is None else silent,
        )
    else:
        message = await destination.send(
            content,
            allowed_mentions=agent_session_allowed_mentions(),
            view=view,
            silent=False if silent is None else silent,
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
                "Agent session could not suppress link previews because I am missing the `Manage Messages` permission here.",
                allowed_mentions=agent_session_allowed_mentions(),
            )
        except discord.DiscordException:
            logger.warning("Unable to post Agent session missing Manage Messages notice in %s", destination_id)
            return
        MISSING_MANAGE_MESSAGES_DESTINATIONS.add(destination_id)


async def edit_agent_session_message(message: discord.Message, *, content: str) -> discord.Message:
    if message_uses_components_v2(message):
        return await message.edit(content=None, allowed_mentions=agent_session_allowed_mentions(), view=notice_card(content))
    return await message.edit(content=strip_assistant_markers(content), allowed_mentions=agent_session_allowed_mentions(), view=None)


def message_uses_components_v2(message: discord.Message) -> bool:
    return bool(getattr(getattr(message, "flags", None), "components_v2", False)) or any(
        isinstance(component, discord.Container) for component in getattr(message, "components", [])
    )
