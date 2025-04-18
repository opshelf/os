from typing import Optional, Literal
from nats.aio.client import Client as NATS
import asyncio
from perpetual_barcode_scanner.base import AbstractScanner
from pydantic import BaseModel

class LabelScannedEvent(BaseModel):
    kind: Literal["label_scanned"] = "label_scanned"
    payload: str

class ScannerWorker:
    def __init__(
        self,
        scanner: AbstractScanner,
        nats_url: str,
        scanner_event_queue: str,
    ) -> None:
        self._nats_url = nats_url
        self._scanner = scanner
        self._scanner_queue = scanner_event_queue
        self._nc = NATS()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        await self._nc.connect(servers=[self._nats_url])
        await self._scanner.initialize()

    async def run(self) -> None:
        while self._running:
            try:
                result = await self._scanner.scan()
                if result is not None:
                    print(result)
                    await self._nc.publish(
                        self._scanner_queue,
                        LabelScannedEvent(
                            payload=result
                        )
                        .model_dump_json()
                        .encode(),
                    )
            except asyncio.CancelledError:
                pass

    async def start(self) -> None:
        if not self._running:
            self._running = True
            if self._worker_task:
                self._worker_task.cancel()
            self._worker_task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            self._worker_task = None

    async def close(self) -> None:
        await self._nc.close()
        await self._scanner.shutdown()
