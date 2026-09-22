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
    request_id: Optional[str] = Field(default=None, description="Idempotency ID retained for seven days. Reuse only with the same payload.", examples=["store-001"])
    barcode: Optional[str] = None
    location: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Current item location. Required by the retrieval endpoint; bin homes cannot be retrieved directly.",
        examples=[{"side": "left_module", "shelf_idx": 0, "cell_idx": 2}],
        json_schema_extra={"properties": {
            "side": {"type": "string", "enum": ["left_module", "right_module"]},
            "shelf_idx": {"type": "integer", "description": "Zero-based shelf index"},
            "cell_idx": {"type": "integer", "description": "Zero-based cell index"},
        }},
    )


class StoreItemCommand(BaseModel):
    command: Literal["store_item"] = "store_item"
    request_id: Optional[str] = Field(default=None, description="Idempotency ID retained for seven days. Reuse only with the same payload.", examples=["store-001"])
    location: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Requested destination cell. Must exist, be available, and fit the item. "
            "Storage bin homes are reserved and cannot be requested; demo mode keeps its "
            "configured cell mapping."
        ),
        examples=[{"side": "left_module", "shelf_idx": 0, "cell_idx": 2}],
        json_schema_extra={"properties": {
            "side": {"type": "string", "enum": ["left_module", "right_module"]},
            "shelf_idx": {"type": "integer", "description": "Zero-based shelf index"},
            "cell_idx": {"type": "integer", "description": "Zero-based cell index"},
        }},
    )
    item: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Existing inventory barcode, or new item enrollment. Hardware also supports anonymous dimension-based storage; mock requires barcode, name and dimensions. Existing inventory mass/dimensions take precedence.",
        examples=[{"barcode": "880000000001"}, {"barcode": "880000000002", "name": "Opened bottle", "dimensions": {"width": 10, "height": 10, "length": 10}, "tare_weight": 100, "net_weight": 250.5, "full_net_weight": 500}],
        json_schema_extra={"properties": {
            "barcode": {"type": "string", "description": "Inventory barcode"},
            "name": {"type": "string", "description": "New item display name"},
            "dimensions": {"type": "object", "description": "Positive millimeter dimensions", "properties": {key: {"type": "number"} for key in ("width", "height", "length")}},
            "weight": {"type": ["number", "null"], "description": "Gross grams; mutually exclusive with net_weight"},
            "tare_weight": {"type": ["number", "null"], "description": "Empty-container grams"},
            "net_weight": {"type": ["number", "null"], "description": "Contents grams; requires tare and excludes weight"},
            "full_net_weight": {"type": ["number", "null"], "description": "Full contents reference in grams"},
            "metadata": {"type": "object", "additionalProperties": True},
            "access_level_id": {"type": ["integer", "null"]},
        }},
    )

    @model_validator(mode="after")
    def validate_item_data(self) -> "StoreItemCommand":
        if self.item is None or len(self.item) == 0:
            raise ValueError("item field is required with 'barcode' or 'dimensions'")

        has_barcode = self.item.get("barcode") is not None
        has_dimensions = self.item.get("dimensions") is not None

        if has_barcode is False and has_dimensions is False:
            raise ValueError("item must contain 'barcode' or 'dimensions'")

        return self


class DeliverTrayCommand(RetrieveItemCommand):
    """Internal workflow motion; intentionally absent from the public command union."""
    motion: Literal["deliver_tray"] = "deliver_tray"


class ReturnTrayCommand(StoreItemCommand):
    """Internal workflow motion that collects the tray directly from the IO box."""
    motion: Literal["return_tray"] = "return_tray"


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
