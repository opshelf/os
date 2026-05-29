from typing import Literal, Optional, Union

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


class RobotStatus(BaseModel):
    gantry: GantryStatus
    tool: ToolAxisStatus
    state: RobotState
    storage: StorageGrid
    curtain_blocked: Optional[bool] = None
    door_open: Optional[bool] = None
    curtain_guard_active: Optional[bool] = None
    awaiting_curtain_ack: bool = False
    motors: dict[str, AxisTelemetry] = Field(default_factory=dict)


class RobotCommandSuccessful(BaseModel):
    command: str = "unknown"
    message: Literal["success"] = "success"
    location: Optional[StorageModuleCellLocation] = None
    upca: Optional[str] = None
    weight: Optional[float] = None


class RobotCommandError(BaseModel):
    command: str = "unknown"
    error: str


RobotResponse = Union[RobotCommandSuccessful, RobotCommandError]
