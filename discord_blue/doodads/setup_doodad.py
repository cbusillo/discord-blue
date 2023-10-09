import logging
import discord
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context
from discord_blue.plugzillas.discord_plug import BlueBot
from discord_blue.plugzillas.checks import has_employee_role

logger = logging.getLogger(__name__)


class SetupDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        super().__init__()

    @commands.Cog.listener("on_ready")
    async def my_on_ready(self) -> None:
        self.bot.tree.copy_global_to(guild=self.bot.guilds[0])
        tree_sync = await self.bot.tree.sync()
        logger.info(f"Loaded {len(tree_sync)} commands")
        await self.bot.bot_channel.send(f"Loaded {len(tree_sync)} commands")

    @app_commands.checks.has_role("Shiny")  # type: ignore
    @commands.hybrid_command(name="sync")
    async def sync_command(self, context: Context) -> None:
        logger.info("Syncing commands")
        if (
            isinstance(context, Context)
            and isinstance(context.interaction, discord.Interaction)
            and isinstance(context.interaction.response, discord.InteractionResponse)
        ):
            await context.interaction.response.defer()
        self.bot.tree.clear_commands(guild=self.bot.guilds[0])
        tree_sync = await self.bot.tree.sync()
        logger.info(f"Loaded {len(tree_sync)} commands")
        await context.send(f"Loaded {len(tree_sync)} commands")

    CLEAR_CHOICES = [
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="Bot", value="bot"),
    ]

    @app_commands.checks.has_role("Shiny")  # type: ignore
    @app_commands.command(name="clear")
    @app_commands.choices(scope=CLEAR_CHOICES)
    async def clear_command(self, interaction: discord.Interaction, scope: str) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            return
        if interaction.channel.id != self.bot.bot_channel.id:
            await interaction.channel.send("Cannot use in this channel")
            return
        temp_message = await interaction.channel.send(f"Clearing messages from {scope}")
        if isinstance(interaction.response, discord.InteractionResponse):
            await interaction.response.defer()
        if scope == "bot":
            async for message in interaction.channel.history():
                if message.author == self.bot.user and message != temp_message:
                    await message.delete()
        elif scope == "all":
            async for message in interaction.channel.history():
                if message != temp_message:
                    await message.delete()
        await temp_message.delete()


@app_commands.default_permissions(administrator=True)
class SetupCommands(commands.GroupCog, name="setup"):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        super().__init__()

    permissions_group = app_commands.Group(name="permissions", description="Manage permissions")

    @permissions_group.command(name="get", description="Get role with employee permissions")  # type: ignore
    async def get_employee_role_command(self, interaction: discord.Interaction) -> None:
        if isinstance(interaction.response, discord.InteractionResponse):
            message = f"{self.bot.config.discord.employee_role_name} is the role with employee permission"
            await interaction.response.send_message(message)

    @permissions_group.command(name="set", description="Set role with employee permissions")  # type: ignore
    async def set_employee_role_command(self, context: discord.Interaction, role: discord.Role) -> None:
        self.bot.config.discord.employee_role_name = role.name
        self.bot.config.save()
        if isinstance(context.response, discord.InteractionResponse):
            await context.response.send_message("Replacing permissions")

    @has_employee_role()  # type: ignore
    @permissions_group.command(name="list_doodads", description="list all doodads")
    async def list_doodads_command(self, context: discord.Interaction) -> None:
        if isinstance(context.response, discord.InteractionResponse):
            await context.response.send_message(f"{list(Path(__file__).parent.glob('*'))=}")


async def setup(bot: BlueBot) -> None:
    await bot.add_cog(SetupDoodad(bot))
    await bot.add_cog(SetupCommands(bot))
