from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union, TypeVar

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    RootModel,
    field_validator,
    model_validator,
)

C = TypeVar("C")
S = TypeVar("S")

CONFIG_PATH = Path("data/config.json")

# ---------------------- NATS Config ----------------------

class RobotNatsConfiguration(BaseModel):
    stream_name: str = "robot"
    command_queue: str = "commands"
    event_queue: str = "events"
    status_queue: str = "robot.status"
    get_storage_subject: str = "robot.get_storage"
    set_storage_subject: str = "robot.set_storage"
    get_config_subject: str = "robot.get_configuration"
    set_config_subject: str = "robot.set_configuration"
    get_status_subject: str = "robot.get_status"

    domain: str = "localhost"
    port: int = 4222

    def connection_string(self) -> str:
        return f"nats://{self.domain}:{self.port}"


# ---------------------- Coordinates ----------------------

@dataclass
class AxisCoordinate:
    x: float
    y: float
    s: int = 0  # 0 -> left module, 1 -> right module
    z_offset: float = 0.0

    def __post_init__(self) -> None:
        if self.s not in (0, 1):
            raise ValueError("AxisCoordinate.s must be 0 (left) or 1 (right)")

    def _assert_same_side(self, other: "AxisCoordinate", op: str) -> None:
        if not isinstance(other, AxisCoordinate):
            raise TypeError(f"Cannot {op} AxisCoordinate with {type(other)!r}")
        if self.s != other.s:
            raise ValueError(
                f"Cannot {op} AxisCoordinate instances from different sides "
                f"(self={self.s}, other={other.s})"
            )

    def __add__(self, other: "AxisCoordinate") -> "AxisCoordinate":
        self._assert_same_side(other, "add")
        return AxisCoordinate(
            x=self.x + other.x,
            y=self.y + other.y,
            s=self.s,
            z_offset=self.z_offset + other.z_offset,
        )

    def __sub__(self, other: "AxisCoordinate") -> "AxisCoordinate":
        self._assert_same_side(other, "subtract")
        return AxisCoordinate(
            x=self.x - other.x,
            y=self.y - other.y,
            s=self.s,
            z_offset=self.z_offset - other.z_offset,
        )


@dataclass
class ToolCoordinate:
    g: float  # gripper
    s: float  # stage
    r: float  # rotation (only 0 or 180) -- does not add/subtract

    def __add__(self, other: "ToolCoordinate") -> "ToolCoordinate":
        return ToolCoordinate(g=self.g + other.g, s=self.s + other.s, r=self.r)

    def __sub__(self, other: "ToolCoordinate") -> "ToolCoordinate":
        return ToolCoordinate(g=self.g - other.g, s=self.s - other.s, r=self.r)


# --------------------------- Helpers ---------------------------

TwoInt = Tuple[int, int]
TwoFloat = Tuple[float, float]

# def _nonneg(v: Union[int, float], name: str) -> Union[int, float]:
#     if v < 0:
#         raise ValueError(f"{name} must be >= 0")
#     return v

def _positive(v: Union[int, float], name: str) -> Union[int, float]:
    if v <= 0:
        raise ValueError(f"{name} must be > 0")
    return v

def _pair_len2(v: List[Union[int, float]], name: str) -> Tuple[Union[int, float], Union[int, float]]:
    if len(v) != 2:
        raise ValueError(f"{name} must be a 2-element list/tuple")
    return v[0], v[1]


# --------------------------- Motor -----------------------------

