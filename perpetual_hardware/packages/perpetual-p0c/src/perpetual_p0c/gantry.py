from hardware_common.stepper_motor import (
    StepperMotorConfig,
)
from pydantic import BaseModel
from enum import Enum
from typing import Optional, Any, Dict, List
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from perpetual_can import CANBusHandler
from typing import List, Literal, Union, Annotated
from pydantic import BaseModel, Field


class GantryConfiguration(BaseModel):
    x_axis_id: int
    x_axis_config: StepperMotorConfig
    y_axis_id: int
    y_axis_config: StepperMotorConfig


class GantryCoordinate(BaseModel):
    x: float
    y: float

    def __add__(self, rhs: "GantryCoordinate") -> "GantryCoordinate":
        return GantryCoordinate(x=self.x + rhs.x, y=self.y + rhs.y)

    def __sub__(self, rhs: "GantryCoordinate") -> "GantryCoordinate":
        return GantryCoordinate(x=self.x - rhs.x, y=self.y - rhs.y)


class GantryMoveCommand(BaseModel):
    position: GantryCoordinate


class GantryStatus(BaseModel):
    x_position: float
    y_position: float


class ShutdownCommand(BaseModel):
    command: Literal["shutdown"]


class StopCommand(BaseModel):
    command: Literal["stop"]


class InitializeCommand(BaseModel):
    command: Literal["initialize"]


class HomeCommand(BaseModel):
    command: Literal["home"]


class HomeYFirstCommand(BaseModel):
    command: Literal["home_y"]


class MoveRelativeCommand(BaseModel):
    command: Literal["move_relative"]
    coordinate: GantryCoordinate


class MoveCommand(BaseModel):
    command: Literal["move"]
    coordinate: GantryCoordinate


GantryCommands = Annotated[
    Union[
        ShutdownCommand,
        StopCommand,
        InitializeCommand,
        HomeCommand,
        HomeYFirstCommand,
        MoveRelativeCommand,
        MoveCommand,
    ],
    Field(discriminator="command"),
]


class Gantry:
    def __init__(self, bus: CANBusHandler, config: GantryConfiguration):
        self._config = config
        self._x_axis = TrinamicAxis(
            bus,
            self._config.x_axis_config,
            module_id=self._config.x_axis_id,
            motor_id=0,
            max_current=6000,
        )
        self._y_axis = TrinamicAxis(
            bus,
            self._config.y_axis_config,
            module_id=self._config.y_axis_id,
            motor_id=0,
            max_current=6000,
        )

    async def cleanup(self) -> None:
        await self.stop()

    async def status(self) -> GantryStatus:
        return GantryStatus(
            x_position=await self._x_axis.get_position(),
            y_position=await self._y_axis.get_position(),
        )

    async def stop(self) -> None:
        await self._x_axis.stop()
        await self._y_axis.stop()
        await self._y_axis.end_brake_move()

    async def initialize(self) -> None:
        await self._x_axis.initialize()
        await self._y_axis.initialize()

    @property
    def x_axis(self) -> TrinamicAxis:
        return self._x_axis

    @property
    def y_axis(self) -> TrinamicAxis:
        return self._y_axis

    async def move_coordinate(self, coordinate: GantryCoordinate, wait: bool = True) -> None:
        await self._x_axis.move_to_position(coordinate.x)
        await self._y_axis.move_to_position(coordinate.y)
        if wait:
            await self.wait_for_move()
            await self._y_axis.reset_location()

    async def move_path(self, path: List[GantryCoordinate], wait: bool = True) -> None:
        position = await self.position
        await self._y_axis.reset_location()
        try:
            await self._y_axis.init_brake_move()
            for coordinate in path:
                await self._x_axis.move_to_position(coordinate.x)
                await self._y_axis.move_to_position(coordinate.y, override_brake=True)
                await self._y_axis.wait_for_move()
                await self._x_axis.wait_for_move()
            if wait:
                await self.wait_for_move()
        except Exception as e:
            print("Failed Path Movement: ", path, position)
            raise e
        finally:
            await self._y_axis.end_brake_move()

    async def move(self, x: float, y: float, wait: bool = True) -> None:
        await self._x_axis.move_to_position(x)
        await self._y_axis.move_to_position(y)
        if wait:
            await self.wait_for_move()

    async def move_relative(self, x: float, y: float, wait: bool = True) -> None:
        await self._x_axis.move_to_relative_position(x)
        await self._y_axis.move_to_relative_position(y)
        if wait:
            await self.wait_for_move()

    async def wait_for_move(self, timeout: float = 60) -> None:
        await self._y_axis.wait_for_move(timeout)
        await self._x_axis.wait_for_move(timeout)

    async def home(self) -> None:
        await self._x_axis.home()
        limit = await self._x_axis.get_digital_input(1, 0)
        if not bool(limit.value):
            raise Exception("X Axis Failed to hit homing switch")
        await self._y_axis.home()
        limit = await self._y_axis.get_digital_input(1, 0)
        if not bool(limit.value):
            raise Exception("Y Axis Failed to hit homing switch")

    async def home_y_first(self) -> None:
        await self._y_axis.home()
        limit = await self._y_axis.get_digital_input(1, 0)
        if not bool(limit.value):
            raise Exception("Y Axis Failed to hit homing switch")
        await self._x_axis.home()
        limit = await self._x_axis.get_digital_input(1, 0)
        if not bool(limit.value):
            raise Exception("X Axis Failed to hit homing switch")

    @property
    async def position(self) -> GantryCoordinate:
        return GantryCoordinate(
            x=await self._x_axis.get_position(),
            y=await self._y_axis.get_position(),
        )

    async def handle_command(self, command: GantryCommands) -> None:
        match command:
            case ShutdownCommand():
                await self.cleanup()
            case StopCommand():
                await self.stop()
            case InitializeCommand():
                await self.initialize()
            case HomeCommand():
                await self.home()
            case HomeYFirstCommand():
                await self.home_y_first()
            case MoveRelativeCommand(coordinate=coordinate):
                await self.move_relative(coordinate.x, coordinate.y)
            case MoveCommand(coordinate=coordinate):
                await self.move(coordinate.x, coordinate.y)
            case _:
                raise Exception("Invalid Command")
