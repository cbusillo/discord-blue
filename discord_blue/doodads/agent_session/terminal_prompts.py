from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT
from discord_blue.doodads.agent_session.formatting import INJECTED_BLOCK, strip_assistant_markers
from discord_blue.doodads.agent_session.messages import agent_session_allowed_mentions

if TYPE_CHECKING:
    from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)
WEBHOOK_NAME = "Discord Blue terminal prompts"
TERMINAL_SUBTEXT = "\n-# from terminal"
CLIPPED_TERMINAL_SUBTEXT = f"{TERMINAL_SUBTEXT} · Full prompt remains in the terminal"


def is_operator_prompt(text: str) -> bool:
    remaining = text.lstrip()
    while envelope := INJECTED_BLOCK.match(remaining):
        if envelope.group(1) != "system-reminder":
            return False
        remaining = remaining[envelope.end() :].lstrip()
    return True


class TerminalPrompts:
    def __init__(self, bot: BlueBot, is_operator: Callable[[discord.User | discord.Member], bool]) -> None:
        self.bot = bot
        self.is_operator = is_operator
        self.webhooks: dict[int, discord.Webhook] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.warned: set[int] = set()

    def warn_once(self, thread: discord.Thread, reason: str) -> None:
        channel_id = thread.parent_id or thread.id
        if channel_id not in self.warned:
            self.warned.add(channel_id)
            logger.warning("Terminal prompt webhook unavailable in channel %s: %s; using bot mirror", channel_id, reason)

    async def operator(self, thread: discord.Thread) -> discord.Member | None:
        user_ids = set(self.bot.config.agent_session.auto_join_user_ids)
        if len(user_ids) != 1:
            self.warn_once(thread, "auto_join_user_ids must identify one session operator")
            return None
        user_id = next(iter(user_ids))
        member = thread.guild.get_member(user_id) or await thread.guild.fetch_member(user_id)
        if member.bot or not self.is_operator(member):
            self.warn_once(thread, "configured member is not a session operator")
            return None
        return member

    async def webhook(self, parent: discord.TextChannel, thread: discord.Thread) -> discord.Webhook:
        async with self.locks.setdefault(parent.id, asyncio.Lock()):
            if parent.id in self.webhooks:
                return self.webhooks[parent.id]
            for hook in await parent.webhooks():
                if (
                    hook.name == WEBHOOK_NAME
                    and hook.type == discord.WebhookType.incoming
                    and hook.user is not None
                    and thread.guild.me is not None
                    and hook.user.id == thread.guild.me.id
                    and hook.token is not None
                ):
                    self.webhooks[parent.id] = hook
                    return hook
            hook = await parent.create_webhook(name=WEBHOOK_NAME, reason="Mirror session terminal prompts")
            self.webhooks[parent.id] = hook
            return hook

    async def send(self, thread: discord.Thread, text: str) -> bool:
        parent = thread.parent
        if not isinstance(parent, discord.TextChannel):
            return False
        if thread.guild.me is None or not parent.permissions_for(thread.guild.me).manage_webhooks:
            self.warn_once(thread, "bot lacks Manage Webhooks")
            return False
        try:
            member = await self.operator(thread)
            if member is None:
                return False
            hook = await self.webhook(parent, thread)
        except discord.HTTPException as error:
            self.warn_once(thread, f"webhook/member lookup failed (HTTP {error.status})")
            return False
        content = strip_assistant_markers(text)
        subtext = TERMINAL_SUBTEXT
        if len(content) + len(subtext) > DISCORD_MESSAGE_LIMIT:
            subtext = CLIPPED_TERMINAL_SUBTEXT
            content = content[: DISCORD_MESSAGE_LIMIT - len(subtext) - 1] + "…"
        try:
            await hook.send(
                content + subtext,
                thread=thread,
                username=member.display_name,
                avatar_url=str(member.display_avatar),
                wait=True,
                allowed_mentions=agent_session_allowed_mentions(),
                suppress_embeds=True,
            )
        except discord.HTTPException as error:
            self.webhooks.pop(parent.id, None)
            self.warn_once(thread, f"webhook send failed (HTTP {error.status})")
            return False
        return True
