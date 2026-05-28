from __future__ import annotations

from typing import List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field, RootModel, field_validator, model_validator

from openshelf_os.config.types import TwoInt
from openshelf_os.config.laser_scan import LaserScanConfig


def _positive(v: Union[int, float], name: str) -> Union[int, float]:
    if v <= 0:
        raise ValueError(f"{name} must be > 0")
    return v


def _pair_len2(v: List[Union[int, float]], name: str) -> Tuple[Union[int, float], Union[int, float]]:
    if len(v) != 2:
        raise ValueError(f"{name} must be a 2-element list/tuple")
    return v[0], v[1]


# ---------- Motor ----------

class Motor(BaseModel):
    motor_name: str = Field(..., min_length=1)
    motor_id: int = Field(..., ge=0)
    enabled: bool = Field(default=True)
    rotation_safe_stage_extension_enabled: bool = Field(default=False)
    rotation_safe_stage_extension_mm: float = Field(default=8.5, ge=0)
    Ip: int = Field(..., ge=0)
    rpm: int = Field(..., gt=0)
    dr: Literal[0, 1]
    mpr: float = Field(..., gt=0)
    accel: int = Field(..., gt=0)
    ret_rpm: int = Field(..., gt=0)
    home_rpm: Optional[int] = Field(default=None, gt=0)
    lim: Union[int, float] = Field(..., gt=0)
    lower_lim: Union[int, float] = Field(default=0, ge=0)
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


# ---------- Cameras ----------

class PoeCamera(BaseModel):
    url: str


class UsbCamera(BaseModel):
    usb_index: int = Field(..., ge=0)
    usb_fourcc: str = Field(..., min_length=1)
    usb_exposure: Union[int, float]


# ---------- Peripherals ----------

class CurtainConfig(BaseModel):
    enabled: bool = True
    pin: int = Field(2, ge=0)
    inverted: bool = False
    poll_interval_s: float = Field(0.01, gt=0)
    door_open_threshold_mm: float = Field(5.0, ge=0)
    door_debounce_s: float = Field(0.0, ge=0)


class ScaleConfig(BaseModel):
    enabled: bool = True


class LaserConfig(BaseModel):
    enabled: bool = False
    device_id: int = Field(1, ge=0, le=255)
    read_function_code: int = Field(3, ge=3, le=4)
    register_address: int = Field(0, ge=0, le=65535)
    register_count: int = Field(2, ge=1, le=125)
    distance_register_index: int = Field(1, ge=0, le=124)
    distance_scale_mm: float = Field(0.001, gt=0)

    @model_validator(mode="after")
    def _validate_register_index(self) -> "LaserConfig":
        if self.read_function_code not in (3, 4):
            raise ValueError("read_function_code must be 3 or 4")

        if self.distance_register_index >= self.register_count:
            raise ValueError("distance_register_index must be smaller than register_count")

        return self


# ---------- Buses ----------

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
    laser: Optional[ModbusBusConfig] = None


# ---------- Parameters / tool / IO ----------

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
    laser: LaserConfig = Field(default_factory=LaserConfig)  # type: ignore[arg-type]


class IOBlock(BaseModel):
    motors: Motors
    usb_camera: UsbCamera
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


# ---------- Grid ----------

class GridConfig(BaseModel):
    path: str = Field(..., min_length=1)
    scan: LaserScanConfig = Field(default_factory=LaserScanConfig)  # type: ignore[arg-type]

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


# ---------- Robot ----------

class Robot(BaseModel):
    tool: Tool
    grid: GridConfig
    io: IOBlock
    bus: Optional[RobotBus] = None
