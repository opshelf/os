from typing import Optional, Tuple
from perpetual_p0c.storage import GS1Data
import asyncio
from pydantic import ValidationError
import serial_asyncio

from typing import Optional
from .base import AbstractScanner
from .gs1_parser import GS1Parser


class SymcodeScanner(AbstractScanner):
    def __init__(self, port: str, baudrate: int, location: str) -> None:
        self.port = port
        self.baudrate = baudrate
        self._location = location
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None


    def location(self) -> str:
        return self._location

    async def initialize(self) -> None:
        self.reader, self.writer = await serial_asyncio.open_serial_connection(
            url=self.port, baudrate=self.baudrate
        )

    async def read(self) -> bytes:
        if not self.reader:
            raise ConnectionError("Serial connection is not established. Call initialize() first.")
        data = await self.reader.readuntil(b"\r")
        return data

    async def scan(self) -> Optional[Tuple[GS1Data, str]]:
        try:
            data = await self.read()
            decoded_data = data.decode().strip()
            gs1 = GS1Parser.parse_gs1_to_model(decoded_data)
            return (gs1, self._location)
        except ValidationError as e:
            print(e)
            pass
        except Exception as e:
            await self.shutdown()
        return None

    async def shutdown(self) -> None:
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
