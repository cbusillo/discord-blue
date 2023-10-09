import logging
import discord
from discord import app_commands
from discord.ext import commands
from discord_blue.plugzillas.discord_plug import BlueBot
from discord_blue.plugzillas.checks import has_employee_role

logger = logging.getLogger(__name__)


class TemplateDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        super().__init__()

    @has_employee_role()  # type: ignore
    @app_commands.command(name="hello", description="Hello World")
    async def hello_command(self, context: discord.Interaction) -> None:
        if isinstance(context.response, discord.InteractionResponse):
            await context.response.send_message(f"Hello World: {context.user.mention}")


async def setup(bot: BlueBot) -> None:
    await bot.add_cog(TemplateDoodad(bot))
