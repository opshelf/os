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
    s: int = 0
    z_offset: float = 0.0

    def __post_init__(self) -> None:
        if self.s not in (0, 1):
            raise ValueError("AxisCoordinate.s must be 0 (left) or 1 (right)")

    def _assert_same_side(self, other: "AxisCoordinate", op: str) -> None:
        if not isinstance(other, AxisCoordinate):
            raise TypeError(f"Cannot {op} AxisCoordinate with {type(other)!r}")
        if self.s != other.s:
            raise ValueError(
                f"Cannot {op} AxisCoordinate instances from different sides "
                f"(self={self.s}, other={other.s})"
            )

    def __add__(self, other: "AxisCoordinate") -> "AxisCoordinate":
        self._assert_same_side(other, "add")
        return AxisCoordinate(x=self.x + other.x, y=self.y + other.y, s=self.s, z_offset=self.z_offset + other.z_offset)

    def __sub__(self, other: "AxisCoordinate") -> "AxisCoordinate":
        self._assert_same_side(other, "subtract")
        return AxisCoordinate(x=self.x - other.x, y=self.y - other.y, s=self.s, z_offset=self.z_offset - other.z_offset)

@dataclass
class ToolCoordinate:
    g: float # gripper
    s: float # stage
    r: float # rotation (only 0 or 180) # does not add/subtract

    def __add__(self, other: "ToolCoordinate") -> "ToolCoordinate":
        return ToolCoordinate(g=self.g + other.g, s=self.s + other.s, r=self.r)

    def __sub__(self, other: "ToolCoordinate") -> "ToolCoordinate":
        return ToolCoordinate(g=self.g - other.g, s=self.s - other.s, r=self.r)
