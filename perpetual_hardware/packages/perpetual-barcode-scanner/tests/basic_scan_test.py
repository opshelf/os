import asyncio
from perpetual_barcode_scanner import ScannerWorker
from perpetual_barcode_scanner.symcode import SymcodeScanner
from perpetual_usb_utils import get_usb_serial

async def main():
    devices = get_usb_serial()
    for device in devices:
        print(device)
    scanner = SymcodeScanner("/dev/ttyACM0", baudrate=9600)
    nats_url = "nats://localhost:4222"
    scale_event_queue = "scale.events"

    worker = ScannerWorker(scanner, nats_url, scale_event_queue)
    await worker.connect()
    await worker.start()

    try:
        # Just keep running
        while True:
            await asyncio.sleep(10)
    except KeyboardInterrupt:
        print("Stopping worker...")
    finally:
        await worker.stop()
        await worker.close()

if __name__ == "__main__":
    asyncio.run(main())
