from typing import Optional
from perpetual_barcode_scanner import ScannerWorker
from perpetual_barcode_scanner.symcode import SymcodeScanner, get_symcode_port
import logging

scanner_worker: Optional[ScannerWorker] = None


async def init_scanner_worker() -> None:
    global scanner_worker
    logging.info("Initialize Scale")
    port = get_symcode_port()
    scanner = SymcodeScanner(port.device, 9600)
    nats_url = "nats://localhost:4222"
    scanner_event_queue = "scanner.events"
    worker = ScannerWorker(scanner, nats_url, scanner_event_queue)
    await worker.connect()
    logging.info("Scanner Connected")
    await worker.start()
    logging.info("Scanner Started")
    scanner_worker = worker


async def stop_scanner_worker() -> None:
    global robot_worker
    if scanner_worker:
        await scanner_worker.close()



async def get_scanner_worker() -> ScannerWorker:
    if scanner_worker:
        return scanner_worker
    raise ValueError("Scanner Worker was Not Initialized")
