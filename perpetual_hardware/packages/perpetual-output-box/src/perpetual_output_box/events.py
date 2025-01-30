from asyncio import Lock
from pydantic import BaseModel, UUID4, Field
from uuid import uuid4
from typing import Annotated, Literal, Union
from perpetual_output_box.drivers.base import LockState


class OutputBoxPayload(BaseModel):
    output_box_id: int


class OutputBoxMessage(BaseModel):
    message: str


class BaseOutputCommand(BaseModel):
    message_id: UUID4 = Field(default_factory=uuid4)


class BaseOutputEvent(BaseModel):
    message_id: UUID4 = Field(default_factory=uuid4)


class ReceiveItemCommand(BaseOutputCommand):
    command: Literal["receive_item_command"] = "receive_item_command"
    payload: str


class StopCommand(BaseOutputCommand):
    command: Literal["stop"] = "stop"


class HomeCommand(BaseOutputCommand):
    command: Literal["home"] = "home"


class InitializeCommand(BaseOutputCommand):
    command: Literal["initialize"] = "initialize"


class OpenDoorCommand(BaseOutputCommand):
    command: Literal["open_door"] = "open_door"


class CloseDoorCommand(BaseOutputCommand):
    command: Literal["close_door"] = "close_door"


class SetLockCommand(BaseOutputCommand):
    command: Literal["set_lock"] = "set_lock"
    payload: LockState


class ItemReceivedEvent(BaseOutputEvent):
    kind: Literal["item_received"] = "item_received"
    payload: OutputBoxPayload


class FailedToReceiveItemEvent(BaseOutputEvent):
    kind: Literal["item_receive_failed"] = "item_receive_failed"
    payload: OutputBoxMessage


OutputBoxCommands = Annotated[
    Union[
        SetLockCommand,
        OpenDoorCommand,
        CloseDoorCommand,
        InitializeCommand,
        StopCommand,
        HomeCommand,
        ReceiveItemCommand,
    ],
    Field(discriminator="command"),
]

OutputBoxEvents = Annotated[
    Union[ItemReceivedEvent, FailedToReceiveItemEvent],
    Field(discriminator="kind"),
]


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
