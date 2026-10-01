from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

from openshelf_os.storage import StorageGrid, StorageModuleCellLocation


class StatusFlags(BaseModel):
    value: int = 0
    stall_guard2_status: bool = False
    over_temperature: bool = False
    over_temperature_pre_warning: bool = False
    short_to_ground_a: bool = False
    short_to_ground_b: bool = False
    open_load_a: bool = False
    open_load_b: bool = False
    stand_still: bool = False


class TrinamicErrorFlags(BaseModel):
    value: int = 0
    stall_guard_error: bool = False
    deviation_error: bool = False


class AxisTelemetry(BaseModel):
    status_flags: StatusFlags = Field(default_factory=StatusFlags)
    error_flags: TrinamicErrorFlags = Field(default_factory=TrinamicErrorFlags)
    position: float = 0.0
    homed: int = 0
    hard_stopped: int = 0


class ToolAxisStatus(BaseModel):
    z_axis: AxisTelemetry
    stage: Optional[AxisTelemetry] = None
    rotate: Optional[AxisTelemetry] = None


class GantryStatus(BaseModel):
    x_axis: AxisTelemetry
    y_axis: AxisTelemetry


class RobotState(BaseModel):
    current_cell: Optional[StorageModuleCellLocation] = None


class CabinetDoorState(BaseModel):
    top_closed: bool
    bottom_closed: bool
    closed: bool
    top_open: bool
    bottom_open: bool
    open: bool


class SafetyStatus(BaseModel):
    active: bool = False
    motion_blocked: bool = False
    awaiting_ack: bool = False
    can_acknowledge: bool = False
    active_sources: list[str] = Field(default_factory=list)
    sources: dict[str, dict[str, Any]] = Field(default_factory=dict)
    interruption_id: int = 0
    last_changed_at: Optional[str] = None
    bypassed: bool = False
    component_bypasses: dict[str, bool] = Field(default_factory=dict)


class ScaleStatus(BaseModel):
    enabled: bool = True
    available: bool = False
    weight_grams: Optional[float] = None
    stable: bool = False
    valid: bool = False
    overload: bool = False
    zero: bool = False
    occupied: bool = False
    threshold_grams: float = 5.0
    decimal_position: Optional[int] = None
    updated_at: Optional[str] = None
    error: Optional[str] = None


class RobotStatus(BaseModel):
    gantry: GantryStatus
    tool: ToolAxisStatus
    state: RobotState
    storage: StorageGrid
    cabinet_doors: Optional[CabinetDoorState] = None
    io_door_open: Optional[bool] = None
    scale: Optional[ScaleStatus] = None
    safety: SafetyStatus = Field(default_factory=SafetyStatus)
    motors: dict[str, AxisTelemetry] = Field(default_factory=dict)


class RobotCommandSuccessful(BaseModel):
    command: str = "unknown"
    message: Literal["success"] = "success"
    location: Optional[StorageModuleCellLocation] = None
    barcode: Optional[str] = None
    weight: Optional[float] = None


class RobotCommandError(BaseModel):
    command: str = "unknown"
    error: str


RobotResponse = Union[RobotCommandSuccessful, RobotCommandError]
