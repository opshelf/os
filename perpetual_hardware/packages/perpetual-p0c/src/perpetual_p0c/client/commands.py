from pydantic import BaseModel, Field, UUID4
from uuid import uuid4
from typing import Union, Literal, Annotated
from perpetual_p0c.storage import StorageModuleCellLocation

class BaseMessage(BaseModel):
    message_id: UUID4 = Field(default_factory=uuid4)

class StoreCommand(BaseMessage):
    command: Literal["store_item"]
    location: StorageModuleCellLocation

class RetrieveCommand(BaseMessage):
    command: Literal["retrieve_item"]
    location: StorageModuleCellLocation

class HomeCommand(BaseMessage):
    command: Literal["home"]

class StopCommand(BaseMessage):
    command: Literal["stop"]


# Union of all command models
RobotCommands = Annotated[Union[
    StoreCommand, 
    RetrieveCommand, 
    HomeCommand, 
    StopCommand
], Field(discriminator="command")]

