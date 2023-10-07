import discord
from discord.ext import commands
from discord import app_commands


class AssetLabelPrinterDoodad(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="asset-tag")
    @app_commands.choices(
        printer_name=[
            app_commands.Choice(name="Front", value="front"),
            app_commands.Choice(name="Back", value="back"),
        ]
    )
    @app_commands.choices(
        school_name=[
            app_commands.Choice(name="School1", value="school1"),
            app_commands.Choice(name="School2", value="school2"),
        ]
    )
    async def print_asset_tag(self, ctx: discord.Interaction, printer_name: str, school_name: str) -> None:
        await ctx.channel.send(f"{printer_name=} {school_name=}")
        # await ctx.response.send_message("Print command")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AssetLabelPrinterDoodad(bot))
