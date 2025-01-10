from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from pydantic import BaseModel, TypeAdapter
from .events import (
    LabelPrintEvent,
    LabelPrintingCompletedEvent,
    LabelPrintingFailedEvent,
    MessagePayload,
    PrinterEvents,
)
from .printer import AbstractPrinter


class PrintWorkerSettings(BaseModel):
    nats_url: str
    printer_work_queue: str
    printer_event_queue: str


class PrintWorker:
    def __init__(self, printer: AbstractPrinter, settings: PrintWorkerSettings) -> None:
        self._printer = printer
        self._settings = settings
        self._nc = NATS()
        self._processing = True

    @property
    def printer(self) -> AbstractPrinter:
        return self._printer

    @property
    def settings(self) -> PrintWorkerSettings:
        return self._settings

    async def connect(self) -> None:
        await self._nc.connect(servers=[self.settings.nats_url])
        await self._nc.subscribe(self.settings.printer_work_queue, cb=self.handle_print)

    async def handle_print(self, msg: Msg) -> None:
        event_adapter = TypeAdapter[PrinterEvents](PrinterEvents)
        try:
            print(msg.data.decode())
            event = event_adapter.validate_json(msg.data.decode())
            print(event)
            match event:
                case LabelPrintEvent(payload=payload):
                    print(payload)
                    self._printer.print("prescription_label.png", payload)
                    await self._nc.publish(
                        self.settings.printer_event_queue,
                        LabelPrintingCompletedEvent().model_dump_json().encode(),
                    )
                case _:
                    pass
        except Exception as e:
            await self._nc.publish(
                self.settings.printer_event_queue,
                LabelPrintingFailedEvent(payload=MessagePayload(message=str(e)))
                .model_dump_json()
                .encode(),
            )

    async def close(self) -> None:
        await self._nc.close()
