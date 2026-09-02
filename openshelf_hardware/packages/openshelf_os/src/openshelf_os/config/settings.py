from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator

from openshelf_os.config.models import Motor


class StorageBinLocationSettings(BaseModel):
    side: Literal["left_module", "right_module"]
    shelf_idx: int = Field(ge=0)
    cell_idx: int = Field(ge=0)


class StorageBinDimensionsSettings(BaseModel):
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    length: float = Field(gt=0)


class StorageBinItemDimensionsSettings(BaseModel):
    width: float = Field(gt=0)
    length: float = Field(gt=0)


class StorageBinSettings(BaseModel):
    barcode: str = Field(pattern=r"^\d{12}$")
    home_location: StorageBinLocationSettings
    inner_dimensions: StorageBinDimensionsSettings
    max_item_dimensions: Optional[StorageBinItemDimensionsSettings] = None
    max_items: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_max_item_footprint(self) -> "StorageBinSettings":
        maximum = self.max_item_dimensions
        if maximum is None:
            return self
        fits = (
            maximum.width <= self.inner_dimensions.width
            and maximum.length <= self.inner_dimensions.length
        ) or (
            maximum.length <= self.inner_dimensions.width
            and maximum.width <= self.inner_dimensions.length
        )
        if not fits:
            raise ValueError("max_item_dimensions must fit inside inner_dimensions")
        return self


class StorageSettings(BaseModel):
    filename: str = "data/storage.json"
    bins: list[StorageBinSettings] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_bins(self) -> "StorageSettings":
        barcodes = [entry.barcode for entry in self.bins]
        locations = [
            (entry.home_location.side, entry.home_location.shelf_idx, entry.home_location.cell_idx)
            for entry in self.bins
        ]
        if len(barcodes) != len(set(barcodes)):
            raise ValueError("storage.bins barcodes must be unique")
        if len(locations) != len(set(locations)):
            raise ValueError("storage.bins home locations must be unique")
        return self


class SizesSettings(BaseModel):
    filename: str = "data/size.json"


class PrinterSettings(BaseModel):
    enabled: bool = False
    queue: Optional[str] = None


class ScannerSettings(BaseModel):
    enabled: bool = False


class ToolCameraSettings(BaseModel):
    enabled: bool = False
    rtsp_url: str = ""


class OverheadCameraSettings(BaseModel):
    enabled: bool = False
    rtsp_url: str = ""


class OverviewCameraSettings(BaseModel):
    enabled: bool = False
    rtsp_url: str = ""


class AuthSettings(BaseModel):
    enabled: bool = True
    require_auth: bool = True
    jwt_secret: Optional[str] = None
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(30, gt=0)
    jwt_refresh_expire_days: int = Field(7, gt=0)
    dev_mode: bool = False
    dev_auto_user: Optional[str] = None


class DemoFridgeDoorPositionsSettings(BaseModel):
    open_left: float = Field(ge=0)
    closed: float = Field(ge=0)
    open_right: float = Field(ge=0)


class DemoFridgeCellSettings(BaseModel):
    location: StorageBinLocationSettings
    door_open_direction: Literal["left", "right"]
    stage_extension_mm: float = Field(gt=0)


class DemoFridgeSettings(BaseModel):
    enabled: bool = False
    motor: Optional[Motor] = None
    door_positions_mm: Optional[DemoFridgeDoorPositionsSettings] = None
    cells: list[DemoFridgeCellSettings] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_enabled_fridge(self) -> "DemoFridgeSettings":
        if self.enabled is False:
            return self

        if self.motor is None:
            raise ValueError("demo.fridge.motor is required when the fridge is enabled")
        if self.motor.enabled is False:
            raise ValueError("demo.fridge.motor must be enabled when the fridge is enabled")
        if self.motor.motor_name != "fridge_door":
            raise ValueError("demo.fridge.motor.motor_name must be 'fridge_door'")
        if self.motor.motor_id != 20:
            raise ValueError("demo.fridge.motor.motor_id must be 20")
        if self.door_positions_mm is None:
            raise ValueError(
                "demo.fridge.door_positions_mm is required when the fridge is enabled"
            )
        if not self.cells:
            raise ValueError("demo.fridge.cells must contain at least one cell")

        lower_limit = float(self.motor.lower_lim)
        upper_limit = float(self.motor.lim)
        positions = self.door_positions_mm
        named_positions = {
            "open_left": float(positions.open_left),
            "closed": float(positions.closed),
            "open_right": float(positions.open_right),
        }
        for name, value in named_positions.items():
            if value < lower_limit or value > upper_limit:
                raise ValueError(
                    f"demo.fridge.door_positions_mm.{name} must be within the "
                    f"fridge door motor limits [{lower_limit}, {upper_limit}]"
                )

        if len(set(named_positions.values())) != len(named_positions):
            raise ValueError("demo fridge door positions must be distinct")
        if not min(positions.open_left, positions.open_right) < positions.closed < max(
            positions.open_left, positions.open_right
        ):
            raise ValueError(
                "demo.fridge.door_positions_mm.closed must be between the two open positions"
            )

        locations = [
            (cell.location.side, cell.location.shelf_idx, cell.location.cell_idx)
            for cell in self.cells
        ]
        if len(locations) != len(set(locations)):
            raise ValueError("demo.fridge.cells locations must be unique")

        return self


class DemoSettings(BaseModel):
    cell_map_path: str = "data/demo.json"
    fridge: DemoFridgeSettings = Field(default_factory=DemoFridgeSettings)


class RobotAppSettings(BaseModel):
    enabled: bool = False
    home_on_init: bool = True


class ScaleAppSettings(BaseModel):
    enabled: bool = False
