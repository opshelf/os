from abc import abstractmethod, ABCMeta
import subprocess
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from pydantic import TypeAdapter
from perpetual_p0c_server.core.events import (
    LabelPrintEvent,
    LabelPrintPayload,
    LabelPrintingCompletedEvent,
    LabelPrintingFailedEvent,
    MessagePayload,
    UiEvents,
)
from .label_maker import LabelInformation, LabelMaker


class AbstractPrinter(metaclass=ABCMeta):
    def print(self, filename: str, payload: LabelPrintPayload) -> None:
        self._print(filename, payload)

    @abstractmethod
    def _print(self, filename: str, payload: LabelPrintPayload) -> None: ...


class MockPrinter(AbstractPrinter):
    def _print(self, filename: str, payload: LabelPrintPayload) -> None:
        print("printed", filename, payload)
        return None


class ThermalPrinter(AbstractPrinter):
    def __init__(self, printer_name: str = "ThermalPrinter") -> None:
        self._printer_name = printer_name
        self._label_maker = LabelMaker()

    def _print(self, filename: str, payload: LabelPrintPayload, label_information: LabelInformation = LabelInformation()) -> None:
        self._label_maker.create_label(payload, label_information)
        command = ["lp", "-d", self._printer_name, filename, "-o", "fit-to-page"]
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
        event_adapter = TypeAdapter[UiEvents](UiEvents)
        try:
            print(msg.data.decode())
            event = event_adapter.validate_json(msg.data.decode())
            print(event)
            match event:
                case LabelPrintEvent(payload=payload):
                    print(payload)
                    # self._printer.print("~/Downloads/mock_label.pdf", payload)
                    self._printer.print("prescription_label.png", payload)
                    await self._nc.publish(
                        self._printer_event_queue,
                        LabelPrintingCompletedEvent().model_dump_json().encode(),
                    )
                case _:
                    pass
        except Exception as e:
            await self._nc.publish(
                self._printer_event_queue,
                LabelPrintingFailedEvent(payload=MessagePayload(message=str(e)))
                .model_dump_json()
                .encode(),
            )

    async def close(self) -> None:
        await self._nc.close()
