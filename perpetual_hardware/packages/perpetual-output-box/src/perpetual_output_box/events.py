from pydantic import BaseModel, UUID4, Field
from uuid import uuid4
from typing import Literal

class OutputBoxPayload(BaseModel):
    output_box_id: int

class BaseOutputCommand(BaseModel):
    command_id: UUID4 = Field(default_factory=uuid4)

class BaseOutputEvent(BaseModel):
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
