import base64
from printnodeapi import Gateway


class PrintNodeInterface:
    def __init__(self, api_key: str, printer_id: int) -> None:
        self.api_key = api_key
        self.printer_id = printer_id
        self.gateway = self.get_gateway()

    def get_gateway(self) -> Gateway:
        return Gateway(apikey=self.api_key)

    def get_printers(self):
        gateway = self.get_gateway()
        printers = gateway.printers()
        return printers

    def print_label(self, label: base64, quantity: int = 1):
        gateway = self.gateway
        label_str = label.decode('utf-8')
        print_job = gateway.PrintJob(
            printer=self.printer_id,
            job_type="raw",
            title="Asset Label",
            options={"copies": quantity},
            base64=label_str
        )
        return print_job


from config import Config
from pathlib import Path

if __name__ == '__main__':
    config = Config()
    test_template = Path("mystic_molds/ansonia.dymo").read_bytes()

    printnode = PrintNodeInterface(config.printnode.api_key, printer_id=config.printnode.front_printer_id)
    printnode.print_label(base64.b64encode(test_template))
