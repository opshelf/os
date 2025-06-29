import asyncio
from perpetual_barcode_scanner import ScannerWorker
from perpetual_barcode_scanner.symcode import SymcodeScanner
from perpetual_usb_utils import get_usb_serial

async def main():
    scanner = SymcodeScanner("/dev/ttyACM0", baudrate=9600)
    await scanner.initialize()
    try:
        # Just keep running
        while True:
            print(await scanner.scan())
    except KeyboardInterrupt:
        print("Stopping worker...")
    finally:
        await scanner.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
