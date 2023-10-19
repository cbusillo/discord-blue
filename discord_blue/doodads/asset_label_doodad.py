import io
import re
import discord
from reportlab.graphics import renderPDF
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pathlib import Path
from discord.ext import commands
from discord import app_commands
from discord.app_commands import Choice
from svglib.svglib import svg2rlg

from discord_blue.plugzillas.discord.checks import has_employee_role
from discord_blue.plugzillas.discord_plug import BlueBot
from discord_blue.plugzillas.printnode_plug import PrintNodeInterface


class AssetLabelPrinterDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        super().__init__()

    @staticmethod
    def register_fonts(font_path: Path) -> None:
        for font in font_path.glob("*.[ot]tf"):
            pdfmetrics.registerFont(TTFont(font.stem, font))

    def get_mold_str(self, mold_path: Path, school_key: str, id_0: str, id_1: str, id_2: str) -> str:
        label_size = self.bot.config.asset_label_printer.label_size
        label_size_str = "x".join(map(str, label_size))
        mold_file = mold_path / f"{label_size_str}_asset_{school_key}.svg"
        if mold_file.exists():
            return mold_file.read_text()
        elif id_0 and id_1 and id_2:
            return (mold_path / f"{label_size_str}_asset_3x.svg").read_text()
        elif id_0 and id_1:
            return (mold_path / f"{label_size_str}_asset_2x.svg").read_text()
        else:
            return (mold_path / f"{label_size_str}_asset_1x.svg").read_text()

    @staticmethod
    def list_join(seq) -> list:
        return [x for sub in seq for x in sub]

    def encode_128(self, text: str) -> str:
        code128b_mapping = dict((chr(c), [98, c + 64] if c < 32 else [c - 32]) for c in range(128))
        code128c_mapping = dict([("%02d" % i, [i]) for i in range(100)] + [("%d" % i, [100, 16 + i]) for i in range(10)])
        code128_chars = "".join(chr(c) for c in [212] + list(range(33, 126 + 1)) + list(range(200, 211 + 1)))
        code128_chars = code128_chars[:105] + "Í" + code128_chars[106:]
        code128_chars = code128_chars[:106] + "Î" + code128_chars[107:]

        if text.isdigit() and len(text) >= 2:
            codes = [105] + self.list_join(code128c_mapping[text[i : i + 2]] for i in range(0, len(text), 2))
        else:
            codes = [104] + self.list_join(code128b_mapping[c] for c in text)
        check_digit = (codes[0] + sum(i * x for i, x in enumerate(codes))) % 103
        codes.append(check_digit)
        codes.append(106)  # stop code
        full_code = "".join(code128_chars[x] for x in codes)
        return full_code

    def format_mold(self, mold: str, school_key: str, id_0: str, id_1: str, id_2: str) -> str:
        id_0_128 = self.encode_128(id_0)
        id_1_128 = self.encode_128(id_1)
        id_2_128 = self.encode_128(id_2)
        return mold.format(
            id_0=id_0,
            id_0_128=id_0_128,
            id_1=id_1,
            id_1_128=id_1_128,
            id_2=id_2,
            id_2_128=id_2_128,
            name=self.bot.config.asset_label_printer.schools[school_key].upper(),
        )

    @staticmethod
    def generate_pdf_from_svg(mold: str) -> io.BytesIO:
        label_pdf = io.BytesIO()
        drawing = svg2rlg(io.BytesIO(mold.encode("utf-8")))
        renderPDF.drawToFile(drawing, label_pdf)
        return label_pdf

    @staticmethod
    def print_pdf_to_printnode(printer_id: int, pdf: io.BytesIO):
        printnode = PrintNodeInterface(printer_id=printer_id)
        printnode.print_label(pdf)

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
        await self._print_asset_tag(interaction, printer_id, school_key, id_0, id_1, id_2)

    async def _print_asset_tag(
        self,
        interaction: discord.Interaction[commands.Bot],
        printer_id: int,
        school_key: str,
        id_0: str,
        id_1: str = "",
        id_2: str = "",
    ) -> None:
        mold_path = Path(__file__).parent / "molds"
        font_path = mold_path / "fonts"

        self.register_fonts(font_path)
        mold = self.get_mold_str(mold_path, school_key, id_0, id_1, id_2)
        mold = self.format_mold(mold, school_key, id_0, id_1, id_2)
        label_pdf = self.generate_pdf_from_svg(mold)

        if self.bot.config.debug:
            with open(mold_path / "test.pdf", "wb") as file:
                file.write(label_pdf.getvalue())

        self.print_pdf_to_printnode(printer_id, label_pdf)
        if isinstance(interaction.response, discord.InteractionResponse):
            await interaction.response.send_message(f"{printer_id=} {school_key=}")

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


if __name__ == "__main__":
    import asyncio

    class AssetLabelPrinterConfig:
        label_size = (4, 2)
        schools = {"ansonia": "Ansonia High School"}
        printers = {"front": 72706811, "back": 72693712}

    class MockBot:
        class MockConfig:
            debug = True
            asset_label_printer = AssetLabelPrinterConfig()

        config = MockConfig()

    mock_bot = MockBot()
    # noinspection PyTypeChecker
    test_instance = AssetLabelPrinterDoodad(mock_bot)
    loop = asyncio.get_event_loop()
    # noinspection PyTypeChecker,PyProtectedMember
    loop.run_until_complete(test_instance._print_asset_tag(None, 0, "ansonia", "1234567890", "1234567890", "1234567890"))
    loop.close()
