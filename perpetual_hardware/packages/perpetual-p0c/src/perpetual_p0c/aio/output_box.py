from perpetual_p0c.common import CanAxisConfiguration
from perpetual_can import CANBusHandler
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from pydantic import BaseModel


class OutputBoxOffsets(BaseModel):
    door_open_position: float


class OutputBoxConfiguration(BaseModel):
    door: CanAxisConfiguration
    offsets: OutputBoxOffsets


class OutputBox:
    def __init__(self, bus: CANBusHandler, configuration: OutputBoxConfiguration):
        self._configuration = configuration
        self._door = TrinamicAxis(
            bus,
            module_id=configuration.door.can_id,
            motor_id=0,
            configuration=configuration.door.configuration,
        )
        # atexit.register(self.cleanup)

    async def cleanup(self) -> None:
        await self._door.stop()

    async def stop(self) -> None:
        await self._door.stop()

    @property
    def configuration(self) -> OutputBoxConfiguration:
        return self._configuration

    @configuration.setter
    def configuration(self, configuration: OutputBoxConfiguration) -> None:
        self._configuration = configuration

    @property
    def offsets(self) -> OutputBoxOffsets:
        return self._configuration.offsets

    @offsets.setter
    def offsets(self, offsets: OutputBoxOffsets) -> None:
        self._configuration.offsets = offsets

    @property
    def door(self) -> TrinamicAxis:
        return self._door

    async def initialize(self) -> None:
        await self._door.initialize()

    async def home(self) -> None:
        await self._door.home()

    async def open(self) -> None:
        await self._door.move_to_position(self._configuration.offsets.door_open_position)
        await self._door.wait_for_move()

    async def close(self) -> None:
        await self._door.move_to_position(0)
        await self._door.wait_for_move()
