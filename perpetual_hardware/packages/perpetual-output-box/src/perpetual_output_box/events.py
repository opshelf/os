from pydantic import BaseModel, TypeAdapter, UUID4, Field
from uuid import uuid4
from typing import Literal
# from nats.aio.msg import Msg
# from nats.js.errors import NotFoundError
# from typing import List, Optional, Union
# from perpetual_p0c.robot import RobotCommands, RobotResponses
# from nats.aio.client import Client as NATS
# from nats.errors import TimeoutError
# from perpetual_p0c_server.core.storage import get_storage


# Kclass OutputNatsConfiguration(BaseModel):
# K    nats_url: str = "nats://localhost:4222"
# K    stream_name: str = "robot"
# K    command_queue: str = "commands"
# K    event_queue: str = "events"
# K
# K
# KNATS_CONFIG = OutputNatsConfiguration()

# RobotEvents = Union[RobotCommands, StorageEvents]
# RobotCommandAdapter = TypeAdapter[RobotCommands](RobotCommands)


class BaseOutputCommand(BaseModel):
    # kind: str
    command_id: UUID4 = Field(default_factory=uuid4)


class BaseOutputEvent(BaseModel):
    # kind: str
    command_id: UUID4 = Field(default_factory=uuid4)


class ReceiveItemCommand(BaseOutputCommand):
    kind: Literal["receive_item_command"] = "receive_item_command"
    payload: str


class StopCommand(BaseOutputCommand):
    kind: Literal["stop"] = "stop"


class HomeCommand(BaseOutputCommand):
    kind: Literal["home"] = "home"


class InitializeCommand(BaseOutputCommand):
    kind: Literal["initialize"] = "initialize"


class OpenDoorCommand(BaseOutputCommand):
    kind: Literal["open_door"] = "open_door"
    payload: bool


class SetLockCommand(BaseOutputCommand):
    kind: Literal["set_lock"] = "set_lock"
    payload: bool


"""
Commands:
Receive Item Command
    Item Being Received - Optional
Stop Command
Home Command
Initialize Command
OpenDoor Command 
Set Lock Event

Events:
Item Received Event
    Item Received
Failed to Receive Event
    Message
Door Locked Event
Status -> Door State/Axis State/Relay State Item in Output
"""
