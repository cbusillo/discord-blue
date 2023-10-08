import functools
import logging
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands

from discord_blue.plugzillas.discord_plug import BlueBot

logger = logging.getLogger(__name__)


class SetupDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        super().__init__()

    @commands.Cog.listener("on_ready")
    async def on_ready(self) -> None:
        self.bot.tree.copy_global_to(guild=self.bot.guilds[0])
        tree_sync = await self.bot.tree.sync(guild=self.bot.guilds[0])
        logger.info(f"Loaded {len(tree_sync)} commands")
        await self.bot.bot_channel.send(f"Loaded {len(tree_sync)} commands")

    @app_commands.checks.has_role("Shiny")
    @app_commands.command(name="clear")
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Bot", value="bot"),
            app_commands.Choice(name="All", value="all"),
        ]
    )
    async def clear_command(self, context: discord.Interaction, scope: str) -> None:
        """Clear all or bot messages in bot-config"""

        if not isinstance(context.channel, discord.TextChannel):
            return
        if context.channel.id != self.bot.bot_channel.id:
            await context.channel.send("Cannot use in this channel")
            return
        temp_message = await context.channel.send(f"Clearing messages from {scope}")
        if isinstance(context.response, discord.InteractionResponse):
            await context.response.defer()
        if scope == "bot":
            async for message in context.channel.history():
                if message.author == self.bot.user and message != temp_message:
                    await message.delete()
        elif scope == "all":
            async for message in context.channel.history():
                if message != temp_message:
                    await message.delete()
        await temp_message.delete()


async def setup(bot: BlueBot) -> None:
    await bot.add_cog(SetupDoodad(bot))
