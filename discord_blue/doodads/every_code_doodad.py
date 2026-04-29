from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from discord_blue.doodads.every_code.bridge import EveryCodeBridge
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)


class EveryCodeDoodad(commands.Cog):
    code_group = app_commands.Group(name="code", description="Every Code controls")

    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        self.bridge = EveryCodeBridge(bot)

    @commands.Cog.listener("on_ready")
    async def start_bridge(self) -> None:
        if not self.bot.config.every_code.enabled:
            return
        await self.bridge.start()

    @commands.Cog.listener("on_message")
    async def route_thread_reply(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not self.bot.config.every_code.enabled:
            return
        if not self.bridge.is_operator(message.author):
            return
        delivered = await self.bridge.send_thread_reply(message)
        if delivered:
            logger.info("Delivered Every Code thread reply from %s", message.author.id)

    @commands.Cog.listener("on_raw_reaction_add")
    async def route_quick_reaction(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        if not self.bot.config.every_code.enabled:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return

        member = payload.member
        if member is None or member.bot:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            fetched = await self.bot.fetch_channel(payload.channel_id)
            channel = fetched if isinstance(fetched, discord.Thread) else None
        if not isinstance(channel, discord.Thread):
            return

        handled = await self.bridge.handle_thread_reaction(
            channel,
            payload.message_id,
            str(payload.emoji),
            member,
        )
        if handled:
            logger.info(
                "Handled Every Code quick reaction %s from %s",
                payload.emoji,
                payload.user_id,
            )

    @code_group.command(
        name="go-ahead",
        description="Ask this Every Code session to continue until it needs you.",
    )
    async def go_ahead_command(self, interaction: discord.Interaction[BlueBot]) -> None:
        if not self.bot.config.every_code.enabled:
            await interaction.response.send_message("Every Code is not enabled.", ephemeral=True)
            return

        response = await self.bridge.send_continue_autonomously(
            interaction.channel,
            interaction.user,
        )
        await interaction.response.send_message(response, ephemeral=True)

    @code_group.command(
        name="active",
        description="Show live Every Code sessions.",
    )
    async def active_command(self, interaction: discord.Interaction[BlueBot]) -> None:
        if not self.bot.config.every_code.enabled:
            await interaction.response.send_message("Every Code is not enabled.", ephemeral=True)
            return
        if not self.bridge.is_operator(interaction.user):
            await interaction.response.send_message(
                "Only Every Code operators can list sessions.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            self.bridge.active_sessions_summary(),
            ephemeral=True,
        )

    @code_group.command(
        name="status",
        description="Show the current Every Code session status.",
    )
    async def status_command(self, interaction: discord.Interaction[BlueBot]) -> None:
        if not self.bot.config.every_code.enabled:
            await interaction.response.send_message("Every Code is not enabled.", ephemeral=True)
            return

        await interaction.response.send_message(
            self.bridge.session_status_summary(interaction.channel, interaction.user),
            ephemeral=True,
        )

    @code_group.command(
        name="new",
        description="Ask this Every Code session to start a fresh chat in the same folder.",
    )
    async def new_session_command(self, interaction: discord.Interaction[BlueBot]) -> None:
        if not self.bot.config.every_code.enabled:
            await interaction.response.send_message("Every Code is not enabled.", ephemeral=True)
            return

        response = await self.bridge.send_new_session(
            interaction.channel,
            interaction.user,
        )
        await interaction.response.send_message(response, ephemeral=True)

    @code_group.command(
        name="end-session",
        description="Ask this Every Code session to disconnect.",
    )
    async def end_session_command(self, interaction: discord.Interaction[BlueBot]) -> None:
        if not self.bot.config.every_code.enabled:
            await interaction.response.send_message("Every Code is not enabled.", ephemeral=True)
            return

        response = await self.bridge.send_end_session(
            interaction.channel,
            interaction.user,
        )
        await interaction.response.send_message(response, ephemeral=True)

    async def cog_unload(self) -> None:
        await self.bridge.stop()


async def setup(bot: BlueBot) -> None:
    await bot.add_cog(EveryCodeDoodad(bot))
