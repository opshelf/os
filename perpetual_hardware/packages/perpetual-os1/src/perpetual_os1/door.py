from perpetual_can import CANBusHandler
from pydantic import BaseModel, Field
from perpetual_os1.common import CanAxisConfiguration, AbstractSubsystem
from perpetual_trinamic.aio.trinamic_axis import AxisStatus, TrinamicAxis
from pydantic import BaseModel
from typing import Literal, Annotated, Union


class DoorConfiguration(BaseModel):
    open_position: float = 200.0
    close_position: float = 1.0
    axis: CanAxisConfiguration


class DoorStatus(BaseModel):
    door: AxisStatus


class StopCommand(BaseModel):
    command: Literal["stop"]


class InitializeCommand(BaseModel):
    command: Literal["initialize"]


class HomeCommand(BaseModel):
    command: Literal["home"]


class MoveCommand(BaseModel):
    command: Literal["move"]
    position: float


class MoveRelativeCommand(BaseModel):
    command: Literal["move_relative"]
    position: float


class OpenDoorCommand(BaseModel):
    command: Literal["open_door"]


class CloseDoorCommand(BaseModel):
    command: Literal["close_door"]


DoorCommands = Annotated[
    Union[
        StopCommand,
        InitializeCommand,
        HomeCommand,
        MoveCommand,
        MoveRelativeCommand,
        OpenDoorCommand,
        CloseDoorCommand,
    ],
    Field(discriminator="command"),
]


class DoorCommandSuccessful(BaseModel):
    message: Literal["success"] = "success"


class DoorCommandError(BaseModel):
    error: str


DoorResponse = Union[DoorCommandSuccessful, DoorCommandError]


class Door(AbstractSubsystem[DoorConfiguration, DoorStatus]):
    def __init__(self, bus: CANBusHandler, configuration: DoorConfiguration) -> None:
        self._configuration = configuration
        self._axis = TrinamicAxis(
            bus,
            configuration=self._configuration.axis.configuration,
            module_id=self._configuration.axis.can_id,
        )

    @property
    def axis(self) -> TrinamicAxis:
        return self._axis

    @property
    def configuration(self) -> DoorConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: DoorConfiguration) -> None:
        self._configuration = configuration
        self._axis.configuration = self._configuration.axis.configuration
        await self.initialize()

    async def status(self) -> DoorStatus:
        axis = await self._axis.status()
        return DoorStatus(door=axis)

    async def stop(self) -> None:
        await self._axis.stop()

    async def home(self) -> None:
        await self._axis.home()

    async def initialize(self) -> None:
        await self._axis.initialize()

    async def open_door(self) -> None:
        await self._axis.move_to_position(self._configuration.open_position)
        await self._axis.wait_for_move()

    async def close_door(self) -> None:
        await self._axis.move_to_position(self._configuration.close_position)
        await self._axis.wait_for_move()

    async def handle_command(self, command: DoorCommands) -> DoorResponse:
        match command:
            case StopCommand():
                await self.stop()
                return DoorCommandSuccessful()
            case InitializeCommand():
                await self.initialize()
                return DoorCommandSuccessful()
            case HomeCommand():
                await self.home()
                return DoorCommandSuccessful()
            case MoveCommand(position=position):
                await self._axis.move_to_position(position)
                return DoorCommandSuccessful()
            case MoveRelativeCommand(position=position):
                await self._axis.move_to_relative_position(position)
                return DoorCommandSuccessful()
            case OpenDoorCommand():
                await self.open_door()
                return DoorCommandSuccessful()
            case CloseDoorCommand():
                await self.close_door()
                return DoorCommandSuccessful()
            case _:
                raise Exception("Invalid Door Command")
