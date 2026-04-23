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

    async def cog_unload(self) -> None:
        await self.bridge.stop()

async def setup(bot: BlueBot) -> None:
    await bot.add_cog(EveryCodeDoodad(bot))
