import base64
import io

from printnodeapi import Gateway  # type: ignore[import]
from discord_blue.config import config


class PrintNodeInterface:
    def __init__(self, printer_id: int, api_key: str = config.printnode.api_key) -> None:
        self.api_key = api_key
        self.printer_id = printer_id
        self.gateway = self.get_gateway()

    def get_gateway(self) -> Gateway:
        return Gateway(apikey=self.api_key)

    def get_printers(self) -> list[dict]:
        gateway = self.get_gateway()
        printers = gateway.printers()
        return printers

    def print_label(self, label_pdf: io.BytesIO, quantity: int = 1):
        gateway = self.gateway
        label_base64 = base64.b64encode(label_pdf.getvalue())
        label_utf = label_base64.decode("utf-8")
        print_job = gateway.PrintJob(
            printer=self.printer_id,
            job_type="pdf",
            title="Asset Label",
            options={"copies": quantity},
            base64=label_utf,
        )
        return print_job


if __name__ == "__main__":
    pass
    # test_template = Path("../doodads/mystic_molds/ansonia.dymo").read_bytes()
    # printnode = PrintNodeInterface(printer_id=config.printnode.front_printer_id)
    # printnode.print_label(test_template)
