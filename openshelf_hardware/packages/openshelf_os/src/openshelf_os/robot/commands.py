from typing import Any, Dict, Optional, Literal, Annotated, Union

from pydantic import BaseModel, Field, model_validator

from openshelf_os.common import AxisCoordinate


class StopCommand(BaseModel):
    command: Literal["stop"] = "stop"


class InitializeCommand(BaseModel):
    command: Literal["initialize"] = "initialize"


class IdleCommand(BaseModel):
    command: Literal["idle"] = "idle"


class HomeCommand(BaseModel):
    command: Literal["home"] = "home"


class MoveCommand(BaseModel):
    command: Literal["move"] = "move"
    coordinate: AxisCoordinate


class RetrieveItemCommand(BaseModel):
    command: Literal["retrieve_item"] = "retrieve_item"
    upca: Optional[str] = None
    location: Optional[Dict[str, Any]] = None


class StoreItemCommand(BaseModel):
    command: Literal["store_item"] = "store_item"
    item: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_item_data(self) -> "StoreItemCommand":
        if self.item is None or len(self.item) == 0:
            raise ValueError("item field is required with 'upca' or 'dimensions'")

        has_upca = self.item.get("upca") is not None
        has_dimensions = self.item.get("dimensions") is not None

        if has_upca is False and has_dimensions is False:
            raise ValueError("item must contain 'upca' or 'dimensions'")

        return self


RobotCommands = Annotated[
    Union[
        StopCommand,
        InitializeCommand,
        IdleCommand,
        HomeCommand,
        MoveCommand,
        RetrieveItemCommand,
        StoreItemCommand,
    ],
    Field(discriminator="command"),
]
