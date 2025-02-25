import logging
from .base import AbstractWorkstation
from perpetual_waveshare.relay import RelayController,Action

class ModbusWorkstation(AbstractWorkstation):

    def __init__(self, relay_controller: RelayController) -> None:
        self._relay_controller = relay_controller

    async def close_all_outputs(self) -> None:
        for i in range(1, 4):
            await self._relay_controller.set_channel(i, Action.Off)

    async def open_all_outputs(self) -> None:
        for i in range(1, 4):
            await self._relay_controller.set_channel(i, Action.On)

    async def open_output(self, output_box: int) -> None:
        await self._relay_controller.set_channel(2 - output_box, Action.On)

    async def close_output(self, output_box: int) -> None:
        await self._relay_controller.set_channel(2 - output_box, Action.Off)

    async def open_input(self) -> None:
        await self._relay_controller.set_channel(3, Action.On)

    async def close_input(self) -> None: 
        await self._relay_controller.set_channel(3, Action.Off)
