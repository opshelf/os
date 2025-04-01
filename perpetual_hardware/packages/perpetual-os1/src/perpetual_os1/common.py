from hardware_common.stepper_motor import StepperMotorConfig
from pydantic import BaseModel
from enum import Enum
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

C = TypeVar("C")
S = TypeVar("S")


class AbstractSubsystem(ABC, Generic[C, S]):
    @property
    @abstractmethod
    def configuration(self) -> C: ...

    @abstractmethod
    async def set_configuration(self, configuration: C) -> None: ...

    @abstractmethod
    async def status(self) -> S: ...


class CanAxisConfiguration(BaseModel):
    can_id: int
    configuration: StepperMotorConfig
    name: str
    axis_type: str


class CanMessageIds(int, Enum):
    X_AXIS = 100
    Y_AXIS = 101
    Z_AXIS = 110
    GRIPPER = 111
