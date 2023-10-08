import re
import discord
from pathlib import Path
from discord.ext import commands
from discord import app_commands
from discord.app_commands import Choice
from discord_blue.plugzillas.discord_plug import wrap_reply_lines
from discord_blue.plugzillas.discord_plug import BlueBot
from discord_blue.plugzillas.printnode_plug import PrintNodeInterface


class AssetLabelPrinterDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        super().__init__()

    async def get_schools(self, _: discord.Interaction, current: str) -> list[Choice]:
        schools = list(self.bot.config.asset_label_printer.schools.items())

        if current:
            schools = [(key, value) for key, value in schools if current.lower() in value.lower()]
        schools = schools[0:25]

        return [Choice(name=school_name, value=school_key) for school_key, school_name in schools]

    async def get_printers(self, _: discord.Interaction, _2: str) -> list[Choice]:
        return [
            Choice(name=printer_key, value=printer_id)
            for printer_key, printer_id in self.bot.config.asset_label_printer.printers.items()
        ]

    @app_commands.checks.has_role("Shiny")  # type: ignore
    @app_commands.command(name="asset-tag", description="Print an asset tag")
    @app_commands.autocomplete(school_key=get_schools)  # type: ignore
    @app_commands.autocomplete(printer_id=get_printers)  # type: ignore
    @app_commands.describe(printer_id="Printer Name", school_key="School Name", id_0="First ID", id_1="Second ID", id_2="Third ID")
    async def print_asset_tag(
        self,
        context: discord.Interaction,
        printer_id: int,
        school_key: str,
        id_0: str,
        id_1: str = "",
        id_2: str = "",
    ) -> None:
        mold_file = Path(__file__).parent / "molds" / f"{school_key}.dymo"
        if mold_file.exists():
            mold = mold_file.read_text()
        elif id_0 and id_1 and id_2:
            mold = (Path(__file__).parent / "molds" / "3.dymo").read_text()
        elif id_0 and id_1:
            mold = (Path(__file__).parent / "molds" / "2.dymo").read_text()
        else:
            mold = (Path(__file__).parent / "molds" / "1.dymo").read_text()
        mold = mold.format(
            id_0=id_0,
            id_1=id_1,
            id_2=id_2,
            name=self.bot.config.asset_label_printer.schools[school_key].upper(),
        )
        printnode = PrintNodeInterface(printer_id=printer_id)
        printnode.print_label(mold.encode("utf-8"))
        if isinstance(context.response, discord.InteractionResponse):
            await context.response.send_message(f"{printer_id=} {school_key=}")

    @app_commands.checks.has_role("Shiny")  # type: ignore
    @app_commands.command(name="add-school")
    async def add_school(self, context: discord.Interaction, school_name: str) -> None:
        school_short = re.sub(r"[\s-]+", "_", school_name)
        school_short = re.sub(r"\W+", "", school_short)
        school_short = re.sub(r"_+", "_", school_short.lower())
        self.bot.config.asset_label_printer.schools[school_short] = school_name
        self.bot.config.save()
        message = f"{context.user.mention} Added {school_name} to the list of schools\n" f"Current Schools:\n"
        if isinstance(context.response, discord.InteractionResponse):
            await context.response.send_message(message)
        message = ""
        for (
            school_key,
            school_name,
        ) in self.bot.config.asset_label_printer.schools.items():
            message += f"{school_key}: {school_name}\n"
        await wrap_reply_lines(message, context)


async def setup(bot: BlueBot) -> None:
    await bot.add_cog(AssetLabelPrinterDoodad(bot))
