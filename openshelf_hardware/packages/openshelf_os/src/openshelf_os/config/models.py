from __future__ import annotations

from typing import List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field, RootModel, field_validator, model_validator

from openshelf_os.config.types import TwoInt
from openshelf_os.config.laser_scan import LaserScanConfig


def _positive(v: Union[int, float], name: str) -> Union[int, float]:
    if v <= 0:
        raise ValueError(f"{name} must be > 0")
    return v


def _pair_len2(
    v: List[Union[int, float]], name: str
) -> Tuple[Union[int, float], Union[int, float]]:
    if len(v) != 2:
        raise ValueError(f"{name} must be a 2-element list/tuple")
    return v[0], v[1]


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


class PoeCamera(BaseModel):
    url: str


class UsbCamera(BaseModel):
    usb_index: int = Field(..., ge=0)
    usb_fourcc: str = Field(..., min_length=1)
    usb_exposure: Union[int, float]


class IOBoxSizerCameraConfig(BaseModel):
    device: Union[str, int] = "/dev/video0"
    width: int = Field(1280, gt=0)
    height: int = Field(720, gt=0)
    fps: int = Field(100, gt=0)
    flip_horizontal: bool = True
    buffer_size: int = Field(1, gt=0)


class IOBoxSizerToFConfig(BaseModel):
    ftdi_url: str = Field("ftdi://ftdi:232h/1", min_length=1)
    mux_address: int = Field(112, ge=0, le=127)
    sensor_address: int = Field(41, ge=0, le=127)
    channels: List[int] = Field(default_factory=lambda: [6, 7])
    timing_budget_ms: int = Field(200, gt=0)
    read_timeout_s: float = Field(0.5, gt=0)
    samples: int = Field(1, ge=1, le=25)
    sample_interval_ms: int = Field(0, ge=0, le=1000)
    aggregation: Literal["median", "mean", "nearest", "farthest"] = "median"
    distance_mode: Optional[Literal[1, 2]] = None
    roi_width: Optional[int] = Field(default=None, ge=1, le=16)
    roi_height: Optional[int] = Field(default=None, ge=1, le=16)
    roi_center: Optional[int] = Field(default=None, ge=0, le=255)
    preferred_sensor_index: Optional[int] = Field(default=None, ge=0)

    @field_validator("channels")
    @classmethod
    def _validate_channels(cls, value: List[int]) -> List[int]:
        if len(value) == 0:
            raise ValueError("channels must contain at least one channel")

        if len(set(value)) != len(value):
            raise ValueError("channels must be unique")

        for channel in value:
            if channel < 0 or channel > 7:
                raise ValueError("channels must be integers in range 0..7")

        return value

    @model_validator(mode="after")
    def _validate_tof_selection(self) -> "IOBoxSizerToFConfig":
        if (self.roi_width is None) != (self.roi_height is None):
            raise ValueError("roi_width and roi_height must be configured together")

        if self.preferred_sensor_index is not None and self.preferred_sensor_index >= len(
            self.channels
        ):
            raise ValueError("preferred_sensor_index must reference a configured channel")

        return self


class IOBoxSizerDetectionConfig(BaseModel):
    mode: Literal["brightness", "rectangle", "difference", "circle"] = "brightness"
    roi_x: int = Field(320, ge=0)
    roi_y: int = Field(180, ge=0)
    roi_width: int = Field(640, gt=0)
    roi_height: int = Field(360, gt=0)
    diff_threshold: int = Field(15, ge=0, le=255)
    hsv_v_min: int = Field(170, ge=0, le=255)
    hsv_s_max: int = Field(55, ge=0, le=255)
    white_rgb_min: Tuple[int, int, int] = Field(default=(200, 200, 200))
    min_area_px: int = Field(500, gt=0)
    blank_frame_path: str = "data/iobox_sizer/blank_frame.png"
    contour_selection: Literal["largest", "center"] = "largest"

    @field_validator("white_rgb_min", mode="before")
    @classmethod
    def _validate_rgb_triplet(cls, value) -> Tuple[int, int, int]:
        if isinstance(value, (list, tuple)) is False or len(value) != 3:
            raise ValueError("white_rgb_min must be a 3-element list/tuple")

        rgb = tuple(int(component) for component in value)
        if any(component < 0 or component > 255 for component in rgb):
            raise ValueError("white_rgb_min values must be in range 0..255")

        return rgb


class IOBoxSizerCalibrationConfig(BaseModel):
    tof_blank_mm: List[Optional[float]] = Field(default_factory=lambda: [None, None])
    mm_per_px_x: Optional[float] = Field(default=None, gt=0)
    mm_per_px_y: Optional[float] = Field(default=None, gt=0)
    apply_depth_compensation: bool = False


class IOBoxSizerHistoryConfig(BaseModel):
    save_debug_images: bool = False
    debug_image_dir: str = "data/iobox_sizer/captures"
    max_recent_results: int = Field(200, gt=0)


class IOBoxSizerConfig(BaseModel):
    enabled: bool = False
    observer_mode_enabled: bool = True
    measure_on_store: bool = True
    store_measurement_timeout_s: float = Field(2.0, gt=0)
    camera: IOBoxSizerCameraConfig = Field(default_factory=IOBoxSizerCameraConfig)
    tof: IOBoxSizerToFConfig = Field(default_factory=IOBoxSizerToFConfig)
    detection: IOBoxSizerDetectionConfig = Field(default_factory=IOBoxSizerDetectionConfig)
    calibration: IOBoxSizerCalibrationConfig = Field(default_factory=IOBoxSizerCalibrationConfig)
    history: IOBoxSizerHistoryConfig = Field(default_factory=IOBoxSizerHistoryConfig)


