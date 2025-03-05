from abc import ABCMeta, abstractmethod
from typing import Optional, Literal
from perpetual_p0c.storage import GS1Data
from perpetual_p0c_server.core.events import GS1Raw, LabelScannedEvent, LabelScannedPayload
from pydantic import BaseModel
from enum import Enum
from nats.aio.client import Client as NATS
from typing import Optional, List, Tuple
import asyncio


class ScannerType(str, Enum):
    CAMERA = "CAMERA"
    BARCODE_READER = "BARCODE_READER"


class BaseBarCode(BaseModel):
    barcode_type: Literal[
        "None",
        "Aztec",
        "Codabar",
        "Code39",
        "Code93",
        "Code128",
        "DataBar",
        "DataBarExpanded",
        "DataBarLimited",
        "DataMatrix",
        "DXFilmEdge",
        "EAN-8",
        "EAN-13",
        "ITF",
        "MaxiCode",
        "MicroQRCode",
        "PDF417",
        "QRCode",
        "rMQRCode",
        "UPC-A",
        "UPC-E",
        "Linear-Codes",
        "Matrix-Codes",
    ]
    payload: str


class BarCodeWithImage(BaseBarCode):
    image: bytes


class AbstractScanner(metaclass=ABCMeta):
    @abstractmethod
    def location(self) -> str: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

    @abstractmethod
    async def scan(self) -> Optional[Tuple[GS1Data, str]]: ...

    @abstractmethod
    async def initialize(self) -> None: ...


class ScannerWorker:
    def __init__(
        self,
        scanners: List[AbstractScanner],
        nats_url: str,
        scanner_event_queue: str,
        scanner_events_queue: str,
    ) -> None:
        self._nats_url = nats_url
        self._scanners = scanners
        self._scanner_queue = scanner_event_queue
        self._scanner_events = scanner_events_queue
        self._nc = NATS()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        await self._nc.connect(servers=[self._nats_url])
        for scanner in self._scanners:
            await scanner.initialize()

    async def initiate_scan_wait(self) -> Optional[Tuple[GS1Data, str]]:
        tasks = [asyncio.create_task(scanner.scan()) for scanner in self._scanners]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        first_result = done.pop().result()
        for task in pending:
            task.cancel()
        return first_result

    async def run(self) -> None:
        while self._running:
            try:
                result = await self.initiate_scan_wait()
                if result:
                    print(result)
                    await self._nc.publish(
                        self._scanner_events,
                        LabelScannedEvent(
                            payload=LabelScannedPayload(gs1=result[0].raw_text, location=result[1])
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
        for scanner in self._scanners:
            await scanner.shutdown()
