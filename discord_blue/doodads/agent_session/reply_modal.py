from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT
from discord_blue.doodads.agent_session.cards import ANSWER_CARD_COMPONENT_ID, ANSWER_CONTINUATION_COMPONENT_ID
from discord_blue.doodads.agent_session.formatting import (
    ASSISTANT_MESSAGE_MARKER,
    LEGACY_ASSISTANT_LABEL,
    discord_text_length,
    format_user_message,
    is_assistant_message,
    strip_assistant_markers,
)
from discord_blue.doodads.agent_session.messages import send_agent_session_message
from discord_blue.doodads.agent_session.sessions import AgentSession
from discord_blue.plugs.discord_plug import BlueBot

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.bridge import AgentSessionBridge


logger = logging.getLogger(__name__)
QUOTE_LIMIT = 500


def answer_text(message: discord.Message, bot_id: int) -> str:
    if message.author.id != bot_id:
        return ""
    if is_assistant_message(message.content):
        return strip_assistant_markers(
            message.content.removeprefix(LEGACY_ASSISTANT_LABEL).replace(ASSISTANT_MESSAGE_MARKER, "")
        ).strip()
    texts: list[str] = []
    for component in message.components:
        if not isinstance(component, discord.Container):
            continue
        for child in component.children:
            if not isinstance(child, discord.TextDisplay):
                continue
            marked = is_assistant_message(child.content)
            answer_card = component.id in {ANSWER_CARD_COMPONENT_ID, ANSWER_CONTINUATION_COMPONENT_ID}
            if marked or (answer_card and child.content != "### Answer"):
                texts.append(child.content)
    return strip_assistant_markers("\n".join(texts).replace(ASSISTANT_MESSAGE_MARKER, "")).strip()


def answer_excerpt(message: discord.Message, bot_id: int) -> str:
    return answer_text(message, bot_id)[:QUOTE_LIMIT]


def reply_context_error(
    bridge: AgentSessionBridge,
    interaction: discord.Interaction,
    session: AgentSession | None,
    epoch: str,
    *,
    thread_id: int | None = None,
    reopen_action: str = "Reply about this",
) -> str | None:
    if not bridge.bot.config.agent_session.enabled:
        return "Agent sessions are not enabled."
    if not bridge.is_operator(interaction.user):
        return "Only agent session operators can reply."
    if (
        session is None
        or bridge.sessions.get_by_thread(thread_id if thread_id is not None else getattr(interaction.channel, "id", 0))
        is not session
    ):
        return "Use this on an answer in a bound session thread."
    if session.session_epoch != epoch or not session.acknowledged:
        return f"The session reconnected; open {reopen_action} again."
    return bridge.controls.dispatch_error(session, "reply")


class ReplyAboutModal(discord.ui.Modal):
    def __init__(
        self, bridge: AgentSessionBridge, session: AgentSession, message: discord.Message, user_id: int, excerpt: str
    ) -> None:
        super().__init__(title="Reply about this", timeout=600)
        self.bridge = bridge
        self.session = session
        self.epoch = session.session_epoch
        self.source_id = message.id
        self.user_id = user_id
        self.excerpt = excerpt
        self.jump_url = message.jump_url
        self.older = message.created_at < session.attached_at
        self.submitted = False
        self.lock = asyncio.Lock()
        identity = discord.utils.escape_markdown(session.thread_name or session.hello.title or session.session_id)[:100]
        age = " · Earlier context" if self.older else ""
        self.add_item(discord.ui.TextDisplay(f"**{identity}{age}**\n{self.quote()}\n[Open answer]({self.jump_url})"))
        context_size = discord_text_length(format_user_message(self.reply_text("x"))) - 1
        self.reply: discord.ui.TextInput[ReplyAboutModal] = discord.ui.TextInput[ReplyAboutModal](
            style=discord.TextStyle.paragraph, max_length=(DISCORD_MESSAGE_LIMIT - context_size) // 2
        )
        self.add_item(discord.ui.Label(text="Your reply", component=self.reply))

    def quote(self) -> str:
        return "\n".join(f"> {line}" for line in self.excerpt.splitlines())

    def reply_text(self, reply: str) -> str:
        age = " (earlier context)" if self.older else ""
        return f"About this answer{age}: {self.jump_url}\n{self.quote()}\n\n{reply.strip()}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        error = reply_context_error(self.bridge, interaction, self.session, self.epoch)
        if error is not None or interaction.user.id != self.user_id:
            await interaction.response.send_message(error or "This reply form belongs to another operator.", ephemeral=True)
            return
        if not self.reply.value.strip():
            await interaction.response.send_message("Write a reply, then open Reply about this again.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            if self.submitted:
                await interaction.followup.send("This reply form has already been submitted.", ephemeral=True)
                return
            thread = self.bridge.thread_channel(self.session.thread_id or 0)
            if not isinstance(thread, discord.Thread):
                await interaction.followup.send("The session thread is unavailable; reply was not sent.", ephemeral=True)
                return
            try:
                source = await thread.fetch_message(self.source_id)
            except discord.DiscordException:
                await interaction.followup.send("The answer is unavailable; reply was not sent.", ephemeral=True)
                return
            bot_user = self.bridge.bot.user
            if bot_user is None or answer_excerpt(source, bot_user.id) != self.excerpt:
                await interaction.followup.send("The answer changed; open Reply about this again.", ephemeral=True)
                return
            if error := reply_context_error(self.bridge, interaction, self.session, self.epoch):
                await interaction.followup.send(error, ephemeral=True)
                return
            self.submitted = True
            text = self.reply_text(self.reply.value)
            try:
                message = await send_agent_session_message(thread, format_user_message(text))
            except discord.DiscordException:
                await interaction.followup.send("Discord could not post the reply; it was not sent to the session.", ephemeral=True)
                return
            error = reply_context_error(self.bridge, interaction, self.session, self.epoch)
            if error is None:
                error = await self.bridge.controls.send_reply(self.session, thread, interaction.user, text, message.id)
            if error is not None:
                try:
                    await message.reply(error, mention_author=False)
                except discord.DiscordException:
                    logger.warning("Unable to post contextual reply failure for message %s", message.id)
            await interaction.followup.send(error or "Reply queued for this session.", ephemeral=True)


async def open_reply_modal(bridge: AgentSessionBridge, interaction: discord.Interaction[BlueBot], message: discord.Message) -> None:
    session = bridge.sessions.get_by_thread(getattr(interaction.channel, "id", 0))
    error = reply_context_error(bridge, interaction, session, session.session_epoch if session is not None else "")
    if error is not None:
        await interaction.response.send_message(error, ephemeral=True)
        return
    bot_user = bridge.bot.user
    excerpt = answer_excerpt(message, bot_user.id) if bot_user is not None else ""
    if not excerpt or message.channel.id != getattr(interaction.channel, "id", None):
        await interaction.response.send_message("Choose one of this bot's answer messages in this session thread.", ephemeral=True)
        return
    assert session is not None
    await interaction.response.send_modal(ReplyAboutModal(bridge, session, message, interaction.user.id, excerpt))
