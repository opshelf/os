from __future__ import annotations

from typing import Any, Iterable, Iterator, List, Literal, Optional, Tuple, Union, overload

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
    # Machines are built with and without the axis limit switches. When false the
    # GPIO limit inputs for this motor are never read: homing does not require the
    # home switch to engage, and for x_axis rotation does not require the
    # clearance switches. Left true, a missing switch reads not engaged and blocks
    # initialization.
    limit_switches_enabled: bool = Field(default=True)
    rotation_safe_stage_extension_enabled: bool = Field(default=False)
    rotation_safe_stage_extension_mm: float = Field(default=8.5, ge=0)
    Ip: int = Field(..., ge=0)
    rpm: int = Field(..., gt=0)
    dr: Literal[0, 1]
    mpr: float = Field(..., gt=0)
    accel: int = Field(..., gt=0)
    ret_rpm: int = Field(..., gt=0)
    home_rpm: Optional[int] = Field(default=None, gt=0)
    # Drive torque homing: stall current as % of Ip, and hold time before zeroing.
    home_torque_pct: int = Field(default=30, ge=1, le=100)
    home_torque_ms: int = Field(default=200, ge=1, le=65535)
    # How far from the drive's zero the axis may read once torque homing stops. Compliant
    # axes such as the IO door spring back off the stop (lb4a-3 read 0.496 mm), so they
    # need more than the default; a false success still reads far outside either value.
    home_zero_tolerance_mm: float = Field(default=0.25, gt=0)
    lim: Union[int, float] = Field(..., gt=0)
    lower_lim: Union[int, float] = Field(default=0, ge=0)
    phys_at_zero: Optional[Union[int, float]] = Field(default=None)
    phys_at_max: Optional[Union[int, float]] = Field(default=None)
    position_reg_high: int = Field(default=0x602C)
    position_reg_low: int = Field(default=0x602D)
    status_reg: int = Field(default=0x1003)
    status_fault_bit: Optional[int] = Field(default=0)
    status_complete_bit: int = Field(default=5)
    brake_output_function: Optional[Literal[0x24, 0xA4]] = Field(default=None)
    brake_release_delay_ms: int = Field(default=250, ge=0, le=1500)
    brake_engagement_delay_ms: int = Field(default=250, ge=0, le=1500)
    brake_engagement_speed_threshold: int = Field(default=10, ge=0, le=500)
    control_mode: Literal[0, 2] = Field(default=2)
    ppr: int = Field(default=10000, ge=200, le=100000)
    encoder_resolution_cpr: int = Field(default=4000, ge=200, le=20000)
    max_following_error: int = Field(default=4000, ge=100, le=65535)
    stall_timeout_s: float = Field(default=5.0, gt=0)

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

    @model_validator(mode="after")
    def _validate_y_axis_controller_profile(self) -> "Motor":
        if self.motor_name != "y_axis":
            return self

        required_profile = {
            "Ip": 60,
            "control_mode": 2,
            "ppr": 10000,
            "encoder_resolution_cpr": 4000,
            "brake_output_function": 0x24,
            "brake_release_delay_ms": 250,
            "brake_engagement_delay_ms": 250,
            "brake_engagement_speed_threshold": 10,
        }
        mismatches = [
            f"{field}={getattr(self, field)} (expected {expected})"
            for field, expected in required_profile.items()
            if getattr(self, field) != expected
        ]
        if mismatches:
            raise ValueError(
                "Y-axis controller profile does not match the supplied 6.0 A, "
                "1000-PPR motor data and application gearing: " + ", ".join(mismatches)
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
    def __iter__(self) -> Iterator[Motor]:  # type: ignore[override]
        return iter(self.root)

    def __len__(self) -> int:
        return len(self.root)

    @overload
    def __getitem__(self, index: int) -> Motor: ...

    @overload
    def __getitem__(self, index: slice) -> List[Motor]: ...

    def __getitem__(self, index: int | slice) -> Motor | List[Motor]:
        return self.root[index]

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
    url: str = ""


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
    type: Literal["pca9535", "pcf8575"] = "pca9535"
    bus: int = Field(1, ge=0)
    address: int = Field(0x20, ge=0, le=0x7F)
    interrupt: Optional[GpioInterruptConfig] = None


class GpioPinConfig(BaseModel):
    board: Literal["rpi", "pca9535", "pcf8575"]
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
        if self.board == "pca9535" and self.expander is None:
            raise ValueError("PCA9535 GPIO pins require expander")
        if self.board == "pcf8575" and self.expander is None:
            raise ValueError("PCF8575 GPIO pins require expander")
        if self.board == "rpi" and self.expander is not None:
            raise ValueError("Raspberry Pi GPIO pins cannot set expander")
        if self.direction == "input" and (self.initial is not None or self.shutdown is not None):
            raise ValueError("GPIO inputs cannot set initial or shutdown")
        return self


def _default_gpio_expanders() -> List[GpioExpanderConfig]:
    shared_interrupt = GpioInterruptConfig(pin=4, pull="up", edge="falling")
    return [
        GpioExpanderConfig(name="pca9535_0", bus=1, address=0x20, interrupt=shared_interrupt),
        GpioExpanderConfig(name="pca9535_1", bus=1, address=0x21, interrupt=shared_interrupt),
    ]


def _rpi_input(
    pin: int,
    *,
    inverted: bool = True,
    debounce_ms: int = 0,
    pull: Literal["up", "down", "off"] = "up",
) -> GpioPinConfig:
    return GpioPinConfig(board="rpi", pin=pin, direction="input", pull=pull, inverted=inverted, debounce_ms=debounce_ms)


def _pca9535_input(
    expander: str,
    pin: int,
    *,
    inverted: bool = True,
    debounce_ms: int = 0,
    pull: Literal["up", "down", "off"] = "off",
) -> GpioPinConfig:
    return GpioPinConfig(
        board="pca9535",
        expander=expander,
        pin=pin,
        direction="input",
        inverted=inverted,
        debounce_ms=debounce_ms,
        pull=pull,
    )


def _pca9535_output(
    expander: str,
    pin: int,
    *,
    initial: Optional[bool] = None,
    shutdown: Optional[bool] = None,
) -> GpioPinConfig:
    return GpioPinConfig(
        board="pca9535",
        expander=expander,
        pin=pin,
        direction="output",
        initial=initial,
        shutdown=shutdown,
    )


def _default_gpio_pins() -> dict[str, GpioPinConfig]:
    return {
        # PCA9535_0 (0x20) - X/Y axis limits and tool limits
        # X/Y end stops (x_lim_min/x_lim_max/y_lim_min/y_lim_max) read engaged
        # on a high input, so they are not inverted; the tool-head limits
        # (r_lim_min/s_lim_min/z_lim_min) and io_lim_min stay inverted. Both
        # types are NPN switches and the difference is measured rather than
        # explained. The post-home limit check reads these pins.
        "x_lim_min": _pca9535_input("pca9535_0", 0, inverted=False),
        "x_lim_max": _pca9535_input("pca9535_0", 1, inverted=False),
        "x_lim_minunsafe": _pca9535_input("pca9535_0", 2),
        "x_lim_maxunsafe": _pca9535_input("pca9535_0", 3),
        "y_lim_min": _pca9535_input("pca9535_0", 4, inverted=False),
        "y_lim_max": _pca9535_input("pca9535_0", 5, inverted=False),
        "y_lim_minunsafe": _pca9535_input("pca9535_0", 6),
        "y_lim_maxunsafe": _pca9535_input("pca9535_0", 7),
        "r_lim_min": _pca9535_input("pca9535_0", 8),
        "s_lim_min": _pca9535_input("pca9535_0", 9),
        "z_lim_min": _pca9535_input("pca9535_0", 10),
        "tool_1": _pca9535_input("pca9535_0", 11),
        "tool_in1": _pca9535_input("pca9535_0", 12),
        "tool_out0": _pca9535_output("pca9535_0", 13),
        "tool_standby": _pca9535_output("pca9535_0", 14),
        "io_lim_min": _pca9535_input("pca9535_0", 15),
        # PCA9535_1 (0x21) - IO box, cabinet locks, estop, power
        "io_lim_max": _pca9535_input("pca9535_1", 0),
        "light_curtain": _pca9535_input("pca9535_1", 1, inverted=False, debounce_ms=50),
        "io_in0": _pca9535_input("pca9535_1", 2),
        "io_1": _pca9535_input("pca9535_1", 3),
        # PCB comparison sheet marks IO2 as an output; kept as input here to
        # preserve existing behavior. Tracked separately, not fixed here.
        "io_2": _pca9535_input("pca9535_1", 4),
        "io_standby": _pca9535_output("pca9535_1", 5),
        "cabinet_top_closed": _pca9535_input("pca9535_1", 6, inverted=True, debounce_ms=50),
        "cabinet_top_unlock": _pca9535_output("pca9535_1", 7, initial=False, shutdown=False),
        "cabinet_bot_closed": _pca9535_input("pca9535_1", 8, inverted=True, debounce_ms=50),
        "cabinet_bot_unlock": _pca9535_output("pca9535_1", 9, initial=False, shutdown=False),
        "estop": _pca9535_input("pca9535_1", 10, inverted=False, pull="off"),
        "sw_estop": _pca9535_output("pca9535_1", 11),
        # X and Y drive power, cut together.
        "pwr_out0": _pca9535_output("pca9535_1", 12),
        "pwr_out1": _pca9535_output("pca9535_1", 13),
        "pwr_out2": _pca9535_output("pca9535_1", 14),
        "pwr_out3": _pca9535_output("pca9535_1", 15),
        # Unrelated to the PCA9535 expanders - direct RPi GPIO, unchanged.
        "tool_2": _rpi_input(15),
        "tool_3": _rpi_input(14),
    }


def compare_gpio_pins_to_defaults(configured: Iterable[str]) -> Tuple[List[str], List[str]]:
    """Pin names the defaults define but a config omits, and ones it adds.

    A config that declares robot.gpio.pins replaces the defaults outright, so a
    renamed or newly added default is silently absent on that machine.
    """
    names = set(configured)
    defaults = set(_default_gpio_pins())

    return sorted(defaults - names), sorted(names - defaults)


#: Pin names that existing config files still use. A config that declares
#: robot.gpio.pins replaces the defaults wholesale, so renaming a default is
#: invisible to those machines until the old key is translated here.
GPIO_PIN_ALIASES = {
    "power_control": "pwr_out0",
    "x_limit_min": "x_lim_min",
    "x_limit_max": "x_lim_max",
    "x_1": "x_lim_minunsafe",
    "x_2": "x_lim_maxunsafe",
    "y_limit_min": "y_lim_min",
    "y_limit_max": "y_lim_max",
    "y_1": "y_lim_minunsafe",
    "y_2": "y_lim_maxunsafe",
    "tool_limit_r": "r_lim_min",
    "tool_limit_s": "s_lim_min",
    "tool_limit_z": "z_lim_min",
    "io_limit_min": "io_lim_min",
    "io_limit_max": "io_lim_max",
}


class GpioConfig(BaseModel):
    expanders: List[GpioExpanderConfig] = Field(default_factory=_default_gpio_expanders)
    pins: dict[str, GpioPinConfig] = Field(default_factory=_default_gpio_pins)

    @model_validator(mode="before")
    @classmethod
    def _apply_pin_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict) is False:
            return data

        pins = data.get("pins")
        if isinstance(pins, dict) is False:
            return data

        renamed: dict[str, Any] = {}
        for name, spec in pins.items():
            alias = GPIO_PIN_ALIASES.get(name)
            # An explicit modern key always wins over its legacy alias.
            if alias is not None and alias in pins:
                continue

            renamed[alias if alias is not None else name] = spec

        if renamed == pins:
            return data

        return {**data, "pins": renamed}


