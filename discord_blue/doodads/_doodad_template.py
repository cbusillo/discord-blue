import logging

import discord
from discord import app_commands
from discord.abc import Messageable
from discord.ext import commands

from discord_blue.plugs.discord import checks
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)


class TemplateDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        super().__init__()

    @checks.has_employee_role()  # type: ignore[arg-type]
    @app_commands.command(name="hello", description="Hello World")
    async def hello_command(self, context: discord.Interaction[commands.Bot]) -> None:
        if isinstance(context.response, discord.InteractionResponse):
            await context.response.defer()
            if isinstance(context.channel, Messageable):
                await context.channel.send(f"Hello World: {context.user.mention}")


async def setup(bot: BlueBot) -> None:
    await bot.add_cog(TemplateDoodad(bot))
