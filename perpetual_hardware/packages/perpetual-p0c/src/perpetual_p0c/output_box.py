from typing import Annotated, Literal, Union
from perpetual_can import CANBusHandler
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from perpetual_p0c.common import AbstractSubsystem, CanAxisConfiguration
from pydantic import BaseModel, Field


class OutputBoxOffsets(BaseModel):
    door_open_position: float

class OutputBoxStatus(BaseModel):
    door_position: float

class OutputBoxConfiguration(BaseModel):
    door: CanAxisConfiguration
    offsets: OutputBoxOffsets


class ShutdownCommand(BaseModel):
    command: Literal["shutdown"]


class StopCommand(BaseModel):
    command: Literal["stop"]


class InitializeCommand(BaseModel):
    command: Literal["initialize"]


class OpenCommand(BaseModel):
    command: Literal["open"]


class CloseCommand(BaseModel):
    command: Literal["close"]


class HomeCommand(BaseModel):
    command: Literal["home"]


OutputBoxCommands = Annotated[
    Union[ShutdownCommand, StopCommand, InitializeCommand, OpenCommand, CloseCommand, HomeCommand],
    Field(discriminator="command"),
]


class OutputBox(AbstractSubsystem[OutputBoxConfiguration, OutputBoxStatus]):
    def __init__(self, bus: CANBusHandler, configuration: OutputBoxConfiguration):
        self._configuration = configuration
        self._door = TrinamicAxis(
            bus,
            module_id=configuration.door.can_id,
            motor_id=0,
            configuration=configuration.door.configuration,
        )

    async def cleanup(self) -> None:
        await self._door.stop()

    async def stop(self) -> None:
        await self._door.stop()

    @property
    async def configuration(self) -> OutputBoxConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: OutputBoxConfiguration) -> None:
        self._configuration = configuration
        self._door.configuration = self._configuration.door.configuration
        await self.initialize()

    async def status(self) -> OutputBoxStatus:
        position = await self._door.get_position()
        return OutputBoxStatus(
            door_position=position
        )

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

    async def handle_command(self, command: OutputBoxCommands) -> None:
        match command:
            case ShutdownCommand():
                await self.cleanup()
            case StopCommand():
                await self.stop()
            case InitializeCommand():
                await self.initialize()
            case OpenCommand():
                await self.open()
            case CloseCommand():
                await self.close()
            case HomeCommand():
                await self.home()
            case _:
                raise Exception("Invalid Command")
