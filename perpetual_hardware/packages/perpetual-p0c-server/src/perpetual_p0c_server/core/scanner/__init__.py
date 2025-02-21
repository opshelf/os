from typing import Optional, List
from perpetual_p0c_server.core.scanner.base import AbstractScanner, ScannerWorker
from perpetual_p0c_server.core.scanner.mock import MockScanner
from perpetual_p0c_server.core.scanner.symcode import SymcodeScanner
from perpetual_p0c_server.core.scanner.waveshare import WaveshareScanner
from .utils import get_symcode_ports, get_waveshare_ports
from perpetual_p0c_server.settings import get_settings

scanner_worker: Optional[ScannerWorker] = None


async def init_scanner_worker(enabled: bool) -> None:
    global scanner_worker
    if enabled:
        waveshare_ports = get_waveshare_ports()
        symcode_ports = get_symcode_ports()
        if len(waveshare_ports) or len(symcode_ports):
            waveshare_scanners: List[AbstractScanner] = [
                WaveshareScanner(port, 9600) for port in waveshare_ports
            ]
            symcode_scanners: List[AbstractScanner] = [
                SymcodeScanner(port, 115200) for port in symcode_ports
            ]
            scanners = waveshare_scanners + symcode_scanners
            scanner_worker = ScannerWorker(
                scanners, get_settings().nats.connection_string(), "scanner", "scanner_events"
            )
            await scanner_worker.connect()
            await scanner_worker.start()
        else:
            raise Exception("Unable to get scanner port")
    else:
        scanner_worker = ScannerWorker(
            [MockScanner()], get_settings().nats.connection_string(), "scanner", "scanner_events"
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
