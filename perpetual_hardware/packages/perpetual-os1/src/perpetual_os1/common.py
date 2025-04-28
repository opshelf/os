from hardware_common.stepper_motor import StepperMotorConfig
from pydantic import BaseModel
from enum import Enum
from abc import ABC, abstractmethod
from typing import Generic, TypeVar


C = TypeVar("C")
S = TypeVar("S")


class RobotNatsConfiguration(BaseModel):
    stream_name: str = "robot"
    command_queue: str = "commands"
    event_queue: str = "events"
    status_queue: str = "robot.status"

    get_storage_subject: str = "robot.get_storage"
    set_storage_subject: str = "robot.set_storage"
    get_config_subject: str = "robot.get_configuration"
    set_config_subject: str = "robot.set_configuration"
    get_status_subject: str = "robot.get_status"

    domain: str = "localhost"
    port: int = 4222

    def connection_string(self) -> str:
        return f"nats://{self.domain}:{self.port}"


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
    DOOR = 120
