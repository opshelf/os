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
)

C = TypeVar("C")
S = TypeVar("S")

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

    def __add__(self, other: "AxisCoordinate") -> "AxisCoordinate":
        return AxisCoordinate(x=self.x + other.x, y=self.y + other.y)

    def __sub__(self, other: "AxisCoordinate") -> "AxisCoordinate":
        return AxisCoordinate(x=self.x - other.x, y=self.y - other.y)


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

def _nonneg(v: Union[int, float], name: str) -> Union[int, float]:
    if v < 0:
        raise ValueError(f"{name} must be >= 0")
    return v

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
    Ip: int = Field(..., ge=0)        # keeping exact key casing
    rpm: int = Field(..., gt=0)
    dr: Literal[0, 1]
    mpr: float = Field(..., gt=0)
    accel: int = Field(..., gt=0)
    ret_rpm: int = Field(..., gt=0)
    lim: Union[int, float] = Field(..., gt=0)

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
    confidence_min: int = Field(..., ge=0)
    headless_fallback_path: str = Field(..., min_length=1)


class TofCamera(BaseModel):
    csi_port: int = Field(..., ge=0)
    resolution: TwoInt
    fps: int = Field(..., gt=0)
    max_distance_mm: int = Field(..., gt=0)
    confidence_min: int = Field(..., ge=0)
    depth_unit: Literal["mm"] = "mm"
    calibration_status: Literal["uncalibrated", "calibrated"] = "uncalibrated"

    @field_validator("resolution", mode="before")
    @classmethod
    def _res_as_tuple(cls, v):
        a, b = _pair_len2(v, "resolution")
        return int(a), int(b)


# ----------------------- Parameters / IO -----------------------

class Parameters(BaseModel):
    IDLE_POSITION: TwoInt
    PICKUP_POSITION_L: TwoInt
    PICKUP_POSITION_H: TwoInt
    SCAN_POSITION: TwoInt
    SLIDE_EXTENSION: Union[int, float]
    GRIPPER_EXTENSION_PICK: float
    GRIPPER_EXTENSION_PLACE: float
    DOOR_OPEN: Union[int, float]
    FRONT: float
    BACK: float
    JOG_X: Union[int, float]
    JOG_Y: Union[int, float]
    PXMM: float
    PYMM: float

    @field_validator("IDLE_POSITION", "PICKUP_POSITION_L", "PICKUP_POSITION_H", "SCAN_POSITION", mode="before")
    @classmethod
    def _pairs_to_tuple(cls, v):
        a, b = _pair_len2(v, "position")
        return int(a), int(b)

    @field_validator("PXMM", "PYMM")
    @classmethod
    def _positive_pxmm(cls, v, info):
        return _positive(v, info.field_name)


class Tool(BaseModel):
    parameters: Parameters
    motors: Motors
    poe_camera: PoeCamera


class IOBlock(BaseModel):
    motors: Motors
    usb_camera: UsbCamera
    tof_camera: TofCamera


class Robot(BaseModel):
    tool: Tool
    grid: Dict[str, Any]  # flexible for now
    io: IOBlock


class RootConfig(BaseModel):
    name: str
    version: str
    description: Optional[str] = None
    robot: Robot
    status: Literal["idle", "running", "error"] = "idle"

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
        return self.robot.grid

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

def load_and_validate_config(cfg: Union[Dict[str, Any], str]) -> RootConfig:
    """
    Validate and return a RootConfig.
    - If `cfg` is a dict, it is validated directly.
    - If `cfg` is a JSON string, it is parsed/validated.
    """
    data: Dict[str, Any]
    if isinstance(cfg, str):
        data = json.loads(cfg)
    else:
        data = cfg
    try:
        # Pydantic v2 API
        return RootConfig.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"Invalid configuration:\n{e}") from e


def load_config_file(path: str) -> RootConfig:
    import io
    with io.open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return load_and_validate_config(raw)
