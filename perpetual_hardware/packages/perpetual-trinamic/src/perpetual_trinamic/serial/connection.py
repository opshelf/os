import asyncio
import serial_asyncio
from perpetual_trinamic.base import TrinamicTransporter
from typing import Optional
import concurrent.futures
from perpetual_trinamic.tmcl import TmclReply, TmclMessage

class TrinamicUsbConnection(TrinamicTransporter):

    def __init__(self, port: str, baudrate: int, loop: asyncio.AbstractEventLoop) -> None:
        self._port = port
        self._baudrate = baudrate
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._loop = loop
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

    @classmethod
    async def build(cls, port: str, baudrate: int, loop: asyncio.AbstractEventLoop) -> "TrinamicUsbConnection":
        instance = cls(port, baudrate, loop)
        await instance.initialize()
        return instance


    async def initialize(self) -> None:
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=self._baudrate
        )

    async def read(self) -> bytes:
        if not self._reader:
            raise ConnectionError("Serial connection is not established. Call connect() first.")
        data = await self._reader.readexactly(9)
        return data

    async def shutdown(self) -> None:
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()

    async def send_message(
        self, message: TmclMessage, timeout: Optional[float] = 1.0
    ) -> TmclReply: 
        if not self._writer:
            raise ConnectionError("Serial connection is not established. Call connect() first.")
        await self._loop.run_in_executor(self._executor, self._writer.write, bytes(message.into_serial_buffer()))
        # TODO: FLUSH BUFFER ON FAILED READ
        async with asyncio.timeout(timeout):
            raw_reply = await self.read()
            return TmclReply.from_serial_buffer(raw_reply)