class Motor(BaseModel):
    motor_name: str = Field(..., min_length=1)
    motor_id: int = Field(..., ge=0)  # tighten if desired
    enabled: bool = Field(default=True)
    Ip: int = Field(..., ge=0)        # keeping exact key casing
    rpm: int = Field(..., gt=0)
    dr: Literal[0, 1]
    mpr: float = Field(..., gt=0)
    accel: int = Field(..., gt=0)
    ret_rpm: int = Field(..., gt=0)
    lim: Union[int, float] = Field(..., gt=0)
    phys_at_zero: Optional[Union[int, float]] = Field(default=None)
    phys_at_max: Optional[Union[int, float]] = Field(default=None)
    position_reg_high: int = Field(default=0x602C)
    position_reg_low: int = Field(default=0x602D)
    status_reg: int = Field(default=0x1003)
    status_fault_bit: Optional[int] = Field(default=0)
    status_complete_bit: int = Field(default=5)
    ppr: int = Field(default=10000, ge=200, le=100000)
    max_following_error: int = Field(default=4000, ge=100, le=65535)

    @field_validator("motor_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("motor_name cannot be empty/whitespace")
        return s

    @field_validator("lim")
    @classmethod
    def _lim_positive(cls, v):
        return _positive(v, "lim")

    @model_validator(mode="after")
    def _validate_physical_endpoints(self) -> "Motor":
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = self.phys_at_max if self.phys_at_max is not None else self.lim

        if p0 == pm:
            raise ValueError(
                f"Motor '{self.motor_name}': phys_at_zero ({p0}) and phys_at_max ({pm}) "
                "cannot be equal"
            )

        phys_min = min(p0, pm)
        phys_max = max(p0, pm)

        if phys_min < 0:
            raise ValueError(
                f"Motor '{self.motor_name}': physical endpoints must be >= 0, "
                f"got phys_at_zero={p0}, phys_at_max={pm}"
            )

        if phys_max > self.lim:
            raise ValueError(
                f"Motor '{self.motor_name}': physical endpoints must be <= lim ({self.lim}), "
                f"got phys_at_zero={p0}, phys_at_max={pm}"
            )

        return self

    def logical_to_physical(self, logical: float) -> float:
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = self.phys_at_max if self.phys_at_max is not None else self.lim
        return p0 + (logical / self.lim) * (pm - p0)

    def physical_to_logical(self, physical: float) -> float:
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = self.phys_at_max if self.phys_at_max is not None else self.lim
        return (physical - p0) / (pm - p0) * self.lim

    def logical_to_physical_delta(self, logical_delta: float) -> float:
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = self.phys_at_max if self.phys_at_max is not None else self.lim
        scale = (pm - p0) / self.lim
        return logical_delta * scale


class Motors(RootModel[List[Motor]]):

    def by_name(self, name: str) -> Motor:
        for m in self.root:
            if m.motor_name == name:
                return m
        raise KeyError(f"Motor with name '{name}' not found")

    def by_id(self, motor_id: int) -> Motor:
        for m in self.root:
            if m.motor_id == motor_id:
                return m
        raise KeyError(f"Motor with id '{motor_id}' not found")

    def names(self) -> List[str]:
        return [m.motor_name for m in self.root]

    def ids(self) -> List[int]:
        return [m.motor_id for m in self.root]


# --------------------------- Cameras ---------------------------

class PoeCamera(BaseModel):
    ip: str
    port: int = Field(..., ge=1, le=65535)
    url: str

    calibration_status: Literal["uncalibrated", "calibrated"] = "uncalibrated"
    mtx: Optional[List[List[float]]] = None
    dist: Optional[List[float]] = None
    rvecs: Optional[List[List[float]]] = None
    tvecs: Optional[List[List[float]]] = None
    newcameramtx: Optional[List[List[float]]] = None
    roi: Optional[List[int]] = None
    H: Optional[List[List[float]]] = None
    map1: Optional[Any] = None
    map2: Optional[Any] = None
    map_size: Optional[List[int]] = None

    H_ref_size: TwoInt
    board_W_mm: Optional[float] = None
    board_H_mm: Optional[float] = None
    pattern_size: TwoInt
    square_size_mm: float = Field(..., gt=0)

    @field_validator("H_ref_size", "pattern_size", mode="before")
    @classmethod
    def _as_tuple_len2(cls, v):
        a, b = _pair_len2(v, "pair")
        return int(a), int(b)

    @field_validator("square_size_mm")
    @classmethod
    def _square_pos(cls, v):
        return _positive(v, "square_size_mm")


class UsbCamera(BaseModel):
    usb_index: int = Field(..., ge=0)
    usb_fourcc: str = Field(..., min_length=1)
    usb_exposure: Union[int, float]
    square_mm: float = Field(..., gt=0)
    fx_px: float = Field(..., gt=0)
    fy_px: float = Field(..., gt=0)
    px_per_mm: float = Field(..., gt=0)
    confidence_min: int = Field(..., ge=0)
    headless_fallback_path: str = Field(..., min_length=1)


class TofCamera(BaseModel):
    csi_port: int = Field(..., ge=0)
    resolution: TwoInt
    fps: int = Field(..., gt=0)
    max_distance_mm: int = Field(..., gt=0)
    confidence_min: float = Field(..., ge=0)
    depth_unit: Literal["mm"] = "mm"
    calibration_status: Literal["uncalibrated", "calibrated"] = "uncalibrated"

    @field_validator("resolution", mode="before")
    @classmethod
    def _res_as_tuple(cls, v):
        a, b = _pair_len2(v, "resolution")
        return int(a), int(b)


class CurtainConfig(BaseModel):
    enabled: bool = True
    pin: int = Field(2, ge=0)
    poll_interval_s: float = Field(0.01, gt=0)
    door_open_threshold_mm: float = Field(5.0, ge=0)
    door_debounce_s: float = Field(0.0, ge=0)


class ScaleConfig(BaseModel):
    enabled: bool = True


# --------------------------- App Settings ---------------------------

class StorageSettings(BaseModel):
    filename: str = "data/storage.json"


class SizesSettings(BaseModel):
    filename: str = "data/size.json"


class PrinterSettings(BaseModel):
    enabled: bool = False
    queue: Optional[str] = None


class ScannerSettings(BaseModel):
    enabled: bool = False


class CameraAppSettings(BaseModel):
    enabled: bool = False
    depth_enabled: bool = False


class ToolCameraSettings(BaseModel):
    enabled: bool = False
    rtsp_url: str = ""


class OverheadCameraSettings(BaseModel):
    enabled: bool = False
    rtsp_url: str = ""


class NatsSettings(BaseModel):
    domain: str = "nats"
    port: int = Field(4222, ge=1, le=65535)

    def connection_string(self) -> str:
        return f"nats://{self.domain}:{self.port}"


class AuthSettings(BaseModel):
    enabled: bool = True
    require_auth: bool = True
    jwt_secret: Optional[str] = None
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(30, gt=0)
    jwt_refresh_expire_days: int = Field(7, gt=0)
    dev_mode: bool = False
    dev_auto_user: Optional[str] = None


class RobotAppSettings(BaseModel):
    enabled: bool = False


class ScaleAppSettings(BaseModel):
    enabled: bool = False


# --------------------------- Buses ---------------------------

class ModbusBusConfig(BaseModel):
    ports: List[str] = Field(default_factory=list)
    baudrate: int = Field(..., gt=0)
    parity: Literal["N", "E", "O"] = "N"
    timeout: float = Field(2.0, gt=0)
    retry_delay_s: float = Field(0.1, ge=0)

    @field_validator("ports")
    @classmethod
    def _validate_ports(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("ports must contain at least one entry")
        cleaned = [p.strip() for p in v if p and p.strip()]
        if not cleaned:
            raise ValueError("ports must contain at least one non-empty entry")
        return cleaned


class RobotBus(BaseModel):
    motor: ModbusBusConfig
    scale: ModbusBusConfig


# ----------------------- Parameters / IO -----------------------

class Parameters(BaseModel):
    IDLE_POSITION: TwoInt
    PICKUP_POSITION_L: TwoInt
    PICKUP_POSITION_H: TwoInt
    SCAN_POSITION: TwoInt
    STAGE_EXTENSION_IO_BOX: Union[int, float]
    STAGE_NUDGE: Union[int, float]
    STAGE_EXTENSION_SHELF_FRONT: Union[int, float]
    STAGE_EXTENSION_SHELF_BACK: Union[int, float]
    Z_EXTENSION_PICK: float
    Z_EXTENSION_PLACE: float
    Z_EXTENSION_IO: float
    Z_MOVE_RPM: int = Field(..., gt=0)
    Z_MOVE_ACCEL: int = Field(..., gt=0)
    ROTATION_FRONT: float
    ROTATION_BACK: float
    JOG_X: Union[int, float]
    JOG_Y: Union[int, float]
    PXMM: float
    PYMM: float

    @field_validator(
        "IDLE_POSITION",
        "PICKUP_POSITION_L",
        "PICKUP_POSITION_H",
        "SCAN_POSITION",
        mode="before",
    )
    @classmethod
    def _pairs_to_tuple(cls, v):
        a, b = _pair_len2(v, "position")
        return int(a), int(b)

    @field_validator("PXMM", "PYMM")
    @classmethod
    def _positive_pxmm(cls, v, info):
        return _positive(v, info.field_name)


class Gripper(BaseModel):
    enabled: bool = Field(default=True)
    motor_id: int = Field(default=9, ge=0)
    control_reg: int = Field(default=0x03E8)
    status_reg: int = Field(default=0x07D0)


class Tool(BaseModel):
    parameters: Parameters
    motors: Motors
    poe_camera: PoeCamera
    gripper: Gripper = Field(default_factory=Gripper)


class IOBlock(BaseModel):
    motors: Motors
    usb_camera: UsbCamera
    tof_camera: TofCamera
    curtain: CurtainConfig = Field(default_factory=CurtainConfig)  # type: ignore[arg-type]
    scale: ScaleConfig = Field(default_factory=ScaleConfig)  # type: ignore[arg-type]

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_curtain(cls, value):
        if isinstance(value, dict) and "curtain" not in value and "curtain_enabled" in value:
            data = dict(value)
            data["curtain"] = {"enabled": data.pop("curtain_enabled")}
            return data
        return value


class GridConfig(BaseModel):
    path: str = Field(..., min_length=1)
    cell_spacing_per_shelf: Optional[Union[float, List[Union[int, float]]]] = None
    cells_per_shelf: Optional[Union[int, List[int], List[List[int]], Dict[str, List[int]]]] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy(cls, value):
        if isinstance(value, str):
            return {"path": value}
        if isinstance(value, dict):
            coerced = value.copy()
            if "storage" in coerced and "path" not in coerced:
                coerced["path"] = coerced.pop("storage")
            if "file" in coerced and "path" not in coerced:
                coerced["path"] = coerced.pop("file")
            return coerced
        return value

    def get_param(self, key: str) -> Any:
        alias_map = {
            "CELL_SPACINGS": "cell_spacing_per_shelf",
            "CELLS_PER_SHELF": "cells_per_shelf",
        }
        target = alias_map.get(key, key)
        if hasattr(self, target):
            return getattr(self, target)
        raise KeyError(f"Grid parameter '{key}' not found")


class Robot(BaseModel):
    tool: Tool
    grid: GridConfig
    io: IOBlock
    bus: Optional[RobotBus] = None
    store_detection_window_s: float = Field(1.0, ge=0)
    store_detection_attempts: int = Field(1, ge=1)


class RootConfig(BaseModel):
    name: str
    version: str
    description: Optional[str] = None
    robot: Robot
    status: Literal["idle", "running", "error"] = "idle"

    storage: StorageSettings = Field(default_factory=StorageSettings)
    sizes: SizesSettings = Field(default_factory=SizesSettings)
    printer: PrinterSettings = Field(default_factory=PrinterSettings)
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    camera: CameraAppSettings = Field(default_factory=CameraAppSettings)
    tool_camera: ToolCameraSettings = Field(default_factory=ToolCameraSettings)
    overhead_camera: OverheadCameraSettings = Field(default_factory=OverheadCameraSettings)
    scale: ScaleAppSettings = Field(default_factory=ScaleAppSettings)
    nats: NatsSettings = Field(default_factory=NatsSettings)  # type: ignore[arg-type]
    auth: AuthSettings = Field(default_factory=AuthSettings)  # type: ignore[arg-type]
    robot_app: RobotAppSettings = Field(default_factory=RobotAppSettings)

    @model_validator(mode="before")
    @classmethod
    def _migrate_robot_enabled(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(values, dict) and "robot_app" not in values:
            if "robot" in values and isinstance(values["robot"], dict):
                robot_enabled = values["robot"].pop("enabled", None)
                if robot_enabled is not None:
                    values["robot_app"] = {"enabled": robot_enabled}
        return values

    # ----------- Convenience Getters (typed) -----------

    # status
    def get_status(self) -> Literal["idle", "running", "error"]:
        return self.status

    # parameters
    def get_parameter(self, key: str) -> Any:
        params = self.robot.tool.parameters
        if not hasattr(params, key):
            raise KeyError(f"Parameter '{key}' not found")
        return getattr(params, key)

    def get_pxmm(self) -> float:
        return self.robot.tool.parameters.PXMM

    def get_pymm(self) -> float:
        return self.robot.tool.parameters.PYMM

    def get_idle_position(self) -> TwoInt:
        return self.robot.tool.parameters.IDLE_POSITION

    # motors
    def list_motors(self, section: Literal["tool", "io"]) -> List[str]:
        motors: Motors = getattr(self.robot, section).motors
        return motors.names()

    def get_motor(self, section: Literal["tool", "io"], *, name: Optional[str] = None,
                  motor_id: Optional[int] = None) -> Motor:
        motors: Motors = getattr(self.robot, section).motors
        if (name is None) == (motor_id is None):
            raise ValueError("Provide exactly one of name or motor_id")
        return motors.by_name(name) if name is not None else motors.by_id(motor_id)  # type: ignore[arg-type]

    # cameras
    def get_poe_camera(self) -> PoeCamera:
        return self.robot.tool.poe_camera

    def get_usb_camera(self) -> UsbCamera:
        return self.robot.io.usb_camera

    def get_tof_camera(self) -> TofCamera:
        return self.robot.io.tof_camera

    # grid
    def get_grid(self) -> Dict[str, Any]:
        return self.robot.grid.model_dump()

    # generic dotted path
    def get(self, dotted_path: str, default: Any = ... ) -> Any:
        """
        Fetch via dotted path (e.g., 'robot.tool.poe_camera.ip' or
        'robot.tool.parameters.PICKUP_POSITION_L').
        Raises KeyError if not found and default is Ellipsis; otherwise returns default.
        """
        cur: Any = self
        parts = dotted_path.split(".")
        try:
            for p in parts:
                if isinstance(cur, BaseModel):
                    cur = getattr(cur, p)
                elif isinstance(cur, dict):
                    cur = cur[p]
                else:
                    cur = getattr(cur, p)
            return cur
        except Exception:
            if default is ...:
                raise KeyError(f"path '{dotted_path}' not found")
            return default


# ----------------------- Loader / Validator -----------------------

class ConfigError(Exception):
    pass


class ConfigValidationError(ConfigError):
    def __init__(self, errors: List[Dict[str, Any]], path: Optional[str] = None):
        self.errors = errors
        self.path = path
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        lines = ["Configuration validation failed:"]
        if self.path is not None:
            lines.append(f"  File: {self.path}")
        lines.append("")
        for err in self.errors:
            loc = ".".join(str(x) for x in err.get("loc", []))
            msg = err.get("msg", "unknown error")
            lines.append(f"  - {loc}: {msg}")
        return "\n".join(lines)


class ConfigMissingError(ConfigError):
    def __init__(self, path: str):
        self.path = path
        super().__init__(f"Configuration file not found: {path}")


def load_and_validate_config(cfg: Union[Dict[str, Any], str], source_path: Optional[str] = None) -> RootConfig:
    data: Dict[str, Any]

    if isinstance(cfg, str):
        data = json.loads(cfg)
    else:
        data = cfg

    try:
        return RootConfig.model_validate(data)
    except ValidationError as e:
        errors = [dict(err) for err in e.errors()]
        raise ConfigValidationError(errors, source_path) from e


def load_config_file(path: Union[str, Any]) -> RootConfig:
    import io
    from pathlib import Path

    path_obj = Path(path) if isinstance(path, str) else path

    if path_obj.exists() is False:
        raise ConfigMissingError(str(path_obj))

    with io.open(path_obj, "r", encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in {path_obj}: {e}") from e

    return load_and_validate_config(raw, str(path_obj))


def assert_config(config: RootConfig, requirements: Optional[List[str]] = None) -> None:
    errors: List[str] = []

    if requirements is None:
        requirements = []

    for req in requirements:
        try:
            value = config.get(req)
            if value is None:
                errors.append(f"Required config '{req}' is None")
        except KeyError:
            errors.append(f"Required config '{req}' is missing")

    if config.auth.jwt_secret is None and config.auth.require_auth is True:
        errors.append("auth.jwt_secret is required when auth.require_auth is True")

    if config.robot_app.enabled is True:
        if config.robot.bus is None:
            errors.append("robot.bus is required when robot is enabled")

    if errors:
        raise ConfigError("Configuration assertion failed:\n  - " + "\n  - ".join(errors))
