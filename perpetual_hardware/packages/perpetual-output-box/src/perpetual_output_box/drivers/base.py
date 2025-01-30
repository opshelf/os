from abc import ABCMeta, abstractmethod
from perpetual_trinamic.serial.trinamic_axis import AxisStatus
from hardware_common.stepper_motor import StepperMotorConfig
from pydantic import BaseModel
from enum import Enum


class DoorState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class LockState(str, Enum):
    LOCKED = "LOCKED"
    UNLOCKED = "UNLOCKED"


class BreakBeamState(str, Enum):
    OBSTRUCTED = "OBSTRUCTED"
    UNOBSTRUCTED = "UNOBSTRUCTED"


class DoorConfiguration(BaseModel):
    open_position: float
    closed_position: float = 1.0
    axis_configuration: StepperMotorConfig
    module_id: int
    motor_id: int


class AbstractIO(metaclass=ABCMeta):
    @abstractmethod
    async def get_door_sensor(self) -> DoorState: ...

    @abstractmethod
    async def get_break_beam_sensor(self) -> BreakBeamState: ...

    @abstractmethod
    async def get_lock(self) -> LockState: ...

    @abstractmethod
    async def set_door_locked(self, door_locked: LockState) -> None: ...


class AbstractDoor(metaclass=ABCMeta):
    @abstractmethod
    async def open_door(self) -> None: ...

    @abstractmethod
    async def close_door(self) -> None: ...

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def home(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def status(self) -> AxisStatus: ...

    @abstractmethod
    async def set_configuration(self, configuration: DoorConfiguration) -> None: ...

    @property
    @abstractmethod
    async def configuration(self) -> DoorConfiguration: ...
