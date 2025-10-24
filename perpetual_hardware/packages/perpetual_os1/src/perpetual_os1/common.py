from pydantic import BaseModel
from typing import TypeVar
from dataclasses import dataclass

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
    r: float # rotation (only 0 or 180) # does not add/subtract

    def __add__(self, other: "ToolCoordinate") -> "ToolCoordinate":
        return ToolCoordinate(g=self.g + other.g, s=self.s + other.s, r=self.r)

    def __sub__(self, other: "ToolCoordinate") -> "ToolCoordinate":
        return ToolCoordinate(g=self.g - other.g, s=self.s - other.s, r=self.r)
