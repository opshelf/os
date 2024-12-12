from hardware_common.stepper_motor import (
    StepperMotorConfig,
)
from can import BusABC
from perpetual_trinamic.trinamic_axis import TrinamicAxis
from pydantic import BaseModel
from enum import Enum
from typing import Optional, Any, Dict
import atexit


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


class Commands(str, Enum):
    HOME = "HOME"
    INITIALIZE = "INITIALIZE"
    MOVE = "MOVE"
    MOVE_RELATIVE = "MOVE_RELATIVE"
    STOP = "STOP"


class GantryMoveCommand(BaseModel):
    position: GantryCoordinate


class GantryCommands(BaseModel):
    command: Commands
    payload: Optional[Dict[Any, Any]]


class GantryStatus(BaseModel):
    x_position: float
    y_position: float


class Gantry:
    def __init__(self, bus: BusABC, config: GantryConfiguration):
        self._config = config
        self._x_axis = TrinamicAxis(
            bus,
            module_id=self._config.x_axis_id,
            motor_id=0,
            configuration=self._config.x_axis_config,
            max_current=6000,
        )
        self._y_axis = TrinamicAxis(
            bus,
            module_id=self._config.y_axis_id,
            motor_id=0,
            configuration=self._config.y_axis_config,
            max_current=6000,
        )
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        self.stop()

    def status(self) -> GantryStatus:
        return GantryStatus(
            x_position=self._x_axis.get_position(),
            y_position=self._y_axis.get_position(),
        )

    def stop(self) -> None:
        self._x_axis.stop()
        self._y_axis.stop()

    def initialize(self) -> None:
        self._x_axis.initialize()
        self._y_axis.initialize()

    @property
    def x_axis(self) -> TrinamicAxis:
        return self._x_axis

    @property
    def y_axis(self) -> TrinamicAxis:
        return self._y_axis

    def move_coordinate(self, coordinate: GantryCoordinate, wait: bool = True) -> None:
        self._x_axis.move_to_position(coordinate.x)
        self._y_axis.move_to_position_with_brake(coordinate.y)
        if wait:
            self.wait_for_move()

    def move(self, x: float, y: float, wait: bool = True) -> None:
        self._x_axis.move_to_position(x)
        self._y_axis.move_to_position_with_brake(y)
        if wait:
            self.wait_for_move()

    def move_relative(self, x: float, y: float, wait: bool = True) -> None:
        self._x_axis.move_to_relative_position(x)
        self._y_axis.move_to_relative_position_with_brake(y)
        if wait:
            self.wait_for_move()

    def wait_for_move(self, timeout: float = 60) -> None:
        self._y_axis.wait_for_brake_move(timeout)
        self._x_axis.wait_for_move(timeout)

    def home(self) -> None:
        self._x_axis.home()
        if not bool(self._x_axis.get_digital_input(1, 0).value):
            raise Exception("Failed to hit homing switch")
        self._y_axis.home_with_brake()
        if not bool(self._y_axis.get_digital_input(1, 0).value):
            raise Exception("Failed to hit homing switch")

    def idle(self) -> None:
        self._x_axis.move_to_position(1)
        self._y_axis.move_to_position_with_brake(1)
        self.wait_for_move()

    def y_safe(self) -> None:
        self._y_axis.move_to_position_with_brake(1)

    @property
    def position(self) -> GantryCoordinate:
        return GantryCoordinate(
            x=self._x_axis.get_position(),
            y=self._y_axis.get_position(),
        )

    def handle_command(self, cmd: GantryCommands) -> None:
        if cmd.command == Commands.HOME:
            self.home()
        elif cmd.command == Commands.INITIALIZE:
            self.initialize()
        elif cmd.command == Commands.MOVE:
            if cmd.payload:
                coordinate = GantryCoordinate(**cmd.payload)
                self.move(coordinate.x, coordinate.y)
            else:
                raise Exception("Invalid Command")
        elif cmd.command == Commands.MOVE_RELATIVE:
            if cmd.payload:
                coordinate = GantryCoordinate(**cmd.payload)
                self.move_relative(coordinate.x, coordinate.y)
            else:
                raise Exception("Invalid Command")
        elif cmd.command == Commands.STOP:
            self.stop()
        else:
            raise Exception("Invalid Command")
