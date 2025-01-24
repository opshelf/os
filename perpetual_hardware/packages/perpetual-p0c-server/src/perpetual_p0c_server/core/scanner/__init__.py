from typing import Optional
from perpetual_p0c_server.core.scanner.base import AbstractScanner, ScannerWorker
from perpetual_p0c_server.core.scanner.mock import MockScanner
from perpetual_p0c_server.core.scanner.waveshare import WaveshareScanner
from .utils import get_waveshare_port

scanner_worker: Optional[ScannerWorker] = None


async def init_scanner_worker(enabled: bool) -> None:
    global scanner_worker
    if enabled:
        port = get_waveshare_port()
        if port:
            scanner_worker = ScannerWorker(
                WaveshareScanner(port, 9600), "nats://localhost:4222", "scanner", "scanner_events"
            )
            await scanner_worker.connect()
            await scanner_worker.start()
        else:
            raise Exception("Unable to get scanner port")
    else:
        scanner_worker = ScannerWorker(
            MockScanner(), "nats://localhost:4222", "scanner", "scanner_events"
        )
        await scanner_worker.connect()
        await scanner_worker.start()


async def stop_scanner_worker() -> None:
    global scanner_worker
    if scanner_worker:
        await scanner_worker.stop()
        await scanner_worker.close()


async def get_scanner_worker() -> ScannerWorker:
    if scanner_worker:
        return scanner_worker
    raise ValueError("Scanner Worker was Not Initialized")
