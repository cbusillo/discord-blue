import re
import discord
from pathlib import Path
from discord.ext import commands
from discord import app_commands
from discord.app_commands import Choice
from discord_blue.plugzillas.discord_plug import BlueBot
from discord_blue.plugzillas.printnode_plug import PrintNodeInterface


class AssetLabelPrinterDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        super().__init__()

    async def get_schools(self, _: discord.Interaction, _2: str) -> list[Choice[str]]:
        print(self.bot.config.asset_label_printer.schools.items())
        return [
            Choice(name=school_name, value=school_key)
            for school_key, school_name in self.bot.config.asset_label_printer.schools.items()
        ]

    async def get_printers(self, _: discord.Interaction, _2: str) -> list[Choice[str]]:
        print(self.bot.config.asset_label_printer.printers.items())
        return [
            Choice(name=printer_key, value=printer_id)
            for printer_key, printer_id in self.bot.config.asset_label_printer.printers.items()
        ]

    @commands.has_role("Shiny")
    @app_commands.command(name="asset-tag")
    @app_commands.autocomplete(school_key=get_schools)
    @app_commands.autocomplete(printer_id=get_printers)
    async def print_asset_tag(
        self,
        ctx: discord.Interaction,
        printer_id: int,
        school_key: str,
        id_0: str,
        id_1: str = "",
        id_2: str = "",
    ) -> None:
        mold = (Path(__file__).parent / "molds" / f"{school_key}.dymo").read_text()
        mold = mold.format(id_0=id_0, id_1=id_1, id_2=id_2)
        printnode = PrintNodeInterface(printer_id=printer_id)
        printnode.print_label(mold.encode("utf-8"))
        await ctx.response.send_message(f"{printer_id=} {school_key=}")

    @commands.has_role("Shiny")
    @app_commands.command(name="add-school")
    async def add_school(self, ctx: discord.Interaction, school_name: str) -> None:
        school_name_short = re.sub(r"[\s-]+", "_", school_name.lower())
        self.bot.config.asset_label_printer.schools[school_name_short] = school_name
        self.bot.config.save()
        message = f"Added {school_name} to the list of schools\n" f"Current Schools:\n"
        for (
            school_key,
            school_name,
        ) in self.bot.config.asset_label_printer.schools.items():
            message += f"{school_key}: {school_name}\n"
        await ctx.response.send_message(message)


async def setup(bot: BlueBot) -> None:
    await bot.add_cog(AssetLabelPrinterDoodad(bot))
