from typing import Optional
import asyncio
import serial_asyncio
from typing import Optional
from .base import AbstractScanner


class SymcodeScanner(AbstractScanner):
    def __init__(self, port: str, baudrate: int) -> None:
        self.port = port
        self.baudrate = baudrate
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def initialize(self) -> None:
        self.reader, self.writer = await serial_asyncio.open_serial_connection(
            url=self.port, baudrate=self.baudrate
        )

    async def read(self) -> bytes:
        if not self.reader:
            raise ConnectionError("Serial connection is not established. Call initialize() first.")
        data = await self.reader.readuntil(b"\r")
        return data

    async def scan(self) -> Optional[str]:
        data = await self.read()
        decoded_data = data.decode().strip()
        if len(decoded_data) == 12:
            return decoded_data
        else:
            print(f"Failed Code: {decoded_data}")
            return None

    async def shutdown(self) -> None:
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
