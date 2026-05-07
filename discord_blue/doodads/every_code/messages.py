from __future__ import annotations

import discord


def every_code_allowed_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions.none()


async def send_every_code_message(
    destination: discord.abc.Messageable,
    content: str | None = None,
    *,
    view: discord.ui.View | None = None,
) -> discord.Message:
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


async def edit_every_code_message(message: discord.Message, *, content: str) -> discord.Message:
    return await message.edit(content=content, allowed_mentions=every_code_allowed_mentions(), suppress=True, view=None)
