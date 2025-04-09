from pydantic import BaseModel
from perpetual_trinamic.aio.trinamic_axis import AxisStatus, TrinamicAxis
from perpetual_can import CANBusHandler
from typing import List, Literal, Union, Annotated
from pydantic import BaseModel, Field
import logging

from perpetual_os1.common import AbstractSubsystem, CanAxisConfiguration


class GantryConfiguration(BaseModel):
    x_axis: CanAxisConfiguration
    y_axis: CanAxisConfiguration


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
    x_axis: AxisStatus
    y_axis: AxisStatus


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


class Gantry(AbstractSubsystem[GantryConfiguration, GantryStatus]):
    def __init__(self, bus: CANBusHandler, config: GantryConfiguration):
        self._configuration = config
        self._x_axis = TrinamicAxis(
            bus,
            self._configuration.x_axis.configuration,
            module_id=self._configuration.x_axis.can_id,
            motor_id=0,
            max_current=6000,
        )
        self._y_axis = TrinamicAxis(
            bus,
            self._configuration.y_axis.configuration,
            module_id=self._configuration.y_axis.can_id,
            motor_id=0,
            max_current=6000,
        )

    async def cleanup(self) -> None:
        await self.stop()

    async def status(self) -> GantryStatus:
        return GantryStatus(
            x_axis=await self._x_axis.status(),
            y_axis=await self._y_axis.status(),
        )

    @property
    def configuration(self) -> GantryConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: GantryConfiguration) -> None:
        self._configuration = configuration
        self._x_axis.configuration = self._configuration.x_axis.configuration
        self._y_axis.configuration = self._configuration.y_axis.configuration
        await self.initialize()

    async def stop(self) -> None:
        await self._x_axis.stop()
        await self._y_axis.stop()
        await self._y_axis.end_brake_move()

    async def initialize(self) -> None:
        await self._x_axis.initialize()
        await self._y_axis.initialize()
        await self._x_axis.set_pullup_resistors(0)
        await self._y_axis.set_pullup_resistors(0)

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

    async def move_path(self, path: List[GantryCoordinate]) -> None:
        try:
            position = await self.position
            await self._y_axis.safety_off()
            await self._x_axis.safety_off()
            await self._y_axis.sync_position()
            await self._x_axis.sync_position()
            await self._y_axis.safety_on()
            await self._x_axis.safety_on()
            await self._y_axis.init_brake_move()
            for ix, coordinate in enumerate(path):
                await self._x_axis.move_to_position(coordinate.x)
                await self._y_axis.move_to_position(coordinate.y, override_brake=True)
                await self._y_axis.wait_for_move()
                await self._x_axis.wait_for_move()
        except Exception as e:
            x_pos = await self._x_axis.get_position()
            x_enc = await self._x_axis.get_encoder_position()
            x_actual = await self._x_axis.get_actual_position()
            x_target = await self._x_axis.get_target_position()

            y_pos = await self._y_axis.get_position()
            y_enc = await self._y_axis.get_encoder_position()
            y_actual = await self._y_axis.get_actual_position()
            y_target = await self._y_axis.get_target_position()

            logging.error("Failed Path Movement: ", path, e)
            logging.error(f"X POS: {x_pos} ENC: {x_enc} ACTUAL: {x_actual} TARGET: {x_target}")
            logging.error(f"Y POS: {y_pos} ENC: {y_enc} ACTUAL: {y_actual} TARGET: {y_target}")
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
        # limit = await self._x_axis.get_digital_input(1, 0)
        # if not bool(limit.value):
        #    raise Exception("X Axis Failed to hit homing switch")
        await self._y_axis.home()
        # limit = await self._y_axis.get_digital_input(1, 0)
        # if not bool(limit.value):
        #    raise Exception("Y Axis Failed to hit homing switch")

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
