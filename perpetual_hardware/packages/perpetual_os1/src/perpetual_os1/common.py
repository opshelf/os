import perpetual_hardware.packages.hardware.MotorBase as Motor
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


from dataclasses import dataclass

@dataclass
class AxisCoordinate:
    x: float
    y: float

    def __add__(self, other: "AxisCoordinate") -> "AxisCoordinate":
        return AxisCoordinate(x=self.x + other.x, y=self.y + other.y, z=self.z + other.z)

    def __sub__(self, other: "AxisCoordinate") -> "AxisCoordinate":
        return AxisCoordinate(x=self.x - other.x, y=self.y - other.y, z=self.z - other.z)

@dataclass
class ToolCoordinate:
    g: float # gripper
    s: float # stage
    r: float # rotation (only 0 or 180)

    def __add__(self, other: "ToolCoordinate") -> "ToolCoordinate":
        return ToolCoordinate(i=self.i + other.i, j=self.j + other.j, k=self.k)

    def __sub__(self, other: "ToolCoordinate") -> "ToolCoordinate":
        return ToolCoordinate(i=self.i - other.i, j=self.j - other.j, k=self.k)


