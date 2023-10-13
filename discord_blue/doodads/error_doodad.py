import logging
import discord
from discord import app_commands
from discord.ext import commands
from discord_blue.plugzillas.discord_plug import BlueBot

logger = logging.getLogger(__name__)


class ExceptionDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        self.bot.tree.error(self.on_app_command_error)
        super().__init__()

    @commands.Cog.listener()
    async def on_app_command_error(
        self, interaction: discord.Interaction[commands.Bot], error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, commands.errors.CheckFailure):
            message = "You do not have permission to use this command"
        elif isinstance(error, commands.errors.MissingRequiredArgument):
            message = f"Missing Required Argument: {error}"
        elif isinstance(error, commands.errors.CommandInvokeError):
            message = str(error.original)
        else:
            message = f"Unknown error: {error}"
        logger.warning(f"{message} {interaction}")
        if isinstance(interaction.response, discord.InteractionResponse):
            await interaction.response.send_message(message)


async def setup(bot: BlueBot) -> None:
    await bot.add_cog(ExceptionDoodad(bot))