class EmergencyStopConfig(BaseModel):
    enabled: bool = True


class CurtainConfig(BaseModel):
    enabled: bool = True
    poll_interval_s: float = Field(0.01, gt=0)
    door_open_threshold_mm: float = Field(5.0, ge=0)
    door_debounce_s: float = Field(0.0, ge=0)


class ScaleConfig(BaseModel):
    enabled: bool = True
    # Weight change, in grams, that stops y travel while the axis is moving down.
    motion_guard_enabled: bool = True
    motion_guard_delta_grams: float = Field(10.0, gt=0)
    # Consecutive over-threshold readings before the interruption fires; 1 reacts
    # to the first sample, higher values trade latency for immunity to one bad frame.
    motion_guard_min_samples: int = Field(2, ge=1)
    # Ignore the first part of a move, where brake release jolts the cell.
    motion_guard_arm_delay_s: float = Field(0.05, ge=0)
    # After an acknowledgement the weight is still changed, so track the live
    # reading for this long before a new delta can trip again.
    motion_guard_ack_grace_s: float = Field(1.0, ge=0)
    motion_guard_sample_timeout_s: float = Field(0.25, gt=0)
    motion_guard_idle_interval_s: float = Field(0.2, gt=0)


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
    STAGE_RETRACT_POSITION_MM: float = Field(default=3.0, ge=0)
    Z_RETRACT_POSITION_MM: float = Field(default=1.0, ge=0)
    ROTATION_STAGE_MAX_EXTENSION_MM: float = Field(default=5.0, ge=0)
    ROTATION_Z_MAX_EXTENSION_MM: float = Field(default=5.0, ge=0)
    ROTATION_FRONT: float
    ROTATION_BACK: float

    @model_validator(mode="after")
    def _validate_retract_clearance(self) -> "Parameters":
        if self.STAGE_RETRACT_POSITION_MM > self.ROTATION_STAGE_MAX_EXTENSION_MM:
            raise ValueError(
                "STAGE_RETRACT_POSITION_MM cannot exceed "
                "ROTATION_STAGE_MAX_EXTENSION_MM"
            )
        if self.Z_RETRACT_POSITION_MM > self.ROTATION_Z_MAX_EXTENSION_MM:
            raise ValueError(
                "Z_RETRACT_POSITION_MM cannot exceed ROTATION_Z_MAX_EXTENSION_MM"
            )
        return self

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
    poe_camera: PoeCamera = Field(default_factory=PoeCamera)
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
    emergency_stop: EmergencyStopConfig = Field(default_factory=EmergencyStopConfig)
