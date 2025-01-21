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
    #IO_NODE = 5
    IO_NODE_SEND = 200
    IO_NODE_RECEIVE = 200
    TOOL_NODE = 6
    X_AXIS = 100
    Y_AXIS = 101
    Z_AXIS = 102
    INPUT_FUNNEL = 110
    OPPOSING_FUNNEL = 111
    SLOT_1 = 112
    SLOT_2 = 113
    SLOT_3 = 114
    INPUT_CONVEYOR = 130
    DIVERTER = 131
    OUTPUT_TRANSFER_CONVEYOR = 132
    ACTIVE_WALL = 133
    OUTPUT_CONVEYOR = 134
    IO_IN_CONVEYOR = 135
    IO_OUT_CONVEYOR = 136
    OUTPUT_BOX_1 = 137
    OUTPUT_BOX_2 = 138
    OUTPUT_BOX_3 = 139
