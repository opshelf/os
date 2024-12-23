from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from enum import Enum
from pydantic import BaseModel


class StepperMotorStatus(Enum):
    ERROR = "ERROR"
    GOOD = "GOOD"


class AxisCommands(str, Enum):
    INITIALIZE = "INITIALIZE"
    HOME = "HOME"
    MOVE_TO_POSITION = "MOVE_TO_POSITION"
    MOVE_TO_RELATIVE_POSITION = "MOVE_TO_RELATIVE_POSITION"
    STOP = "STOP"


class PositionCommand(BaseModel):
    position: float


class AxisCommand(BaseModel):
    command: AxisCommands
    payload: Optional[Dict[Any, Any]]


class Rotation(Enum):
    CW = 0
    CCW = 1


class StepperMotorConfig(BaseModel):
    microsteps: int
    steps_per_rev: int
    work_current: int
    holding_current: int
    homing_current: int
    velocity: float
    acceleration: int
    homing_speed: float
    steps_per_mm: Optional[float]
    encoder_ppr: Optional[int]
    max_velocity: Optional[float]
    max_acceleration: Optional[float]
    min_travel: Optional[float]
    max_travel: Optional[float]
    max_encoder_deviation: int = 500
    max_encoder_deviation_homing: int = 100
    safety: bool = True
    invert_coordinates: bool = False
    external_encoder: bool = False
    home_direction: Rotation = Rotation.CW
    brake: bool = False

    def convert_encoder_position(self, encoder_value: int) -> float:
        if self.encoder_ppr and self.steps_per_mm:
            revolutions = encoder_value / self.encoder_ppr
            steps_per_rev = self.microsteps * self.steps_per_rev
            steps = revolutions * steps_per_rev
            if self.invert_coordinates:
                return round((steps / self.steps_per_mm) * -1, 3)
            return round(steps / self.steps_per_mm, 3)
        raise NotImplementedError

    def convert_position_to_pulses(self, position: float) -> int:
        if self.max_travel and position > self.max_travel:
            raise MaxTravelLimitError
        if self.min_travel and position > self.min_travel:
            raise MinTravelLimitError
        if self.steps_per_mm:
            steps = position * self.steps_per_mm
            if self.invert_coordinates:
                return int(steps * -1)
            return int(steps)
        raise NotImplementedError

    @property
    def mms_ps(self) -> int:
        if self.steps_per_mm:
            return self.convert_mms_ps(self.velocity)
        raise NotImplementedError

    def convert_mms_ps(self, mmps: float) -> int:
        if self.steps_per_mm:
            steps_per_second = mmps * self.steps_per_mm
            return int(steps_per_second)
        raise NotImplementedError

    # TODO: This needs to accomodate a gearing ratio it is not a good abstraction
    def convert_to_revolutions(self, encoder_value: int) -> float:
        if self.encoder_ppr:
            encoder_revolutions = encoder_value / self.encoder_ppr
            revolutions = encoder_revolutions / (self.steps_per_rev / 200)
            if self.invert_coordinates:
                return round(revolutions * -1, 3)
            return round(revolutions, 3)
        raise NotImplementedError

    def convert_revolutions_to_steps(self, revolutions: float) -> int:
        steps = round(revolutions * self.steps_per_rev * self.microsteps)
        if self.invert_coordinates:
            return steps * -1
        return steps


class AbstractStepperMotor(ABC):
    @abstractmethod
    def initialize(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def home(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_position(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def move_to_position(self, position: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def move_to_relative_position(self, position: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_move(self, timeout: float = 10) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop_motor(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_status(self) -> StepperMotorStatus:
        raise NotImplementedError

    @abstractmethod
    def set_speed(self, mms_ps: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_acceleration(self, mmss: float) -> None:
        raise NotImplementedError

    @property
    @abstractmethod
    def configuration(self) -> StepperMotorConfig:
        return self._configuration

    @configuration.setter
    @abstractmethod
    def configuration(self, configuration: StepperMotorConfig) -> None:
        self._configuration = configuration

    # def handle_command(self, cmd: AxisCommand) -> None:
    #    if cmd.command == AxisCommands.INITIALIZE:
    #        self.initialize()
    #    elif cmd.command == AxisCommands.HOME:
    #        self.home()
    #    elif cmd.command == AxisCommands.MOVE_TO_POSITION:
    #        if cmd.payload:
    #            move = PositionCommand(**cmd.payload)
    #            self.move_to_relative_position(move.position)
    #        else:
    #            raise Exception("Invalid Command")
    #    elif cmd.command == AxisCommands.MOVE_TO_RELATIVE_POSITION:
    #        if cmd.payload:
    #            move = PositionCommand(**cmd.payload)
    #            self.move_to_relative_position(move.position)
    #        else:
    #            raise Exception("Invalid Command")
    #    elif cmd.command == AxisCommands.STOP:
    #        self.stop_motor()
    #    else:
    #        raise Exception("Invalid Command Provided {cmd}")


class MaxTravelLimitError(Exception):
    """The Requested Travel Distance Exceeds the Maximum"""


class MinTravelLimitError(Exception):
    """The Requested Travel Distance Exceeds the Minimum"""
