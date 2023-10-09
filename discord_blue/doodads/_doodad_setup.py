import logging
import discord
from pathlib import Path
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands
from discord.ext.commands import Context
from discord_blue.plugzillas.discord_plug import BlueBot
from discord_blue.plugzillas.discord import checks

logger = logging.getLogger(__name__)


class SetupDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        super().__init__()

    @commands.Cog.listener("on_ready")
    async def my_on_ready(self) -> None:
        self.bot.tree.clear_commands(guild=self.bot.guilds[0])
        self.bot.tree.copy_global_to(guild=self.bot.guilds[0])
        tree_sync = await self.bot.tree.sync()
        logger.info(f"Loaded {len(tree_sync)} commands")
        await self.bot.bot_channel.send(f"Loaded {len(tree_sync)} commands")

    @checks.has_employee_role()  # type: ignore[arg-type]
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

    @checks.has_employee_role()  # type: ignore[arg-type]
    @app_commands.command(name="clear")
    @app_commands.choices(scope=CLEAR_CHOICES)
    async def clear_command(self, interaction: discord.Interaction, scope: str) -> None:
        if not isinstance(interaction.channel, checks.TEXT_CHANNELS):
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

    @permissions_group.command(name="get", description="Get role with employee permissions")  # type: ignore[arg-type]
    async def get_employee_role_command(self, interaction: discord.Interaction) -> None:
        if isinstance(interaction.response, discord.InteractionResponse):
            message = f"{self.bot.config.discord.employee_role_name} is the role with employee permission"
            await interaction.response.send_message(message)

    @permissions_group.command(name="set", description="Set role with employee permissions")  # type: ignore[arg-type]
    async def set_employee_role_command(self, context: discord.Interaction, role: discord.Role) -> None:
        self.bot.config.discord.employee_role_name = role.name
        self.bot.config.save()
        if isinstance(context.response, discord.InteractionResponse):
            await context.response.send_message("Replacing permissions")

    doodads_group = app_commands.Group(name="doodads", description="Manage doodads")

    def get_loaded_doodads(self) -> list[str]:
        loaded_doodad_names = [
            doodad_name.lower().replace("doodads.", "")
            for doodad_name in self.bot.extensions.keys()
            if not doodad_name.startswith("doodads._")
        ]
        return loaded_doodad_names

    async def get_doodads_to_load_autocomplete(self, _: discord.Interaction, current: str) -> list[Choice]:
        loaded_doodad_names = self.get_loaded_doodads()
        doodad_names = [
            doodad_path.stem
            for doodad_path in Path(__file__).parent.glob("*_doodad.py")
            if doodad_path.stem not in loaded_doodad_names
        ]
        if current:
            doodad_names = [doodad_name for doodad_name in doodad_names if current.lower() in doodad_name.lower()]
        doodad_names = doodad_names[:25]

        return [Choice(name=doodad_name, value=doodad_name) for doodad_name in doodad_names]

    async def get_doodads_to_unload_autocomplete(self, _: discord.Interaction, current: str) -> list[Choice]:
        loaded_doodad_names = self.get_loaded_doodads()
        if current:
            loaded_doodad_names = [doodad_name for doodad_name in loaded_doodad_names if current.lower() in doodad_name.lower()]
        return [Choice(name=doodad_name, value=doodad_name) for doodad_name in loaded_doodad_names]

    @doodads_group.command(name="get_all", description="List all doodads")  # type: ignore[arg-type]
    async def list_doodads_command(self, interaction: discord.Interaction) -> None:
        if isinstance(interaction.response, discord.InteractionResponse):
            doodads = list(Path(__file__).parent.glob("*_doodad.py"))
            doodads_formatted = "\n".join([doodad.stem for doodad in doodads])
            await interaction.response.send_message(f"{doodads_formatted}")

    @doodads_group.command(name="get_loaded", description="List loaded doodads")  # type: ignore[arg-type]
    async def list_running_doodads_command(self, interaction: discord.Interaction) -> None:
        if isinstance(interaction.response, discord.InteractionResponse):
            if not self.bot.config.discord.loaded_doodads:
                await interaction.response.send_message("No doodads loaded")
                return
            doodads_formatted = "\n".join(self.bot.config.discord.loaded_doodads)
            await interaction.response.send_message(f"{doodads_formatted}")

    @doodads_group.command(name="load", description="Load a doodad")  # type: ignore[arg-type]
    @app_commands.autocomplete(doodad_name=get_doodads_to_load_autocomplete)  # type: ignore[arg-type]
    async def load_doodad_command(self, interaction: discord.Interaction, doodad_name: str) -> None:
        if isinstance(interaction.response, discord.InteractionResponse):
            await interaction.response.defer()
        await self.bot.load_extension(f"doodads.{doodad_name}")
        logger.info(f"Loaded {doodad_name}")
        tree_sync = await self.bot.tree.sync()
        logger.info(f"Loaded {len(tree_sync)} commands")
        self.bot.config.discord.loaded_doodads.append(doodad_name)
        self.bot.config.save()
        if isinstance(interaction.channel, checks.TEXT_CHANNELS):
            await interaction.followup.send(f"Loaded {doodad_name}")
            await interaction.followup.send(f"Loaded {len(tree_sync)} commands")

    @doodads_group.command(name="unload", description="Unload a doodad")  # type: ignore[arg-type]
    @app_commands.autocomplete(doodad_name=get_doodads_to_unload_autocomplete)  # type: ignore[arg-type]
    async def unload_doodad_command(self, interaction: discord.Interaction, doodad_name: str) -> None:
        if isinstance(interaction.response, discord.InteractionResponse):
            await interaction.response.defer()
        await self.bot.unload_extension(f"doodads.{doodad_name}")
        logger.info(f"Unloaded {doodad_name}")
        tree_sync = await self.bot.tree.sync()
        logger.info(f"Loaded {len(tree_sync)} commands")
        self.bot.config.discord.loaded_doodads.remove(doodad_name)
        self.bot.config.save()
        if isinstance(interaction.channel, checks.TEXT_CHANNELS):
            await interaction.followup.send(f"Unloaded {doodad_name}")
            await interaction.followup.send(f"Loaded {len(tree_sync)} commands")


async def setup(bot: BlueBot) -> None:
    await bot.add_cog(SetupDoodad(bot))
    await bot.add_cog(SetupCommands(bot))