class GpioInterruptConfig(BaseModel):
    board: Literal["rpi"] = "rpi"
    pin: int = Field(..., ge=0)
    pull: Literal["up", "down", "off"] = "up"
    edge: Literal["rising", "falling", "both"] = "falling"
    debounce_ms: int = Field(default=0, ge=0)


class GpioExpanderConfig(BaseModel):
    name: str = Field(..., min_length=1)
    type: Literal["pcf8575"] = "pcf8575"
    bus: int = Field(1, ge=0)
    address: int = Field(0x20, ge=0, le=0x7F)
    interrupt: Optional[GpioInterruptConfig] = None


class GpioPinConfig(BaseModel):
    board: Literal["rpi", "pcf8575"]
    pin: int = Field(..., ge=0)
    direction: Literal["input", "output"]
    expander: Optional[str] = None
    pull: Literal["up", "down", "off"] = "off"
    inverted: bool = False
    debounce_ms: int = Field(default=0, ge=0)
    initial: Optional[bool] = None
    shutdown: Optional[bool] = None
    required: bool = True

    @model_validator(mode="after")
    def _validate_pin(self) -> "GpioPinConfig":
        if self.board == "pcf8575" and self.expander is None:
            raise ValueError("PCF8575 GPIO pins require expander")
        if self.board == "rpi" and self.expander is not None:
            raise ValueError("Raspberry Pi GPIO pins cannot set expander")
        if self.direction == "input" and (self.initial is not None or self.shutdown is not None):
            raise ValueError("GPIO inputs cannot set initial or shutdown")
        return self


def _default_gpio_expanders() -> List[GpioExpanderConfig]:
    return [
        GpioExpanderConfig(
            name="main",
            bus=1,
            address=0x20,
            interrupt=GpioInterruptConfig(pin=4, pull="up", edge="falling"),
        )
    ]


def _rpi_input(
    pin: int,
    *,
    inverted: bool = True,
    debounce_ms: int = 0,
    pull: Literal["up", "down", "off"] = "up",
) -> GpioPinConfig:
    return GpioPinConfig(board="rpi", pin=pin, direction="input", pull=pull, inverted=inverted, debounce_ms=debounce_ms)


def _rpi_output(pin: int, *, initial: Optional[bool] = None, shutdown: Optional[bool] = None) -> GpioPinConfig:
    return GpioPinConfig(
        board="rpi",
        pin=pin,
        direction="output",
        initial=initial,
        shutdown=shutdown,
    )


def _pcf_input(pin: int) -> GpioPinConfig:
    return GpioPinConfig(
        board="pcf8575",
        expander="main",
        pin=pin,
        direction="input",
        inverted=True,
    )


def _default_gpio_pins() -> dict[str, GpioPinConfig]:
    return {
        "tool_limit_r": _rpi_input(8),
        "tool_limit_s": _rpi_input(7),
        "tool_limit_z": _rpi_input(25),
        "cabinet_top_closed": _rpi_input(17, inverted=False, debounce_ms=50),
        "cabinet_bot_closed": _rpi_input(27, inverted=False, debounce_ms=50),
        "estop": _rpi_input(18, pull="off"),
        "tool_1": _rpi_input(1),
        "tool_2": _rpi_input(15),
        "tool_3": _rpi_input(14),
        "io_standby": _rpi_output(23),
        "tool_standby": _rpi_output(24),
        "power_control": _rpi_output(16),
        "cabinet_top_unlock": _rpi_output(5, initial=False, shutdown=False),
        "cabinet_bot_unlock": _rpi_output(6, initial=False, shutdown=False),
        "y_limit_min": _pcf_input(0),
        "y_limit_max": _pcf_input(1),
        "y_1": _pcf_input(2),
        "y_2": _pcf_input(3),
        "x_limit_min": _pcf_input(4),
        "x_limit_max": _pcf_input(5),
        "x_1": _pcf_input(6),
        "x_2": _pcf_input(7),
        "io_limit_max": _pcf_input(10),
        "io_limit_min": _pcf_input(11),
        "io_1": _pcf_input(12),
        "io_2": _pcf_input(13),
        "light_curtain": _pcf_input(8),
    }


class GpioConfig(BaseModel):
    expanders: List[GpioExpanderConfig] = Field(default_factory=_default_gpio_expanders)
    pins: dict[str, GpioPinConfig] = Field(default_factory=_default_gpio_pins)


class CurtainConfig(BaseModel):
    enabled: bool = True
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


class ModbusBusConfig(BaseModel):
    port: str = Field(..., min_length=1)
    baudrate: int = Field(..., gt=0)
    parity: Literal["N", "E", "O"] = "N"
    timeout: float = Field(2.0, gt=0)
    retry_delay_s: float = Field(0.1, ge=0)


class RobotBus(BaseModel):
    motor: ModbusBusConfig
    scale: ModbusBusConfig
    laser: Optional[ModbusBusConfig] = None


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
    usb_camera: Optional[UsbCamera] = None
    curtain: CurtainConfig = Field(default_factory=CurtainConfig)  # type: ignore[arg-type]
    scale: ScaleConfig = Field(default_factory=ScaleConfig)  # type: ignore[arg-type]
    sizer: IOBoxSizerConfig = Field(default_factory=IOBoxSizerConfig)

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


class Robot(BaseModel):
    tool: Tool
    grid: GridConfig
    io: IOBlock
    bus: Optional[RobotBus] = None
    gpio: Optional[GpioConfig] = None
