from nats.aio.client import Client as NATS
from .events import LabelPrintEvent, LabelPrintPayload
from .worker import PrintWorkerSettings


class PrinterClient:
    def __init__(self, settings: PrintWorkerSettings) -> None:
        self._settings = settings
        self._nc = NATS()
        self._processing = True

    @property
    def settings(self) -> PrintWorkerSettings:
        return self._settings

    async def connect(self) -> None:
        await self._nc.connect(servers=[self.settings.nats_url])

    async def print(self, payload: LabelPrintPayload) -> None:
        await self._nc.publish(
            "printer_queue", LabelPrintEvent(payload=payload).model_dump_json().encode()
        )

    async def close(self) -> None:
        await self._nc.close()
