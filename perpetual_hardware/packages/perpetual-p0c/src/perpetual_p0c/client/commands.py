from pydantic import BaseModel, Field, UUID4
from uuid import uuid4
from typing import Optional, Union, Literal, Annotated
from perpetual_p0c.storage import CellItem, StorageModuleCellLocation
from perpetual_p0c.tool import ToolCommands
from perpetual_p0c.funnel import FunnelCommands
from perpetual_p0c.workstation import WorkstationCommands
from perpetual_p0c.gantry import GantryCommands

class BaseMessage(BaseModel):
    message_id: UUID4 = Field(default_factory=uuid4)

class StorePayload:
    location: StorageModuleCellLocation
    item: Optional[CellItem]

class StoreCommand(BaseMessage):
    command: Literal["store_item"]
    payload: StorePayload

class RetrievePayload:
    location: StorageModuleCellLocation
    item: Optional[CellItem]

class RetrieveCommand(BaseMessage):
    command: Literal["retrieve_item"]
    location: RetrievePayload 

class HomeCommand(BaseMessage):
    command: Literal["home"]

class StopCommand(BaseMessage):
    command: Literal["stop"]

class PauseCommand(BaseMessage):
    command: Literal["pause"]

class InitializeCommand(BaseMessage):
    command: Literal["initialize"]

class ToolSubsystemCommand(BaseMessage):
    command: Literal["tool_command"]
    payload: ToolCommands 

class FunnelSubsystemCommand(BaseMessage):
    command: Literal["funnel_command"]
    payload: FunnelCommands 

class WorkstationSubsystemCommand(BaseMessage):
    command: Literal["workstation_command"]
    payload: WorkstationCommands

class GantrySubsystemCommands(BaseMessage):
    command: Literal["gantry_command"]
    payload: GantryCommands


# Union of all command models
RobotCommands = Annotated[Union[
    StoreCommand, 
    RetrieveCommand, 
    HomeCommand, 
    StopCommand,
    PauseCommand,
    InitializeCommand,
    ToolSubsystemCommand,
    FunnelSubsystemCommand,
    WorkstationSubsystemCommand,
    GantrySubsystemCommands
], Field(discriminator="command")]

"""
High Level Queues are:

Task Queue: 

This is where incoming storage operations and commands will be processed and read from clients/producers it is a pull based queue. This should be a durable pull based consumer queue.

Event Queue:

This is where events will be sent from the robot client and can be read from subscribers

Emergency Queue:

This is where emergency commands will be handled specifically commands that can interrupt service such as stop/pause/shutdown/initialize

RPC Queue:

This is where commands that don't involve hardware will be used.


High Level Robot Client:

class RobotClient(ABCMeta):
    def get_state(self) -> RobotState:
    def update_storage_map(self, StorageGrid) -> None:
    def get_storage_map(self) -> StorageGrid:
    def stop(self) -> None:
    def pause(self) -> None:
    def shutdown(self) -> None:
    def initialize(self) -> None:
    def store_item(self, location: StorageModuleCellLocation) -> None:
    def retrieve_item(self, location: StorageModuleCellLocation) -> None: 
"""
