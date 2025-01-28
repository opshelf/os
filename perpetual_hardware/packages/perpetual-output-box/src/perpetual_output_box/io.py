from abc import ABCMeta, abstractmethod

from hardware_common.stepper_motor import StepperMotorConfig
from perpetual_trinamic.serial.trinamic_axis import AxisStatus
from pydantic import BaseModel


class DoorConfiguration(BaseModel):
    open_position: float
    closed_position: float = 1.0
    axis_configuration: StepperMotorConfig
    module_id: int
    motor_id: int


class AbstractIO(metaclass=ABCMeta):
    @abstractmethod
    def get_door_sensor(self) -> bool: ...

    @abstractmethod
    def get_break_beam_sensor(self) -> bool: ...

    @abstractmethod
    def get_lock(self) -> bool: ...

    @abstractmethod
    def set_door_locked(self, door_locked: bool) -> None: ...


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
    async def status(self) -> AxisStatus: ...
