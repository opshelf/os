from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


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
    feed_dots: int = Field(18, ge=0, le=128)


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


class DemoSettings(BaseModel):
    cell_map_path: str = "data/demo.json"


class RobotAppSettings(BaseModel):
    enabled: bool = False
    home_on_init: bool = True


class ScaleAppSettings(BaseModel):
    enabled: bool = False
