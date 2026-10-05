from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, model_validator

# Item and tray barcodes are arbitrary customer strings; the item and sizer columns are
# VARCHAR(255), so the API and config schemas share that bound instead of a UPC-A shape.
BARCODE_MAX_LENGTH = 255


class StorageBinLocationSettings(BaseModel):
    side: Literal["front_module", "back_module"]
    shelf_idx: int = Field(ge=0)
    cell_idx: int = Field(ge=0)


class StorageBinDimensionsSettings(BaseModel):
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    length: float = Field(gt=0)


class StorageBinItemDimensionsSettings(BaseModel):
    width: float = Field(gt=0)
    length: float = Field(gt=0)


class CompartmentLayoutSettings(BaseModel):
    """Equal-sized compartments, numbered left-to-right then top-to-bottom at IO."""
    rows: int = Field(ge=1, le=32, strict=True)
    columns: int = Field(ge=1, le=32, strict=True)


class StorageBinSettings(BaseModel):
    barcode: str = Field(min_length=1, max_length=BARCODE_MAX_LENGTH)
    home_location: StorageBinLocationSettings
    inner_dimensions: StorageBinDimensionsSettings
    max_item_dimensions: Optional[StorageBinItemDimensionsSettings] = None
    max_items: int = Field(ge=1)
    compartment_layout: Optional[CompartmentLayoutSettings] = None

    @model_validator(mode="before")
    @classmethod
    def default_compartment_capacity(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("compartment_layout") is not None and "max_items" not in value:
            layout = CompartmentLayoutSettings.model_validate(value["compartment_layout"])
            return {**value, "max_items": layout.rows * layout.columns}
        return value

    @model_validator(mode="after")
    def validate_max_item_footprint(self) -> "StorageBinSettings":
        if self.compartment_layout and self.max_items > self.compartment_layout.rows * self.compartment_layout.columns:
            raise ValueError("max_items cannot exceed the compartment count")
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
    # Optional higher-resolution RTSP stream used only for crash-recorder
    # capture; the live view always uses rtsp_url (typically the substream).
    recording_url: str = ""


class OverheadCameraSettings(BaseModel):
    enabled: bool = False
    rtsp_url: str = ""
    recording_url: str = ""


class OverviewCameraSettings(BaseModel):
    enabled: bool = False
    rtsp_url: str = ""
    recording_url: str = ""


# Literals that docker-compose.yml published as signing secret defaults. Anyone who
# can read this repository can forge tokens and audit rows signed with them, so they
# are never used to sign; audit integrity keeps them only to verify old rows.
INSECURE_PUBLISHED_SECRETS = frozenset({
    "INSECURE_JWT_SECRET",
    "INSECURE_AUDIT_HMAC_KEY",
    # Was the hard-coded non-development default; stays in PUBLISHED_HMAC_KEYS["v1"] so rows it signed remain verifiable.
    "development-audit-hmac-key-not-for-production",
})


class AuthSettings(BaseModel):
    enabled: bool = True
    require_auth: bool = True
    jwt_secret: Optional[str] = None
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(30, gt=0)
    jwt_refresh_expire_days: int = Field(7, gt=0)
    audit_hmac_key: Optional[str] = None
    dev_mode: bool = False
    dev_auto_user: Optional[str] = None


class DemoSettings(BaseModel):
    cell_map_path: str = "data/demo.json"


class RobotAppSettings(BaseModel):
    enabled: bool = False
    home_on_init: bool = True


class ScaleAppSettings(BaseModel):
    enabled: bool = False
