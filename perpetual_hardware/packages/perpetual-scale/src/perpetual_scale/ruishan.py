import asyncio
import serial_asyncio
from perpetual_scale.core import AbstractScale


class SerialScale(AbstractScale):
    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 9600, timeout: float = 0.2):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._reader = None
        self._writer = None
        self._running = False

    async def initialize(self) -> None:
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port,
            baudrate=self._baudrate,
        )
        self._running = True

    async def shutdown(self) -> None:
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        self._running = False

    async def read(self) -> float:
        if not self._reader or not self._running:
            raise RuntimeError("Scale not initialized")
        try:
            line = await asyncio.wait_for(self._reader.readline(), timeout=self._timeout)
            response = line.decode().rstrip()
            filtered = "".join(c for c in response if not c.isspace() and not c.isalpha())
            return float(filtered)
        except asyncio.TimeoutError:
            raise TimeoutError("Timed out while reading from scale")
