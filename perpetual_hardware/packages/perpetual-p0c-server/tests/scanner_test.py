from perpetual_p0c_server.core.scanner.utils import UsbInfo, get_usb_serial
from perpetual_p0c_server.core.scanner.symcode import SymcodeScanner
from typing import Optional
import asyncio

def get_symcode_scanner_port() -> Optional[UsbInfo]:
    for port in get_usb_serial():
        if port.description and 'Barcode' in port.description:
            return port
    return None


async def main() -> None:
    port = get_symcode_scanner_port()
    if port:
        scanner = SymcodeScanner(port.device, 115200)
        await scanner.initialize()
        while True:
            result = await scanner.scan()
            print(result)

if __name__ == '__main__':
    asyncio.run(main())

