# async_barcode_reader.py
import asyncio
import serial_asyncio
import serial.tools.list_ports

def choose_port(preferred=None):
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        raise RuntimeError("No serial ports found.")
    if preferred:
        for p in ports:
            if preferred in (p.device, p.description, p.manufacturer or ""):
                return p.device
    return ports[0].device

class BarcodeProtocol(asyncio.Protocol):
    def __init__(self, on_barcode):
        super().__init__()
        self.on_barcode = on_barcode
        self._buffer = bytearray()

    def data_received(self, data):
        self._buffer.extend(data)
        # handle line termination (\n or \r\n)
        while True:
            if b'\n' in self._buffer:
                line, _, rest = self._buffer.partition(b'\n')
                self._buffer = rest
                self._handle_line(line)
            elif b'\r' in self._buffer:
                line, _, rest = self._buffer.partition(b'\r')
                self._buffer = rest
                self._handle_line(line)
            else:
                break

    def _handle_line(self, raw):
        try:
            s = raw.decode('utf-8', errors='replace').strip()
        except Exception:
            s = raw.hex()
        if s:
            asyncio.create_task(self.on_barcode(s))

async def handle_barcode(barcode):
    print("Async Barcode:", barcode)
    # insert async processing here

async def main(port=None, baudrate=115200):
    if port is None:
        port = choose_port()
        print(f"Auto-selected port: {port}")

    loop = asyncio.get_running_loop()
    coro = serial_asyncio.create_serial_connection(
        loop, lambda: BarcodeProtocol(handle_barcode), port, baudrate=baudrate
    )
    transport, protocol = await coro
    print(f"Listening on {port} @ {baudrate} (press Ctrl-C to quit)")
    try:
        await asyncio.Future()  # run forever
    except asyncio.CancelledError:
        pass
    finally:
        transport.close()

if __name__ == "__main__":
    import sys
    port_arg = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        asyncio.run(main(port=port_arg))
    except KeyboardInterrupt:
        print("Exiting...")
