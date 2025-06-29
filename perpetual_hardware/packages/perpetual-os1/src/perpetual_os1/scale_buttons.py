from perpetual_waveshare.digital import DigitalIOController
from pymodbus.client import AsyncModbusSerialClient
import asyncio

class ScaleButtons(object):

    def __init__(self, client: AsyncModbusSerialClient) -> None:
        self._io = DigitalIOController(client)
        self._button_delay = .25
        self._pwr_idx = 3
        self._print_idx = 2
        self._tare_idx = 0


    async def _toggle_channel(self, idx: int) -> None:
        await self._io.set_channel_off(idx)
        await asyncio.sleep(self._button_delay)
        await self._io.set_channel_on(idx)


    async def tare(self) -> None:
        await self._toggle_channel(self._tare_idx)

    async def power(self) -> None:
        await self._toggle_channel(self._pwr_idx)

    async def print(self) -> None:
        await self._toggle_channel(self._print_idx)
