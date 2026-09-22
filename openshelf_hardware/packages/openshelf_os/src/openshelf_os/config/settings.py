from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, model_validator

from openshelf_os.config.models import Motor

# Item and tray barcodes are arbitrary customer strings; the item and sizer columns are
# VARCHAR(255), so the API and config schemas share that bound instead of a UPC-A shape.
BARCODE_MAX_LENGTH = 255


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


class DemoFridgeDoorPositionsSettings(BaseModel):
    open_left: float = Field(ge=0)
    closed: float = Field(ge=0)
    open_right: float = Field(ge=0)


class DemoFridgeCellSettings(BaseModel):
    location: StorageBinLocationSettings
    stage_extension_mm: float = Field(gt=0)


class DemoFridgeSettings(BaseModel):
    enabled: bool = False
    motor: Optional[Motor] = None
    door_positions_mm: Optional[DemoFridgeDoorPositionsSettings] = None
    cells: list[DemoFridgeCellSettings] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_enabled_fridge(self) -> "DemoFridgeSettings":
        if self.motor is not None:
            if self.motor.motor_name != "fridge_door":
                raise ValueError("demo.fridge.motor.motor_name must be 'fridge_door'")
            if self.motor.motor_id != 20:
                raise ValueError("demo.fridge.motor.motor_id must be 20")

        if self.enabled is True and self.motor is None:
            raise ValueError("demo.fridge.motor is required when the fridge is enabled")
        if self.enabled is True and self.motor is not None and self.motor.enabled is False:
            raise ValueError("demo.fridge.motor must be enabled when the fridge is enabled")
        if self.enabled is True and self.door_positions_mm is None:
            raise ValueError(
                "demo.fridge.door_positions_mm is required when the fridge is enabled"
            )
        if self.enabled is True and not self.cells:
            raise ValueError("demo.fridge.cells must contain at least one cell")

        if self.door_positions_mm is None:
            return self
        if self.motor is None:
            raise ValueError(
                "demo.fridge.motor is required when fridge door positions are configured"
            )

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
