import io
import os
import re
import cairosvg  # type: ignore[import]
import discord
from pathlib import Path
from discord.ext import commands
from discord import app_commands
from discord.app_commands import Choice
from discord_blue.plugzillas.discord.checks import has_employee_role
from discord_blue.plugzillas.discord_plug import BlueBot
from discord_blue.plugzillas.printnode_plug import PrintNodeInterface


class AssetLabelPrinterDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        super().__init__()

    async def get_schools_autocomplete(self, _: discord.Interaction[commands.Bot], current: str) -> list[Choice[str]]:
        schools = list(self.bot.config.asset_label_printer.schools.items())
        if current:
            schools = [(key, value) for key, value in schools if current.lower() in value.lower()]
        schools = schools[:25]

        return [Choice(name=school_name, value=school_key) for school_key, school_name in schools]

    async def get_printers_autocomplete(self, _: discord.Interaction[commands.Bot], _2: str) -> list[Choice[int]]:
        return [
            Choice(name=printer_key, value=printer_id)
            for printer_key, printer_id in self.bot.config.asset_label_printer.printers.items()
        ][:25]

    @has_employee_role()  # type: ignore[arg-type]
    @app_commands.command(name="asset-tag", description="Print an asset tag")
    @app_commands.autocomplete(school_key=get_schools_autocomplete, printer_id=get_printers_autocomplete)  # type: ignore[arg-type, unused-ignore, misc]
    @app_commands.describe(printer_id="Printer Name", school_key="School Name", id_0="First ID", id_1="Second ID", id_2="Third ID")
    async def print_asset_tag(
        self,
        interaction: discord.Interaction[commands.Bot],
        printer_id: int,
        school_key: str,
        id_0: str,
        id_1: str = "",
        id_2: str = "",
    ) -> None:
        mold_path = Path(__file__).parent / "molds"
        mold_file = mold_path / f"4x2_asset_{school_key}.svg"
        if mold_file.exists():
            mold = mold_file.read_text()
        elif id_0 and id_1 and id_2:
            mold = (mold_path / "4x2_asset_3x.svg").read_text()
        elif id_0 and id_1:
            mold = (mold_path / "4x2_asset_2x.svg").read_text()
        else:
            mold = (mold_path / "4x2_asset_1x.svg").read_text()
        mold = mold.format(
            id_0=id_0,
            id_1=id_1,
            id_2=id_2,
            name=self.bot.config.asset_label_printer.schools[school_key].upper(),
        )
        label_pdf = io.BytesIO()
        os.environ["FONTCONFIG_PATH"] = (mold_path / "fonts").as_posix()
        cairosvg.svg2pdf(bytestring=mold.encode("utf-8"), write_to=label_pdf)

        printnode = PrintNodeInterface(printer_id=printer_id)
        printnode.print_label(label_pdf)
        if isinstance(interaction.response, discord.InteractionResponse):
            await interaction.response.send_message(f"{printer_id=} {school_key=}")
        with open(mold_path / "test.pdf", "wb") as file:
            file.write(label_pdf.getvalue())
        exit()

    @has_employee_role()  # type: ignore[arg-type]
    @app_commands.command(name="add-school", description="Add a school to the list of schools")
    @app_commands.describe(school_name="School Name")
    async def add_school(self, interactions: discord.Interaction[commands.Bot], school_name: str) -> None:
        school_short = re.sub(r"[\s-]+", "_", school_name)
        school_short = re.sub(r"\W+", "", school_short)
        school_short = re.sub(r"_+", "_", school_short.lower())
        self.bot.config.asset_label_printer.schools[school_short] = school_name
        self.bot.config.save()
        message = f"{interactions.user.mention} Added {school_name} to the list of schools\n" f"Current Schools:\n"
        if isinstance(interactions.response, discord.InteractionResponse):
            await interactions.response.send_message(message)
        message = ""
        for (
            school_key,
            school_name,
        ) in self.bot.config.asset_label_printer.schools.items():
            message += f"{school_key}: {school_name}\n"
        await self.bot.wrap_reply_lines(message, interactions)


async def setup(bot: BlueBot) -> None:
    await bot.add_cog(AssetLabelPrinterDoodad(bot))
