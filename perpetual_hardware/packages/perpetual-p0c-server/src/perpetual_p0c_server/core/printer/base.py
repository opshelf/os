from abc import abstractmethod, ABCMeta
import subprocess
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
import json


class AbstractPrinter(metaclass=ABCMeta):
    def print(self, filename: str) -> None:
        self._print(filename)

    @abstractmethod
    def _print(self, filename: str) -> None: ...


class MockPrinter(AbstractPrinter):
    def _print(self, filename: str) -> None:
        print("printed", filename)
        return None


class ThermalPrinter(AbstractPrinter):
    def __init__(self, printer_name: str = "ThermalPrinter") -> None:
        self._printer_name = printer_name

    def _print(self, filename: str) -> None:
        command = ["lp", "-d", self._printer_name, filename, "-o", "fit-to-page", "-o", "landscape"]
        result = subprocess.run(command, capture_output=True, text=True)
        print(result)
        if result.returncode != 0:
            raise Exception(f"Print failed: {result.stderr}")
        return None


class PrintWorker:
    def __init__(
        self,
        printer: AbstractPrinter,
        nats_url: str,
        printer_work_queue: str,
        printer_event_queue: str,
    ) -> None:
        self._nats_url = nats_url
        self._printer = printer
        self._printer_work_queue = printer_work_queue
        self._printer_event_queue = printer_event_queue
        self._nc = NATS()
        self._processing = True

    async def connect(self) -> None:
        await self._nc.connect(servers=[self._nats_url])
        await self._nc.subscribe(self._printer_work_queue, cb=self.handle_print)

    async def handle_print(self, msg: Msg) -> None:
        print("Received", msg)
        self._printer.print("~/Downloads/mock_label.pdf")
        await self._nc.publish(
            self._printer_event_queue, json.dumps({"message": "Label Printed"}).encode()
        )

    async def close(self) -> None:
        await self._nc.close()
